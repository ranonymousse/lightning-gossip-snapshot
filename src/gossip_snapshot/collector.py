"""Bounded collector for Linux. No network/node operation occurs on import or in tests."""

import fcntl
import json
import math
import os
import platform
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections import Counter
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import core
from .analysis import analyze, verify_artifact
from .audit import counts as audit_counts
from .audit import write_audit
from .common import COMMIT_ID, GIB, PROJECT, save, sha256_file, utc_now
from .peers import DEFAULT_STALL_MINUTES, DEFAULT_TARGET_PEERS, PeerRecovery
from .performance import DEFAULT_BLOCK_CACHE_GB, Telemetry, settings
from .readiness import MINIMUM_INTERVAL_SECONDS, check_readiness
from .schema import LND_COMMIT, BlockRef, ExportReport, Manifest, decode_scid, load_json
from .verification import verified_records

DEFAULT_DIRECTORY = Path.home() / ".local/state/gossip-snapshot/observer"
# The observer's loopback ports, as config/lnd.conf.example sets them, apart from LND's
# defaults 10009 and 8080.
LND_RPC_PORT = 11009
LND_REST_PORT = 11080
# Observations are taken as far apart as the readiness rule requires.
SAMPLE_SECONDS = MINIMUM_INTERVAL_SECONDS
POLL_SECONDS = 30  # Interval between two looks at the node, its resources and stop requests.
STOP_SECONDS = 300  # Wait for LND to exit after its interrupt signal before it is killed.
RPC_SECONDS = 20  # Time limit of an lncli call that names no other.
# getinfo asks Bitcoin Core three times through the one client that channel
# validation uses, and pinned LND sends those requests one at a time.
INFO_SECONDS = 90
GRAPH_SECONDS = 120  # describegraph returns every channel with its proof.
# lncli ends a dial after peers.CONNECT_TIMEOUT. This leaves it time to report the outcome.
PEER_RPC_SECONDS = 12
ERROR_TAIL = 1000  # Characters of lncli's error output that a message keeps.
GIT_SECONDS = 3  # A hanging git must not hold up the start of a session.
WALLET_SECONDS = 90  # Wait for the wallet RPC of a node that has just started.
PROBE_SECONDS = 180  # Wait for LND's handshake with Bitcoin Core in a probe.
PROBE_LOG_LINES = 25  # Last lines of LND's log that a probe returns with its outcome.
EXPORT_SECONDS = 600  # Time limit of the offline exporter.
# LND's own interval between two historical syncs, in minutes, and the shortest that a
# session may ask for. Only an interval other than LND's is passed to the node.
LND_SYNC_MINUTES = 60
MIN_SYNC_MINUTES = 10
STAT_BLOCK_BYTES = 512  # The unit of st_blocks, whatever the file system's block size.
# Tries for the final sample, which a busy node or a new block can spoil, and the pause
# between two of them.
FINAL_ATTEMPTS = 3
FINAL_RETRY_SECONDS = 20
NO_FINAL_SAMPLE = "no coherent final RPC sample"
SYNC_DEBUG_LEVEL = "info,DISC=debug,GRPH=debug,PEER=debug"
PROBE_MARKER = "Waiting for chain backend to finish sync"
# Isolation, channel validation from the first start, and no peer banning.
# Pinned LND scores a peer for announcing a channel in the observer's own
# closed-channel index, which can disconnect every peer it synchronizes from.
REQUIRED = (
    "bitcoin.mainnet=true",
    "db.backend=bolt",
    "db.use-native-sql=false",
    "nolisten=true",
    f"rpclisten=127.0.0.1:{LND_RPC_PORT}",
    f"restlisten=127.0.0.1:{LND_REST_PORT}",
    "bitcoin.node=bitcoind",
    f"bitcoind.rpchost=127.0.0.1:{core.RPC_PORT}",
    "bitcoind.rpcpolling=true",
    "routing.assumechanvalid=false",
    "gossip.ban-threshold=0",
)


def clean_env():
    """The environment without lncli's own variables, which could point a call at another node."""
    return {key: value for key, value in os.environ.items() if not key.startswith("LNCLI_")}


def collector_revision(project=PROJECT):
    """Record the checkout once at startup, independently of a later watcher."""
    revision = {"commit": None, "dirty": None}
    command = ["git", "--no-optional-locks", "-C", str(project)]
    try:
        commit = subprocess.run(
            command + ["rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
        ).stdout.strip()
        if not COMMIT_ID.fullmatch(commit):
            return revision
        revision["commit"] = commit
        changes = subprocess.run(
            command + ["status", "--porcelain=v1", "--untracked-files=normal"],
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
        ).stdout
        revision["dirty"] = bool(changes.strip())
    except (OSError, subprocess.SubprocessError):
        # Git-less copies can still collect. Unknown must never mean clean.
        pass
    return revision


def check_build(project):
    """The build manifest, once the three binaries match the hashes it records for pinned LND."""
    build = load_json(project / "bin/build-manifest.json")
    if build.get("lnd_commit") != LND_COMMIT:
        raise ValueError("build manifest has the wrong LND commit")
    for name in ("lnd", "lncli", "gossip-export"):
        path = project / "bin" / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"missing {name}; run ./run.sh build --with-node")
        if sha256_file(path) != build["binary_sha256"].get(name):
            raise ValueError(f"binary hash mismatch: {name}")
    return build


def lnd_config(project, core_directory):
    """The node configuration template with the path of Bitcoin Core's cookie filled in."""
    config = (project / "config/lnd.conf.example").read_text()
    return config.replace("COOKIE_PATH", str(core_directory / "data/.cookie"))


def initialize(directory, project=PROJECT, *, core_directory=None):
    """Create a private observer directory with its own copies of the binaries and configuration.

    Bitcoin Core must be running and synchronized. initialization.json records what was
    installed, and every later session checks the directory against it.
    """
    if directory.exists() or directory.is_symlink():
        raise ValueError(
            "observer directory already exists; use run to resume, or choose a new --dir"
        )
    if directory.resolve().is_relative_to(project.resolve()):
        raise ValueError("keep the observer directory and its credentials outside this checkout")
    build = check_build(project)
    core_directory = (core_directory or core.DEFAULT_DIRECTORY).absolute()
    # A synchronized full node must exist before the first LND start.
    preflight = core.preflight(core_directory)
    config = lnd_config(project, core_directory)
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    for name in ("lnd", "bin", "sessions", "provenance"):
        (directory / name).mkdir(mode=0o700)
    for name in ("lnd", "lncli", "gossip-export"):
        shutil.copy2(project / "bin" / name, directory / "bin" / name)
        (directory / "bin" / name).chmod(0o500)
    (directory / "lnd/lnd.conf").write_text(config)
    (directory / "lnd/lnd.conf").chmod(0o600)
    save(directory / "provenance/build-manifest.json", build)
    shutil.copy2(project / "uv.lock", directory / "provenance/uv.lock")
    save(
        directory / "initialization.json",
        {
            "schema_version": 1,
            "created_at": utc_now(),
            "fresh_directory": True,
            "backend": "bitcoind",
            "lnd_commit": LND_COMMIT,
            "config_sha256": sha256_file(directory / "lnd/lnd.conf"),
            "binary_sha256": build["binary_sha256"],
            "core": {"directory": str(core_directory), "preflight": preflight},
            "platform": platform.platform(),
            "python": platform.python_version(),
            "purpose": "bounded collection of public Lightning gossip",
        },
    )
    save(directory / "status.json", {"state": "initialized", "directory": str(directory)})


def verify_directory(directory):
    """Check that an observer directory is private and unchanged since `initialize`.

    Returns the initialization record.
    """
    stat = directory.lstat()
    if directory.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError(
            "observer directory must be owned by this user, private (0700), and not a link"
        )
    init = load_json(directory / "initialization.json")
    if init.get("backend") != "bitcoind":
        raise ValueError(
            "this directory was not initialized for the Bitcoin Core backend; use a new --dir"
        )
    if init["lnd_commit"] != LND_COMMIT or init["fresh_directory"] is not True:
        raise ValueError("observer initialization provenance does not match")
    if sha256_file(directory / "lnd/lnd.conf") != init["config_sha256"]:
        raise ValueError("observer configuration changed; do not reuse it as validated collection")
    lines = (directory / "lnd/lnd.conf").read_text().splitlines()
    if any(line not in lines for line in REQUIRED):
        raise ValueError("observer configuration lacks required validation/isolation settings")
    cookie = f"bitcoind.rpccookie={Path(init['core']['directory']) / 'data/.cookie'}"
    if cookie not in lines:
        raise ValueError("observer configuration does not use the recorded Bitcoin Core node")
    for name in ("lnd", "lncli", "gossip-export"):
        if sha256_file(directory / "bin" / name) != init["binary_sha256"][name]:
            raise ValueError(f"observer binary changed: {name}")
    return init


def core_rpc(init):
    """An RPC client for the Bitcoin Core node that this observer directory was prepared with."""
    return core.Client(Path(init["core"]["directory"]))


def probe_backend(core_directory, project=PROJECT, *, timeout=PROBE_SECONDS):
    """Start pinned LND against Bitcoin Core in a throwaway directory.

    LND logs PROBE_MARKER only after its RPC handshake, feature checks and wallet
    start against the backend succeeded. The probe stops it there, before any
    Lightning peer is contacted, and deletes the throwaway wallet.
    """
    check_build(project)
    core.verify(core_directory)
    if not core.running(core_directory):
        raise ValueError("Bitcoin Core is not running; use ./run.sh core start")
    check_ports()
    with tempfile.TemporaryDirectory(prefix="gossip-lnd-probe-") as temporary:
        root = Path(temporary)
        (root / "lnd.conf").write_text(lnd_config(project, core_directory))
        with (root / "lnd.log").open("xb") as log:
            child = subprocess.Popen(
                [
                    str(project / "bin/lnd"),
                    f"--lnddir={root / 'lnd'}",
                    f"--configfile={root / 'lnd.conf'}",
                    "--noseedbackup",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=clean_env(),
            )
        deadline = time.monotonic() + timeout
        try:
            while True:
                text = (root / "lnd.log").read_text(errors="replace")
                if PROBE_MARKER in text:
                    outcome = "passed"
                elif child.poll() is not None:
                    # Read once more: the last lines may postdate the check above.
                    text = (root / "lnd.log").read_text(errors="replace")
                    outcome = "passed" if PROBE_MARKER in text else "failed"
                elif time.monotonic() >= deadline:
                    outcome = "inconclusive"
                else:
                    time.sleep(0.5)
                    continue
                break
        finally:
            stop_child(child)
        return {"outcome": outcome, "log_tail": text.splitlines()[-PROBE_LOG_LINES:]}


def cli_args(directory, *args, readonly=True):
    """An lncli command line that addresses this observer only, read-only unless stated."""
    command = [
        str(directory / "bin/lncli"),
        "--network=mainnet",
        f"--lnddir={directory / 'lnd'}",
        f"--rpcserver=127.0.0.1:{LND_RPC_PORT}",
    ]
    # Pinned lncli rejects --profile= when the profile file does not exist.
    # When it does exist, an empty explicit profile disables its default.
    if (Path.home() / ".lncli/profiles.json").exists():
        command.append("--profile=")
    if readonly:
        command.append(
            f"--macaroonpath={directory / 'lnd/data/chain/bitcoin/mainnet/readonly.macaroon'}"
        )
    return command + list(args)


def rpc(directory, *args, timeout=RPC_SECONDS):
    """Run one read-only lncli command against the observer and return its JSON reply."""
    result = subprocess.run(
        cli_args(directory, *args), env=clean_env(), capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise ValueError(result.stderr.strip()[-ERROR_TAIL:] or f"lncli {args[0]} failed")
    return json.loads(result.stdout)


def peer_rpc(directory, command, *args):
    """The sole administrative automation: connect/disconnect the empty observer."""
    if command not in ("connect", "disconnect"):
        raise ValueError("peer recovery may only connect or disconnect peers")
    result = subprocess.run(
        cli_args(directory, readonly=False)
        + [f"--macaroonpath={directory / 'lnd/data/chain/bitcoin/mainnet/admin.macaroon'}"]
        + [command, *args],
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=PEER_RPC_SECONDS,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip()[-ERROR_TAIL:] or f"lncli {command} failed")
    return json.loads(result.stdout)


def wallet(directory):
    """Create or unlock the observer's wallet in this terminal; returns lncli's exit status.

    Waits until the node's wallet RPC answers. The prompts are lncli's own.
    """
    verify_directory(directory)
    deadline = time.monotonic() + WALLET_SECONDS
    while True:
        try:
            state = rpc(directory, "state")["state"]
            break
        except (ValueError, OSError, subprocess.TimeoutExpired):
            if time.monotonic() >= deadline:
                raise ValueError(
                    "wallet RPC is not ready; check ./run.sh collect status and lnd.log"
                ) from None
            time.sleep(2)
    command = {"NON_EXISTING": "create", "LOCKED": "unlock"}.get(state)
    if command:
        # Deliberately interactive. Never capture the seed/password in our logs.
        print(
            "Use a new empty wallet for this observer. Keep its seed private and leave it unfunded."
        )
        return subprocess.run(
            cli_args(directory, command, readonly=False), env=clean_env(), check=False
        ).returncode
    print(f"Wallet state: {state}. No creation/unlock needed at this stage.")
    return 0


@contextmanager
def supervisor_lock(directory):
    """Hold the lock that makes this process the only supervisor of an observer directory."""
    with (directory / "supervisor.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("a collection supervisor is already running") from None
        yield


def receive_bytes():
    """Bytes that the host has received on every interface but loopback, from /proc/net/dev."""
    total = 0
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, values = line.split(":", 1)
        if name.strip() != "lo":
            total += int(values.split()[0])
    return total


def supervisor_running(directory):
    """Read the ownership lock without creating files or trusting saved PIDs."""
    try:
        with (directory / "supervisor.lock").open("r") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def request_stop(directory):
    """Ask the running supervisor for a clean stop; returns whether one is running."""
    verify_directory(directory)
    if not supervisor_running(directory):
        return False
    (directory / "stop.request").touch(mode=0o600)
    return True


def resources(directory, initial_rx):
    """Free disk, the observer directory's size and what the host received during the session."""
    used = allocated_bytes(directory)
    return {
        "free_bytes": shutil.disk_usage(directory).free,
        "observer_disk_bytes": used,
        "host_received_bytes": max(0, receive_bytes() - initial_rx),
    }


def allocated_bytes(directory):
    """Disk space that the regular files below a directory occupy."""
    used = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                used += path.stat().st_blocks * STAT_BLOCK_BYTES
        except FileNotFoundError:
            pass  # LND may rotate a log between listing and stat.
    return used


def resource_limit(measured, args):
    """The budget that a measurement has spent, or None while the session may continue."""
    if measured["free_bytes"] < args.min_free_gb * GIB:
        return "free disk threshold"
    if measured["observer_disk_bytes"] >= args.max_data_gb * GIB:
        return "observer disk budget"
    if measured["host_received_bytes"] >= args.max_download_gb * GIB:
        return "Host download budget"
    return None


def block_ref(info):
    """The chain tip of a getinfo reply as a validated block reference."""
    return BlockRef.model_validate({"height": info["block_height"], "hash": info["block_hash"]})


def sample(directory, destination):
    """Save one observation of the node: its graph and its peers at an unchanged chain tip.

    The complete replies go to `destination`. The result keeps the time, the getinfo reply
    and the channel identifiers, which is what the readiness rule compares.
    """
    before = rpc(directory, "getinfo", timeout=INFO_SECONDS)
    graph = rpc(directory, "describegraph", "--include_auth_proof", timeout=GRAPH_SECONDS)
    peers = rpc(directory, "listpeers")
    after = rpc(directory, "getinfo", timeout=INFO_SECONDS)
    if block_ref(before) != block_ref(after):
        raise ValueError("chain tip changed while sampling; retry with a consistent reference")
    if after.get("chains") != [{"chain": "bitcoin", "network": "mainnet"}]:
        raise ValueError("RPC is not the expected Bitcoin mainnet node")
    ids = [edge["channel_id"] for edge in graph["edges"]]
    if any(not isinstance(scid, str) for scid in ids) or len(set(ids)) != len(ids):
        raise ValueError("RPC channel IDs must be distinct decimal strings")
    result = {"sampled_at": utc_now(), "getinfo": after, "scids": sorted(ids, key=int)}
    save(destination, {**result, "describegraph": graph, "listpeers": peers})
    return result


def stop_child(child):
    """Only signal the child we own, never a PID read from a stale file."""
    if child.poll() is not None:
        return False
    child.send_signal(signal.SIGINT)
    try:
        return child.wait(timeout=STOP_SECONDS) == 0
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        return False


def inspect_export(capture, workers=0):
    """Verify every exported record and reconcile the counts with the exporter's report.

    Returns the report and the exported channel identifiers.
    """
    report = ExportReport.model_validate(load_json(capture / "export-report.json"))
    if sha256_file(capture / "records.jsonl") != report.records_sha256:
        raise ValueError("export records hash mismatch")
    ids, outpoints, exclusions = set(), set(), Counter()
    included = 0
    with closing(verified_records(capture / "records.jsonl", workers)) as records:
        for record in records:
            if record.scid in ids or record.outpoint in outpoints:
                raise ValueError("duplicate exported SCID or outpoint")
            ids.add(record.scid)
            outpoints.add(record.outpoint)
            if record.height > report.graph_prune_tip.height:
                raise ValueError("exported funding height exceeds graph reference")
            if record.exclusion:
                exclusions[record.exclusion] += 1
            else:
                included += 1
    if (len(ids), included, dict(exclusions)) != (
        report.exported_records,
        report.included_records,
        report.exclusions,
    ):
        raise ValueError("Go/Python export counts do not reconcile")
    return report, ids


def freeze_and_analyze(
    directory,
    session,
    samples,
    final,
    clean,
    max_data_bytes,
    on_phase=lambda _phase: None,
    *,
    workers=0,
):
    """Turn the graph database of a stopped node into a capture or a diagnostic export.

    The database is copied, exported and verified, and the closed-channel index is audited.
    Only a session that reached readiness, and whose final sample equals the export at the
    same block, gets a manifest and an analysis. Returns the outcome for the session's result.
    """
    if not clean:
        raise ValueError("no clean node shutdown; no snapshot copied")
    init = verify_directory(directory)
    on_phase("freezing")
    database = directory / "lnd/data/graph/mainnet/channel.db"
    if not database.is_file() or database.is_symlink():
        raise ValueError("no regular Bolt graph database yet")
    if shutil.disk_usage(session).free < 2 * database.stat().st_size + GIB:
        raise ValueError("insufficient disk headroom for frozen and working DB copies")
    if allocated_bytes(directory) + 2 * database.stat().st_size >= max_data_bytes:
        raise ValueError("offline DB copies would exceed the observer disk budget")
    frozen, working = session / "channel.db", session / "working-channel.db"
    before = sha256_file(database)
    for path in (frozen, working):
        if path.exists():
            raise ValueError("snapshot destination already exists")
        shutil.copyfile(database, path)
        path.chmod(0o400)
        if sha256_file(path) != before:
            raise ValueError("database changed during offline copy")
    if sha256_file(database) != before:
        raise ValueError("live database changed after shutdown")
    capture = session / "capture"
    on_phase("exporting")
    with (session / "export.log").open("x") as log:
        subprocess.run(
            [str(directory / "bin/gossip-export"), "--db", str(working), "--output", str(capture)],
            check=True,
            timeout=EXPORT_SECONDS,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if sha256_file(frozen) != before or sha256_file(working) != before:
        raise ValueError("offline export changed a database copy")
    on_phase("verifying")
    report, ids = inspect_export(capture, workers)
    if report.database_sha256 != before:
        raise ValueError("export report does not identify the frozen database")
    outcome = {
        "capture": str(capture),
        "exported_records": report.exported_records,
        "included_records": report.included_records,
        "database_sha256": before,
        "python_signatures_checked": True,
        "artifact_verified": False,
    }
    if ids:
        # A historical sync fetches channels oldest first, so a large gap between the
        # newest funding block and the tip marks a sync that stopped early.
        outcome["newest_funding_lag_blocks"] = report.graph_prune_tip.height - max(
            decode_scid(scid)[0] for scid in ids
        )
    # Diagnostic exports are audited too: a live channel in the closed index
    # is the failure the Bitcoin Core backend was chosen to avoid.
    on_phase("auditing")
    try:
        outcome["closed_audit"] = audit_counts(write_audit(capture, core_rpc(init)))
    except (OSError, ValueError) as exc:
        outcome["closed_audit_error"] = str(exc)
    # A short session may export authenticated records before becoming ready.
    # Such an export gets no mainnet manifest and cannot pass the analysis CLI.
    readiness = samples[-3:-1] + [final] if final is not None else []
    decision = check_readiness(readiness)
    if final is None:
        # Nothing ties the RPC view to the stopped database. Report what the
        # session's own samples show, never a sample count that hides the cause.
        seen = check_readiness(samples)
        reasons = sorted({*seen["reasons"], NO_FINAL_SAMPLE})
        decision = {"ready": False, "reasons": reasons, "churn": seen["churn"]}
    outcome["readiness"] = decision
    if not decision["ready"]:
        return {**outcome, "state": "diagnostic"}
    if "closed_audit" not in outcome:
        raise ValueError(f"closed-index audit did not complete: {outcome['closed_audit_error']}")
    if report.graph_prune_tip != block_ref(final["getinfo"]):
        raise ValueError("graph prune tip differs from final RPC reference; resume and recapture")
    if ids != set(final["scids"]):
        raise ValueError("graph membership changed during shutdown; resume and recapture")
    save(capture / "readiness.json", readiness)
    manifest = Manifest(
        schema_version=1,
        dataset_kind="mainnet",
        # The observation instant, which also anchors the update-recency window.
        captured_at=final["sampled_at"],
        reference=report.graph_prune_tip,
        backend="bitcoind",
        validation_enabled_from_start=True,
        clean_shutdown=True,
        records_sha256=report.records_sha256,
        export_report_sha256=sha256_file(capture / "export-report.json"),
        readiness_sha256=sha256_file(capture / "readiness.json"),
        closed_audit_sha256=sha256_file(capture / "closed-audit.json"),
    )
    save(capture / "manifest.json", manifest.model_dump(exclude={"pilot"}))
    output = session / "analysis"
    on_phase("analyzing")
    analyze(capture, output, workers=workers)
    on_phase("verifying")
    verify_artifact(output, capture, workers=workers)
    return {**outcome, "state": "verified", "artifact_verified": True, "analysis": str(output)}


def check_ports():
    """Refuse to start while another process holds one of the observer's loopback ports."""
    for port in (LND_RPC_PORT, LND_REST_PORT):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise ValueError(f"observer port {port} is already in use") from None


def node_command(
    directory,
    performance,
    *,
    keep_slow_peers=False,
    debug_sync=False,
    historical_sync_minutes=LND_SYNC_MINUTES,
):
    """Auditable session overrides; the original config and binaries stay intact."""
    command = [
        str(directory / "bin/lnd"),
        f"--lnddir={directory / 'lnd'}",
        f"--configfile={directory / 'lnd/lnd.conf'}",
        f"--blockcachesize={performance['block_cache_bytes']}",
    ]
    if keep_slow_peers:
        # Present in pinned LND 390b2d2. Only suppresses the pong-failure
        # disconnect callback, not signature/chain checks or sync criteria.
        command.append("--no-disconnect-on-pong-failure")
    if debug_sync:
        command.append(f"--debuglevel={SYNC_DEBUG_LEVEL}")
    if historical_sync_minutes != LND_SYNC_MINUTES:
        # LND reconciles with one more random peer per interval, one hour by
        # default. A shorter one gives a resumed graph more peers to compare with.
        command.append(f"--historicalsyncinterval={historical_sync_minutes}m")
    return command


def final_observation(status, final):
    """Use the final coherent RPC sample, never an earlier recovery cache."""
    if final is None:
        return
    status["node"] = {
        key: final["getinfo"][key]
        for key in ("block_height", "synced_to_chain", "synced_to_graph", "num_peers")
    }
    status["node_sampled_at"] = final["sampled_at"]
    status["graph_channels"] = len(final["scids"])
    status["graph_sampled_at"] = final["sampled_at"]
    status["final_sampled_at"] = final["sampled_at"]


def run_session(directory, args, *, emit_result=True):
    """Supervise one bounded session, from the start of the node to its recorded result.

    LND runs until readiness, a spent budget, a stalled synchronization or a stop request.
    The session then takes its final sample, stops the node and freezes the outcome.
    Returns 0 for a verified capture or a diagnostic export, and 2 otherwise.
    """
    init = verify_directory(directory)
    with supervisor_lock(directory):
        revision = collector_revision()
        check_ports()
        performance = settings(
            getattr(args, "block_cache_gb", DEFAULT_BLOCK_CACHE_GB), getattr(args, "workers", 0)
        )
        lnd_command = node_command(
            directory,
            performance,
            keep_slow_peers=getattr(args, "keep_slow_peers", True),
            debug_sync=getattr(args, "debug_sync", False),
            historical_sync_minutes=getattr(args, "historical_sync_minutes", LND_SYNC_MINUTES),
        )
        logging = {
            "debug_sync": getattr(args, "debug_sync", False),
            "level_override": SYNC_DEBUG_LEVEL if getattr(args, "debug_sync", False) else None,
            "file": "lnd.log",
        }
        initial_rx = receive_bytes()
        if reason := resource_limit(resources(directory, initial_rx), args):
            raise ValueError(f"cannot start: {reason}")
        preflight = core.preflight(Path(init["core"]["directory"]))
        session = directory / "sessions" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        session.mkdir(mode=0o700)
        (session / "observations").mkdir(mode=0o700)
        recovery = PeerRecovery(
            session,
            lambda *a, **kw: rpc(directory, *a, **kw),
            lambda *a: peer_rpc(directory, *a),
            target=getattr(args, "target_peers", DEFAULT_TARGET_PEERS),
            stall_minutes=getattr(args, "stall_minutes", DEFAULT_STALL_MINUTES),
        )
        started_at = utc_now()
        limits = {
            key: getattr(args, key)
            for key in ("hours", "min_free_gb", "max_data_gb", "max_download_gb")
        }
        save(
            session / "start.json",
            {
                "started_at": started_at,
                "collector_revision": revision,
                "limits": vars(args) | {"dir": str(directory)},
                "core_preflight": preflight,
                "performance": performance,
                "base_config_sha256": sha256_file(directory / "lnd/lnd.conf"),
                "initialization_sha256": sha256_file(directory / "initialization.json"),
                "collector_sources": {
                    p.name: sha256_file(p) for p in Path(__file__).parent.glob("*.py")
                },
                "lnd_command": lnd_command,
                "logging": logging,
                "peer_recovery_policy": recovery.policy(),
            },
        )
        stop_file = directory / "stop.request"
        stop_file.unlink(missing_ok=True)
        requested = threading.Event()
        child, clean, final, samples, history = None, False, None, [], []
        reason, failure = "time budget", None
        started, next_sample, wallet_state = time.monotonic(), 0, None
        status = {
            "state": "starting",
            "collector_revision": revision,
            "started_at": started_at,
            "limits": limits,
            "session": str(session),
            "updated_at": utc_now(),
            "elapsed_seconds": 0,
            "performance": performance,
            "logging": logging,
        }
        save(directory / "status.json", status)
        previous = {
            sig: signal.signal(sig, lambda *_: requested.set())
            for sig in (signal.SIGINT, signal.SIGTERM)
        }

        def phase(name):
            status.update(state=name, updated_at=utc_now())
            save(directory / "status.json", status)

        try:
            with (session / "lnd.log").open("xb") as log:
                child = subprocess.Popen(
                    lnd_command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=clean_env(),
                )
                telemetry = Telemetry(child.pid)
                telemetry.sample(0)
                if emit_result:
                    print(
                        "Collector started. Run ./run.sh collect wallet in another terminal. "
                        f"Logs: {session}",
                        flush=True,
                    )
                while time.monotonic() - started < args.hours * 3600:
                    if child.poll() is not None:
                        raise ValueError(
                            f"LND exited unexpectedly ({child.returncode}); inspect lnd.log"
                        )
                    measured = resources(directory, initial_rx)
                    measured.update(telemetry.sample(measured["host_received_bytes"]))
                    # Keep the measurement that actually triggered the guard,
                    # including when the loop exits before its next RPC poll.
                    status.update(resources=measured, updated_at=utc_now())
                    reason = resource_limit(measured, args)
                    if reason or requested.is_set() or stop_file.exists():
                        reason = reason or "operator stop"
                        break
                    status = {
                        "state": "running",
                        "collector_revision": revision,
                        "started_at": started_at,
                        "limits": limits,
                        "updated_at": utc_now(),
                        "session": str(session),
                        "resources": measured,
                        "performance": performance,
                        "logging": logging,
                    }
                    try:
                        state = rpc(directory, "state")["state"]
                        status["wallet_state"] = state
                        if state != wallet_state:
                            # The first answers after an unlock can take minutes on a
                            # populated graph. Show the new state without waiting.
                            wallet_state = state
                            save(directory / "status.json", status)
                        if state == "SERVER_ACTIVE":
                            info = rpc(directory, "getinfo", timeout=INFO_SECONDS)
                            status["node_sampled_at"] = utc_now()
                            status["peer_recovery"] = recovery.tick(info)
                            status["node"] = {
                                key: info[key]
                                for key in (
                                    "block_height",
                                    "synced_to_chain",
                                    "synced_to_graph",
                                    "num_peers",
                                )
                            }
                            if time.monotonic() >= next_sample:
                                item = sample(
                                    directory, session / "observations" / f"{len(history):04}.json"
                                )
                                history.append(item)
                                samples.append(item)
                                next_sample = time.monotonic() + SAMPLE_SECONDS
                                save(session / "sample-history.json", history)
                                decision = check_readiness(samples)
                                status["readiness"] = decision
                        else:
                            samples = []
                    except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
                        # A failed RPC or sample, for example a block arriving
                        # mid-sample, is retried on the next poll. Earlier
                        # samples remain valid evidence of the same node.
                        status["last_rpc_error"] = str(exc)
                    status["readiness"] = check_readiness(samples)
                    status["readiness_samples"] = len(samples)
                    if history:
                        status["graph_channels"] = len(history[-1]["scids"])
                        status["graph_sampled_at"] = history[-1]["sampled_at"]
                    status["elapsed_seconds"] = round(time.monotonic() - started)
                    with (session / "progress.jsonl").open("a") as progress:
                        progress.write(json.dumps(status) + "\n")
                    save(directory / "status.json", status)
                    if status.get("peer_recovery", {}).get("stop_reason"):
                        reason = status["peer_recovery"]["stop_reason"]
                        break
                    if status["readiness"]["ready"]:
                        reason = "readiness reached"
                        break
                    requested.wait(POLL_SECONDS)
                reason = reason or "time budget"
                status["elapsed_seconds"] = round(time.monotonic() - started)
                status["stop_reason"] = reason
                # Every historical sync of the session: evidence of how much each
                # peer still knew that this observer lacked.
                status["sync_coverage"] = recovery.syncs
                phase("sampling")
                # Keep a fresh, coherent RPC view for comparison with the stopped DB.
                # A busy node or a new block can spoil one attempt, so try a few times.
                for attempt in range(1, FINAL_ATTEMPTS + 1):
                    try:
                        final = sample(directory, session / "final-sample.json")
                        final_observation(status, final)
                        (session / "final-rpc-error.json").unlink(missing_ok=True)
                        break
                    except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
                        save(
                            session / "final-rpc-error.json",
                            {"error": str(exc), "attempts": attempt},
                        )
                        if attempt < FINAL_ATTEMPTS:
                            time.sleep(FINAL_RETRY_SECONDS)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            failure = str(exc)
        finally:
            try:
                if child is not None:
                    try:
                        phase("stopping")
                    except OSError as exc:
                        failure = failure or str(exc)
                    clean = stop_child(child)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        result = {
            **status,
            "state": "stopped",
            "stopped_at": utc_now(),
            "session": str(session),
            "stop_reason": reason,
            "clean_shutdown": clean,
            "artifact_verified": False,
        }
        save(session / "shutdown.json", result)
        if failure:
            result.update(state="failed", error=failure)
        elif clean:
            try:
                result.update(
                    freeze_and_analyze(
                        directory,
                        session,
                        samples,
                        final,
                        clean,
                        args.max_data_gb * GIB,
                        on_phase=phase,
                        workers=performance["verification_workers"],
                    )
                )
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                result.update(state="incomplete", error=str(exc))
        else:
            result.update(state="failed", error="shutdown was not clean; no snapshot copied")
        result["updated_at"] = utc_now()
        save(session / "result.json", result)
        save(directory / "status.json", result)
        if emit_result:
            print(json.dumps(result, indent=2), flush=True)
        return 0 if result["state"] in ("verified", "diagnostic") else 2


def positive(value):
    """A value as a positive finite number, which every budget of a session must be."""
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("must be a positive finite number")
    return number
