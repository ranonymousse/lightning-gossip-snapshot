"""Bitcoin Core management with fake archives and a loopback test server only."""

import base64
import io
import json
import subprocess
import sys
import tarfile
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from typer.testing import CliRunner

from gossip_snapshot import cli, collector, core
from gossip_snapshot.analysis import sha256_file
from gossip_snapshot.schema import LND_COMMIT, load_json

SYNCED = {
    "getblockchaininfo": {
        "chain": "main",
        "blocks": 950000,
        "headers": 950000,
        "bestblockhash": "11" * 32,
        "verificationprogress": 0.99999,
        "initialblockdownload": False,
        "pruned": False,
        "size_on_disk": 790 * core.GIB,
    },
    "getnetworkinfo": {"connections": 10, "version": 290500, "subversion": "/Satoshi:29.5.0/"},
    "getchainstates": {
        "headers": 950000,
        "chainstates": [{"blocks": 950000, "coins_tip_cache_bytes": 16374 * 1024**2}],
    },
}


def archive(target, version="29.5", names=core.BINARIES):
    with tarfile.open(target, "w:gz") as tar:
        for name in names:
            data = f"#!/bin/sh\n# fake {name}\n".encode()
            info = tarfile.TarInfo(f"bitcoin-{version}/bin/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(core.platform, "system", lambda: "Linux")
    monkeypatch.setattr(core.platform, "machine", lambda: "x86_64")


@pytest.fixture
def installed(tmp_path, linux):
    directory = tmp_path / "state/bitcoin-core"
    record = core.install(
        directory, min_free_gb=0.001, fetch=lambda url, target, expected, progress: archive(target)
    )
    return directory, record


def test_release_is_the_pinned_official_archive_for_linux_only(linux, monkeypatch):
    version, name, url, digest = core.release()
    assert name == f"bitcoin-{version}-x86_64-linux-gnu.tar.gz"
    assert url == f"https://bitcoincore.org/bin/bitcoin-core-{version}/{name}"
    assert (
        len(digest) == 64
        and digest == load_json(core.PROJECT / "versions.json")["bitcoin_core_sha256"][name]
    )
    monkeypatch.setattr(core.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="Linux only"):
        core.release()


def test_download_accepts_only_the_pinned_hash(tmp_path):
    source = tmp_path / "source.tar.gz"
    archive(source)
    core.download(source.as_uri(), tmp_path / "good", sha256_file(source))
    with pytest.raises(ValueError, match="pinned SHA-256"):
        core.download(source.as_uri(), tmp_path / "bad", "00" * 32)


def test_install_is_private_full_chain_loopback_and_walletless(installed, tmp_path):
    directory, record = installed
    assert directory.stat().st_mode & 0o077 == 0
    assert sorted(path.name for path in (directory / "bin").iterdir()) == sorted(core.BINARIES)
    assert all((directory / "bin" / name).stat().st_mode & 0o777 == 0o500 for name in core.BINARIES)
    lines = (directory / "bitcoin.conf").read_text().splitlines()
    for line in (
        "prune=0",
        "disablewallet=1",
        "listen=0",
        "rpcbind=127.0.0.1",
        "rpcallowip=127.0.0.1",
        f"rpcport={core.RPC_PORT}",
        "rpcthreads=16",
        "rpcworkqueue=256",
        "dbcache=16384",
    ):
        assert line in lines
    assert not list(directory.glob("*.tar.gz"))
    assert core.verify(directory) == record
    assert core.status(directory) == {"running": False, "installed_version": record["version"]}
    with pytest.raises(ValueError, match="already exists"):
        core.install(directory, min_free_gb=0.001)
    (directory / "bitcoin.conf").write_text("prune=550\n")
    with pytest.raises(ValueError, match="configuration changed"):
        core.verify(directory)


@pytest.mark.parametrize("problem", ["disk", "download", "archive", "repository"])
def test_failed_install_leaves_no_half_prepared_node(tmp_path, linux, problem):
    directory = tmp_path / "bitcoin-core"

    def fetch(url, target, expected, progress):
        if problem == "download":
            raise ValueError("release archive does not match the pinned SHA-256")
        archive(target, names=("bitcoind",))  # bitcoin-cli missing

    if problem == "repository":
        directory = core.PROJECT / "output/never-created-core"
    with pytest.raises((ValueError, KeyError)):
        core.install(directory, min_free_gb=10**9 if problem == "disk" else 0.001, fetch=fetch)
    assert not directory.exists()


class Node(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.headers["Authorization"], request))
        error = self.server.errors.get(request["method"])
        reply = {"result": None if error else self.server.results[request["method"]]}
        reply["error"] = error
        data = json.dumps(reply).encode()
        self.send_response(500 if error else 200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


@pytest.fixture
def node(installed, monkeypatch):
    directory, _ = installed
    server = HTTPServer(("127.0.0.1", 0), Node)
    server.seen, server.errors, server.results = [], {}, dict(SYNCED, getblockcount=950000)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(core, "RPC_PORT", server.server_address[1])
    monkeypatch.setattr(core, "running", lambda _: True)
    (directory / "data/.cookie").write_text("__cookie__:secret\n")
    yield directory, server
    server.shutdown()


def test_rpc_can_preserve_exact_decimal_amounts_without_changing_default(node):
    directory, server = node
    server.results["gettxout"] = {"value": 0.00000001}
    exact = core.Client(directory, timeout=5, exact_amounts=True)("gettxout", "test", 0, False)
    normal = core.Client(directory, timeout=5)("gettxout", "test", 0, False)
    assert exact["value"] == Decimal("0.00000001")
    assert isinstance(exact["value"], Decimal) and isinstance(normal["value"], float)


def test_rpc_uses_the_cookie_ignores_proxies_and_reports_errors(node, monkeypatch):
    directory, server = node
    monkeypatch.setenv("http_proxy", "http://192.0.2.1:9")  # must never see the credentials
    client = core.Client(directory, timeout=5)
    assert client("getblockcount") == 950000
    header, request = server.seen[-1]
    assert header == "Basic " + base64.b64encode(b"__cookie__:secret").decode()
    assert request["method"] == "getblockcount" and request["params"] == []
    server.errors["getblock"] = {"code": -5, "message": "Block not found"}
    with pytest.raises(core.RPCError, match="Block not found") as raised:
        client("getblock", "00" * 32, 1)
    assert raised.value.code == -5 and "secret" not in str(raised.value)
    (directory / "data/.cookie").unlink()
    with pytest.raises(ValueError, match="not running"):
        client("getblockcount")


def test_collection_needs_a_synchronized_unpruned_mainnet_node(node):
    directory, server = node
    info = core.preflight(directory)
    assert info["blocks"] == 950000 and info["version"] == 290500
    assert info["core_json_sha256"] == sha256_file(directory / "core.json")
    assert info["coins_cache_bytes"] == 16374 * 1024**2 and info["launch"] is None
    for change, message in (
        ({"pruned": True}, "pruned"),
        ({"chain": "test"}, "not on mainnet"),
        ({"initialblockdownload": True}, "still synchronizing"),
        ({"blocks": 949000}, "949000 of 950000"),
    ):
        server.results["getblockchaininfo"] = SYNCED["getblockchaininfo"] | change
        with pytest.raises(ValueError, match=message):
            core.preflight(directory)
    server.errors["getblockchaininfo"] = {"code": -28, "message": "Loading block index…"}
    assert "warming_up" in core.status(directory)
    with pytest.raises(ValueError, match="not ready"):
        core.preflight(directory)


def test_running_reads_the_data_directory_lock_held_by_another_process(installed):
    directory, _ = installed
    assert not core.running(directory)
    lock = directory / "data/.lock"
    lock.touch()
    assert not core.running(directory)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time\n"
            f"stream = open({str(lock)!r}, 'r+b')\n"
            "fcntl.lockf(stream, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\ntime.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert core.running(directory)
    finally:
        holder.kill()
        holder.wait()
    assert not core.running(directory)


@pytest.fixture
def launches(installed, monkeypatch):
    """A node that only records its command line and then answers like a synchronized one."""
    directory, _ = installed
    commands, alive = [], []
    monkeypatch.setattr(core.subprocess, "run", lambda command, **_: commands.append(command))
    monkeypatch.setattr(core, "running", lambda _: len(alive) < len(commands))

    def client(method, *params):
        return dict(SYNCED, getblockcount=950000)[method]

    return directory, commands, alive, client


def test_cache_override_is_passed_for_one_run_and_recorded(launches):
    directory, commands, alive, client = launches
    config = (directory / "bitcoin.conf").read_bytes()
    info = core.start(directory, client=client)
    assert commands[-1] == [
        str(directory / "bin/bitcoind"),
        f"-datadir={directory / 'data'}",
        f"-conf={directory / 'bitcoin.conf'}",
        "-daemon",
    ]
    assert info["launch"]["dbcache_override_mib"] is None
    with pytest.raises(ValueError, match="already running"):
        core.start(directory, dbcache_gb=48, client=client)
    assert len(commands) == 1 and core.start(directory, client=client)["running"]
    assert len(commands) == 1

    alive.append("stopped")
    info = core.start(directory, dbcache_gb=48, client=client)
    assert commands[-1][-1] == "-dbcache=49152" and commands[-1][:-1] == commands[0]
    record = load_json(directory / core.LAUNCH)
    assert info["launch"] == record and record["dbcache_override_mib"] == 49152
    assert record["config_sha256"] == sha256_file(directory / "bitcoin.conf")
    assert (directory / core.LAUNCH).stat().st_mode & 0o077 == 0
    # The generated configuration is untouched, so its fingerprint still verifies.
    assert (directory / "bitcoin.conf").read_bytes() == config and core.verify(directory)

    alive.append("stopped")
    for size in (0.5, 65):
        with pytest.raises(ValueError, match="between 1 and 64"):
            core.start(directory, dbcache_gb=size, client=client)
    assert len(commands) == 2
    assert core.start(directory, client=client)["launch"]["dbcache_override_mib"] is None


def test_start_option_reaches_the_node_and_status_shows_its_cache(launches, monkeypatch):
    directory, commands, _, client = launches
    monkeypatch.setattr(cli, "require_linux", lambda: None)
    monkeypatch.setattr(core, "Client", lambda *_, **__: client)
    result = CliRunner().invoke(
        cli.app, ["core", "--dir", str(directory), "start", "--dbcache-gb", "48"]
    )
    assert result.exit_code == 0, result.output
    assert commands[-1][-1] == "-dbcache=49152"
    assert "Synchronized" in result.output and "16.0 GiB cache" in result.output


def test_stop_asks_over_rpc_and_waits_for_the_lock(installed, monkeypatch):
    directory, _ = installed
    calls, alive = [], iter([True, True, False])
    monkeypatch.setattr(core, "running", lambda _: next(alive))
    monkeypatch.setattr(core.time, "sleep", lambda _: None)
    assert core.stop(directory, client=lambda method: calls.append(method))
    assert calls == ["stop"]
    monkeypatch.setattr(core, "running", lambda _: False)
    assert not core.stop(directory, client=lambda method: calls.append(method))
    assert calls == ["stop"]


def test_stop_keeps_waiting_while_the_node_writes_its_cache(installed, monkeypatch):
    directory, _ = installed
    monkeypatch.setattr(core.time, "sleep", lambda _: None)

    def closed(method):
        raise ValueError("Bitcoin Core is not running (no RPC cookie)")

    # A repeated stop: the RPC is gone, the data directory lock is still held.
    alive = iter([True, True, True, False])
    monkeypatch.setattr(core, "running", lambda _: next(alive))
    assert core.stop(directory, client=closed)

    # A node that is still starting refuses every call, then accepts the request.
    replies = iter([core.RPCError(core.WARMING_UP, "Loading block index…"), None])
    calls = []

    def warming(method):
        calls.append(method)
        if reply := next(replies):
            raise reply

    alive = iter([True, True, True, True, False])
    monkeypatch.setattr(core, "running", lambda _: next(alive))
    assert core.stop(directory, client=warming) and calls == ["stop", "stop"]

    monkeypatch.setattr(core, "running", lambda _: True)
    with pytest.raises(ValueError, match="has not stopped yet"):
        core.stop(directory, wait=0, client=closed)

    def refused(method):
        raise core.RPCError(-32601, "Method not found")

    with pytest.raises(core.RPCError, match="Method not found"):
        core.stop(directory, client=refused)


@pytest.mark.parametrize(
    "script, outcome",
    [
        ("print('[INF] LTND: Waiting for chain backend to finish sync', flush=True)", "passed"),
        (
            "print('unable to create chain control: unexpected reply', flush=True); sys.exit(1)",
            "failed",
        ),
        ("print('still starting', flush=True)", "inconclusive"),
    ],
)
def test_probe_runs_pinned_lnd_in_a_throwaway_directory(
    installed, tmp_path, monkeypatch, script, outcome
):
    directory, _ = installed
    project = tmp_path / "project"
    (project / "bin").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "config/lnd.conf.example").write_text(
        (collector.PROJECT / "config/lnd.conf.example").read_text()
    )
    record = tmp_path / "probe-arguments"
    for name in ("lnd", "lncli", "gossip-export"):
        path = project / "bin" / name
        path.write_text(
            f"#!{sys.executable}\nimport signal, sys, time\n"
            "signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n"
            f"open({str(record)!r}, 'w').write('\\n'.join(sys.argv[1:]))\n"
            f"{script}\ntime.sleep(30)\n"
        )
        path.chmod(0o700)
    collector.save(
        project / "bin/build-manifest.json",
        {
            "lnd_commit": LND_COMMIT,
            "binary_sha256": {
                name: sha256_file(project / "bin" / name)
                for name in ("lnd", "lncli", "gossip-export")
            },
        },
    )
    monkeypatch.setattr(collector, "check_ports", lambda: None)
    with pytest.raises(ValueError, match="not running"):
        collector.probe_backend(directory, project)
    monkeypatch.setattr(core, "running", lambda _: True)
    result = collector.probe_backend(
        directory, project, timeout=3 if outcome == "inconclusive" else 20
    )
    assert result["outcome"] == outcome and result["log_tail"]
    arguments = record.read_text().splitlines()
    assert "--noseedbackup" in arguments
    throwaway = next(a for a in arguments if a.startswith("--lnddir=")).split("=", 1)[1]
    assert "gossip-lnd-probe-" in throwaway and not collector.Path(throwaway).exists()
