"""Collector correctness with synthetic exports and a dummy child, never a real node."""

import argparse
import json
import shutil
import signal
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from gossip_snapshot import cli, collector
from gossip_snapshot.analysis import calculate, sha256_file, verify_artifact
from gossip_snapshot.fixture import make_fixture
from gossip_snapshot.schema import LND_COMMIT, load_json


def fake_core(method, *params):
    """Bitcoin Core replies for the fixture's single closed channel (spent)."""
    return {
        "getblockcount": 900000,
        "getblockhash": "22" * 32,
        "getblock": {"tx": ["33" * 32] * 8},
        "gettxout": None,
    }[method]


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "bin").mkdir(parents=True)
    (project / "config").mkdir()
    shutil.copyfile(
        collector.PROJECT / "config/lnd.conf.example", project / "config/lnd.conf.example"
    )
    (project / "uv.lock").write_text("test-lock\n")
    for name in ("lnd", "lncli", "gossip-export"):
        path = project / "bin" / name
        # This is an idle local test child, not Bitcoin software.
        path.write_text(
            f"#!{sys.executable}\nimport signal, time, sys\n"
            "signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n"
            "from pathlib import Path\nPath(__file__).with_suffix('.ready').touch()\n"
            "while True: time.sleep(0.01)\n"
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
    monkeypatch.setattr(collector.core, "preflight", lambda _: {"test_fixture": True})
    monkeypatch.setattr(collector, "core_rpc", lambda _: fake_core)
    directory = tmp_path / "private-observer"
    collector.initialize(directory, project, core_directory=tmp_path / "core")
    return directory, project


def test_init_is_private_fresh_and_reuses_no_existing_wallet(prepared):
    directory, project = prepared
    assert directory.stat().st_mode & 0o077 == 0
    init = collector.verify_directory(directory)
    assert init["fresh_directory"] is True
    sentinel = directory / "lnd/wallet-sentinel"
    sentinel.write_text("preserve")
    with pytest.raises(ValueError, match="already exists"):
        collector.initialize(directory, project)
    assert sentinel.read_text() == "preserve"


def test_build_or_config_drift_cannot_start_validated_collection(prepared):
    directory, project = prepared
    (project / "bin/lnd").write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        collector.check_build(project)
    config = directory / "lnd/lnd.conf"
    original = config.read_text()
    config.write_text(original.replace("nolisten=true", "nolisten=false"))
    with pytest.raises(ValueError, match="configuration changed"):
        collector.verify_directory(directory)
    # Even with a matching hash, the validation settings must be present.
    config.write_text(original.replace("gossip.ban-threshold=0", "gossip.ban-threshold=100"))
    init = load_json(directory / "initialization.json")
    init["config_sha256"] = sha256_file(config)
    collector.save(directory / "initialization.json", init)
    with pytest.raises(ValueError, match="lacks required"):
        collector.verify_directory(directory)


def test_core_backend_validates_never_bans_and_names_its_node(prepared, tmp_path):
    directory, _ = prepared
    lines = (directory / "lnd/lnd.conf").read_text().splitlines()
    init = load_json(directory / "initialization.json")
    assert init["backend"] == "bitcoind" and init["core"]["preflight"] == {"test_fixture": True}
    # No external fee estimator: the observer never opens a channel.
    assert not any(line.startswith("fee.url") for line in lines)
    for line in (
        "bitcoin.node=bitcoind",
        "bitcoind.rpcpolling=true",
        "routing.assumechanvalid=false",
        "gossip.ban-threshold=0",
        f"bitcoind.rpccookie={tmp_path / 'core/data/.cookie'}",
    ):
        assert line in lines
    # A directory cannot be pointed at another node afterwards.
    init["core"]["directory"] = str(tmp_path / "other-core")
    collector.save(directory / "initialization.json", init)
    with pytest.raises(ValueError, match="recorded Bitcoin Core"):
        collector.verify_directory(directory)
    # A directory from before this backend is refused, never resumed.
    del init["backend"]
    collector.save(directory / "initialization.json", init)
    with pytest.raises(ValueError, match="not initialized for the Bitcoin Core backend"):
        collector.verify_directory(directory)


def test_cli_targets_only_the_observer_and_handles_absent_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert "--profile=" not in collector.cli_args(tmp_path, "state")
    (tmp_path / ".lncli").mkdir()
    (tmp_path / ".lncli/profiles.json").write_text("{}")
    command = collector.cli_args(tmp_path, "getinfo")
    assert "--profile=" in command
    assert "--rpcserver=127.0.0.1:11009" in command
    assert any("readonly.macaroon" in part for part in command)
    monkeypatch.setenv("LNCLI_RPCSERVER", "unrelated-node:10009")
    assert "LNCLI_RPCSERVER" not in collector.clean_env()


def test_wallet_does_not_capture_secrets(prepared, monkeypatch):
    directory, _ = prepared
    monkeypatch.setattr(collector, "rpc", lambda *_: {"state": "NON_EXISTING"})
    calls = []

    def interactive(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(collector.subprocess, "run", interactive)
    assert collector.wallet(directory) == 0
    command, kwargs = calls[0]
    assert command[-1] == "create"
    assert "capture_output" not in kwargs and "stdout" not in kwargs and "input" not in kwargs


def test_only_one_supervisor_can_hold_the_directory(prepared):
    directory, _ = prepared
    with collector.supervisor_lock(directory):
        with (
            pytest.raises(ValueError, match="already running"),
            collector.supervisor_lock(directory),
        ):
            pass


def test_forced_shutdown_is_not_clean():
    class StuckChild:
        killed = False

        def poll(self):
            return None

        def send_signal(self, sig):
            assert sig == signal.SIGINT

        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired("dummy", timeout)
            return -9

        def kill(self):
            self.killed = True

    child = StuckChild()
    assert not collector.stop_child(child)
    assert child.killed


@pytest.fixture
def exported(prepared, monkeypatch):
    directory, _ = prepared
    database = directory / "lnd/data/graph/mainnet/channel.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"dummy DB used only by the fake exporter")
    session = directory / "sessions/test"
    session.mkdir()
    original = collector.subprocess.run

    def fake_export(command, **kwargs):
        if command[0] != str(directory / "bin/gossip-export"):
            return original(command, **kwargs)
        capture = Path(command[-1])
        make_fixture(capture)
        # The exporter itself creates neither a manifest nor an audit.
        (capture / "manifest.json").unlink()
        (capture / "closed-audit.json").unlink()
        report = load_json(capture / "export-report.json")
        report["database_sha256"] = sha256_file(database)
        collector.save(capture / "export-report.json", report)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(collector.subprocess, "run", fake_export)
    fixture = directory / "fixture-for-ids"
    make_fixture(fixture)
    ids = [
        json.loads(line)["scid"] for line in (fixture / "records.jsonl").read_text().splitlines()
    ]
    samples = [
        {
            "sampled_at": f"2026-11-10T0{hour}:{minute}:00Z",
            "scids": ids,
            "getinfo": {
                "block_height": 900000,
                "block_hash": "11" * 32,
                "synced_to_chain": True,
                "synced_to_graph": True,
                "num_peers": 3,
                "chains": [{"chain": "bitcoin", "network": "mainnet"}],
            },
        }
        for hour, minute in ((0, "00"), (0, "30"), (1, "00"))
    ]
    final = deepcopy(samples[-1])
    final["sampled_at"] = "2026-11-10T01:01:00Z"
    return directory, session, samples, final


def freeze(data, *, clean=True):
    return collector.freeze_and_analyze(*data, clean=clean, max_data_bytes=30 * collector.GIB)


def test_partial_export_cannot_be_analyzed_as_mainnet(exported):
    directory, session, samples, final = exported
    result = freeze((directory, session, samples[:1], final))
    assert result["state"] == "diagnostic"
    assert result["python_signatures_checked"]
    assert not (session / "capture/manifest.json").exists()
    assert not (session / "analysis").exists()


@pytest.mark.parametrize("problem", ["unclean", "tip_mismatch", "membership_change"])
def test_inconsistent_capture_never_gets_a_manifest(exported, problem):
    directory, session, samples, final = exported
    if problem == "tip_mismatch":
        final["getinfo"]["block_height"] += 1
    if problem == "membership_change":
        final["scids"] = final["scids"] + [str(800000 << 40)]
        # Keep the readiness graph stable, so the final DB-vs-RPC check is exercised.
        for item in samples:
            item["scids"] = final["scids"]
    with pytest.raises(ValueError):
        freeze((directory, session, samples, final), clean=problem != "unclean")
    assert not (session / "capture/manifest.json").exists()
    assert not (session / "analysis").exists()
    if problem == "unclean":
        assert not (session / "channel.db").exists()


def test_ready_session_reaches_existing_independent_artifact_verifier(exported):
    directory, session, samples, final = exported
    result = freeze((directory, session, samples, final))
    assert result["state"] == "verified"
    assert result["artifact_verified"]
    verify_artifact(session / "analysis", session / "capture")
    assert sha256_file(session / "channel.db") == sha256_file(session / "working-channel.db")
    summary = load_json(session / "analysis/summary.json")
    assert summary["included_records"] == 3
    manifest = load_json(session / "capture/manifest.json")
    # The key of earlier collector revisions is accepted when read, never written.
    assert "pilot" not in manifest and "pilot" not in summary
    assert manifest["backend"] == summary["backend"] == "bitcoind"
    assert manifest["captured_at"] == final["sampled_at"]
    assert result["closed_audit"]["entries"] == 1
    assert summary["closed_index"]["audit"]["unspent_at_audit"] == 0
    assert manifest["closed_audit_sha256"] == sha256_file(session / "capture/closed-audit.json")
    # Rehashing a different final RPC reference cannot make it match this DB.
    capture = session / "capture"
    readiness = load_json(capture / "readiness.json")
    readiness[-1]["getinfo"]["block_height"] += 1
    collector.save(capture / "readiness.json", readiness)
    manifest = load_json(capture / "manifest.json")
    manifest["readiness_sha256"] = sha256_file(capture / "readiness.json")
    collector.save(capture / "manifest.json", manifest)
    with pytest.raises(ValueError, match="final readiness reference"):
        calculate(capture)


def test_capture_needs_its_closed_index_audit(exported, monkeypatch):
    directory, session, samples, final = exported

    def unreachable(_init):
        def rpc(*_):
            raise ValueError("Bitcoin Core RPC is unreachable")

        return rpc

    monkeypatch.setattr(collector, "core_rpc", unreachable)
    with pytest.raises(ValueError, match="audit did not complete"):
        freeze((directory, session, samples, final))
    assert not (session / "capture/manifest.json").exists()
    assert not (session / "capture/closed-audit.json").exists()


def test_live_channel_in_closed_index_is_disclosed_not_hidden(exported, monkeypatch):
    directory, session, samples, final = exported
    monkeypatch.setattr(
        collector,
        "core_rpc",
        lambda _: (
            lambda method, *params: (
                {"value": 0.01} if method == "gettxout" else fake_core(method, *params)
            )
        ),
    )
    # A diagnostic export is audited as well.
    result = freeze((directory, session, samples[:1], final))
    assert result["state"] == "diagnostic"
    assert result["closed_audit"]["unspent_at_audit"] == 1
    audit = load_json(session / "capture/closed-audit.json")
    assert audit["unspent_scids"] == [str((700000 << 40) | (7 << 16))]


def test_sampler_rejects_tip_change(tmp_path, monkeypatch):
    infos = iter(
        [
            {"block_height": 900000, "block_hash": "11" * 32},
            {"block_height": 900001, "block_hash": "22" * 32},
        ]
    )

    def replies(_directory, command, *args, **kwargs):
        if command == "getinfo":
            return next(infos)
        return {"edges": []} if command == "describegraph" else {}

    monkeypatch.setattr(collector, "rpc", replies)
    with pytest.raises(ValueError, match="tip changed"):
        collector.sample(tmp_path, tmp_path / "sample.json")
    assert not (tmp_path / "sample.json").exists()


@pytest.mark.parametrize("status_write_fails", [False, True])
def test_short_supervisor_run_stops_its_dummy_child(prepared, monkeypatch, status_write_fails):
    directory, _ = prepared
    original_config = (directory / "lnd/lnd.conf").read_bytes()
    original_initialization = (directory / "initialization.json").read_bytes()
    revisions = iter([{"commit": "a" * 40, "dirty": False}, {"commit": "b" * 40, "dirty": True}])
    monkeypatch.setattr(collector, "collector_revision", lambda: next(revisions))
    monkeypatch.setattr(collector, "check_ports", lambda: None)
    monkeypatch.setattr(collector, "receive_bytes", lambda: 0)
    monkeypatch.setattr(collector, "POLL_SECONDS", 0.02)

    def wait_for_dummy(*_):
        deadline = collector.time.monotonic() + 30
        while not (directory / "bin/lnd.ready").exists():
            assert collector.time.monotonic() < deadline, "dummy child did not start"
            collector.time.sleep(0.01)
        return {"state": "NON_EXISTING"}

    monkeypatch.setattr(collector, "rpc", wait_for_dummy)
    monkeypatch.setattr(collector, "sample", lambda *_: None)
    original_save = collector.save

    def save_unless_stopping(path, value):
        if status_write_fails and path.name == "status.json" and value.get("state") == "stopping":
            raise OSError("simulated full disk while publishing shutdown progress")
        original_save(path, value)

    monkeypatch.setattr(collector, "save", save_unless_stopping)
    args = argparse.Namespace(
        hours=0.00015,
        min_free_gb=0.01,
        max_data_gb=30,
        max_download_gb=10,
        block_cache_gb=2,
        workers=1,
        command="run",
        dir=directory,
    )
    assert collector.run_session(directory, args) == 2  # no graph yet, not a measurement
    status = load_json(directory / "status.json")
    assert status["clean_shutdown"] is True
    assert status["state"] == ("failed" if status_write_fails else "incomplete")
    assert status["stop_reason"] == "time budget"
    assert ("simulated full disk" if status_write_fails else "no regular Bolt graph") in status[
        "error"
    ]
    start = load_json(Path(status["session"]) / "start.json")
    assert (
        start["collector_revision"]
        == status["collector_revision"]
        == {"commit": "a" * 40, "dirty": False}
    )
    for line in (Path(status["session"]) / "progress.jsonl").read_text().splitlines():
        assert json.loads(line)["collector_revision"] == start["collector_revision"]
    assert start["performance"]["block_cache_bytes"] == 2 * collector.GIB
    assert start["performance"]["verification_workers"] == 1
    assert start["peer_recovery_policy"]["target_peers"] == 6
    assert start["peer_recovery_policy"]["parallel_dials"] == 2
    assert f"--blockcachesize={2 * collector.GIB}" in start["lnd_command"]
    assert start["base_config_sha256"] == sha256_file(directory / "lnd/lnd.conf")
    if not status_write_fails:
        # Resume the same node with another cache size; no provenance rewrite.
        (directory / "bin/lnd.ready").unlink()
        args.block_cache_gb = 4
        args.keep_slow_peers = True
        args.debug_sync = True
        args.historical_sync_minutes = 20
        assert collector.run_session(directory, args) == 2
        next_status = load_json(directory / "status.json")
        assert next_status["session"] != status["session"]
        assert next_status["clean_shutdown"]
        next_start = load_json(Path(next_status["session"]) / "start.json")
        assert f"--blockcachesize={4 * collector.GIB}" in next_start["lnd_command"]
        assert "--no-disconnect-on-pong-failure" in next_start["lnd_command"]
        assert next_start["limits"]["keep_slow_peers"] is True
        # LND's hourly default needs no flag; a shorter interval is passed and recorded.
        assert "--historicalsyncinterval=20m" in next_start["lnd_command"]
        assert next_start["limits"]["historical_sync_minutes"] == 20
        assert not any("historicalsyncinterval" in arg for arg in start["lnd_command"])
        assert f"--debuglevel={collector.SYNC_DEBUG_LEVEL}" in next_start["lnd_command"]
        assert next_start["logging"]["debug_sync"] is True
        assert not any(arg.startswith("--debuglevel=") for arg in start["lnd_command"])
        assert next_status["collector_revision"] == {"commit": "b" * 40, "dirty": True}
        assert (
            load_json(Path(status["session"]) / "start.json")["collector_revision"]
            == start["collector_revision"]
        )
    assert (directory / "lnd/lnd.conf").read_bytes() == original_config
    assert (directory / "initialization.json").read_bytes() == original_initialization
    collector.verify_directory(directory)


def test_stop_requests_only_target_a_live_owner(prepared):
    directory, _ = prepared
    assert not collector.request_stop(directory)
    assert not (directory / "stop.request").exists()
    with collector.supervisor_lock(directory):
        assert collector.request_stop(directory)
        assert (directory / "stop.request").stat().st_mode & 0o077 == 0


def test_graph_watchdog_stops_cleanly_and_exports_only_diagnostics(exported, monkeypatch):
    from gossip_snapshot.peers import PeerRecovery

    directory, _, samples, _ = exported
    info = deepcopy(samples[0]["getinfo"])
    info.update(
        synced_to_graph=False,
        num_peers=1,
        identity_pubkey="02" + "11" * 32,
        num_active_channels=0,
        num_inactive_channels=0,
        num_pending_channels=0,
    )
    times = iter([0, 0, 2701])
    monkeypatch.setattr(
        collector,
        "PeerRecovery",
        lambda *a, **kw: PeerRecovery(*a, **kw, clock=lambda: next(times)),
    )
    monkeypatch.setattr(collector, "check_ports", lambda: None)
    monkeypatch.setattr(collector, "receive_bytes", lambda: 0)
    monkeypatch.setattr(collector, "POLL_SECONDS", 0.02)

    def replies(_directory, command, *args, **kwargs):
        if command == "state":
            deadline = collector.time.monotonic() + 30
            while not (directory / "bin/lnd.ready").exists():
                assert collector.time.monotonic() < deadline
                collector.time.sleep(0.01)
            return {"state": "SERVER_ACTIVE"}
        return {
            "getinfo": info,
            "listpeers": {"peers": [{"pub_key": "03" + "22" * 32}]},
            "describegraph": {"nodes": []},
        }[command]

    def snapshot(_directory, destination):
        value = {**samples[0], "getinfo": info}
        collector.save(destination, value)
        return value

    monkeypatch.setattr(collector, "rpc", replies)
    monkeypatch.setattr(collector, "sample", snapshot)
    args = argparse.Namespace(
        hours=1, min_free_gb=0.01, max_data_gb=30, max_download_gb=10, workers=1
    )
    assert collector.run_session(directory, args) == 0
    result = load_json(directory / "status.json")
    assert result["stop_reason"] == "graph stalled despite peer recovery"
    assert result["sync_coverage"] == []  # The dummy node logged no historical sync.
    assert result["clean_shutdown"] and result["python_signatures_checked"]
    assert result["state"] == "diagnostic" and not result["artifact_verified"]
    assert result["elapsed_seconds"] < 30
    assert not (Path(result["capture"]) / "manifest.json").exists()


def test_failed_rpc_poll_keeps_earlier_readiness_samples(exported, monkeypatch):
    directory, _, samples, _ = exported
    info = {
        **samples[0]["getinfo"],
        "identity_pubkey": "02" + "11" * 32,
        "num_active_channels": 0,
        "num_inactive_channels": 0,
        "num_pending_channels": 0,
    }
    monkeypatch.setattr(collector, "check_ports", lambda: None)
    monkeypatch.setattr(collector, "receive_bytes", lambda: 0)
    monkeypatch.setattr(collector, "POLL_SECONDS", 0.02)
    polls = 0

    def replies(_directory, command, *args, **kwargs):
        nonlocal polls
        if command == "state":
            deadline = collector.time.monotonic() + 30
            while not (directory / "bin/lnd.ready").exists():
                assert collector.time.monotonic() < deadline
                collector.time.sleep(0.01)
            return {"state": "SERVER_ACTIVE"}
        if command == "getinfo":
            polls += 1
            if polls == 3:
                raise ValueError("simulated RPC timeout")
        return {
            "getinfo": info,
            "listpeers": {"peers": [{"pub_key": "03" + "22" * 32}]},
            "describegraph": {"nodes": []},
        }[command]

    def snapshot(_directory, destination):
        collector.save(destination, samples[0])
        return samples[0]

    monkeypatch.setattr(collector, "rpc", replies)
    monkeypatch.setattr(collector, "sample", snapshot)
    args = argparse.Namespace(
        hours=0.0003, min_free_gb=0.01, max_data_gb=30, max_download_gb=10, workers=1
    )
    assert collector.run_session(directory, args) == 0
    session = Path(load_json(directory / "status.json")["session"])
    progress = [json.loads(line) for line in (session / "progress.jsonl").read_text().splitlines()]
    failed = [item for item in progress if "last_rpc_error" in item]
    assert failed and all(item["readiness_samples"] == 1 for item in failed)
    assert load_json(session / "start.json")["core_preflight"] == {"test_fixture": True}


def test_wallet_state_is_shown_before_the_first_slow_answer(exported, monkeypatch):
    directory, _, samples, _ = exported
    info = {
        **samples[0]["getinfo"],
        "identity_pubkey": "02" + "11" * 32,
        "num_active_channels": 0,
        "num_inactive_channels": 0,
        "num_pending_channels": 0,
    }
    monkeypatch.setattr(collector, "check_ports", lambda: None)
    monkeypatch.setattr(collector, "receive_bytes", lambda: 0)
    monkeypatch.setattr(collector, "POLL_SECONDS", 0.02)
    shown = []

    def replies(_directory, command, *args, **kwargs):
        if command == "state":
            deadline = collector.time.monotonic() + 30
            while not (directory / "bin/lnd.ready").exists():
                assert collector.time.monotonic() < deadline
                collector.time.sleep(0.01)
            return {"state": "SERVER_ACTIVE"}
        if command == "getinfo" and not shown:
            # What a dashboard reads while the first answer is still pending.
            shown.append(load_json(directory / "status.json"))
        return {
            "getinfo": info,
            "listpeers": {"peers": [{"pub_key": "03" + "22" * 32}]},
            "describegraph": {"nodes": []},
        }[command]

    def snapshot(_directory, destination):
        collector.save(destination, samples[0])
        return samples[0]

    monkeypatch.setattr(collector, "rpc", replies)
    monkeypatch.setattr(collector, "sample", snapshot)
    args = argparse.Namespace(
        hours=0.0003, min_free_gb=0.01, max_data_gb=30, max_download_gb=10, workers=1
    )
    assert collector.run_session(directory, args) == 0
    assert shown[0]["state"] == "running" and shown[0]["wallet_state"] == "SERVER_ACTIVE"
    assert "node" not in shown[0]


def test_real_node_start_is_rejected_outside_linux(monkeypatch, capsys):
    monkeypatch.setattr(collector.platform, "system", lambda: "Darwin")
    assert cli.main(["collect", "run"]) == 2
    assert "on Linux" in capsys.readouterr().err


def test_budgets_are_explicit_and_nonfinite_durations_fail():
    args = SimpleNamespace(min_free_gb=10, max_data_gb=30, max_download_gb=10)
    resources = {
        "free_bytes": 100 * collector.GIB,
        "observer_disk_bytes": 0,
        "host_received_bytes": 0,
    }
    assert collector.resource_limit(resources, args) is None
    resources["host_received_bytes"] = 10 * collector.GIB
    assert collector.resource_limit(resources, args) == "Host download budget"
    for value in ("nan", "inf", "-1", "0"):
        with pytest.raises(ValueError):
            collector.positive(value)


def test_download_guard_saves_triggering_measurement_and_final_sample(exported, monkeypatch):
    directory, _, _, final = exported
    final["getinfo"].update(synced_to_graph=False, num_peers=4)
    monkeypatch.setattr(collector, "check_ports", lambda: None)
    monkeypatch.setattr(collector, "receive_bytes", lambda: 0)
    calls = 0

    def measured(*_):
        nonlocal calls
        calls += 1
        if calls > 1:
            deadline = collector.time.monotonic() + 30
            while not (directory / "bin/lnd.ready").exists():
                assert collector.time.monotonic() < deadline
                collector.time.sleep(0.01)
        return {
            "free_bytes": 100 * collector.GIB,
            "observer_disk_bytes": 1,
            "host_received_bytes": 0 if calls == 1 else 11 * collector.GIB,
        }

    monkeypatch.setattr(collector, "resources", measured)
    monkeypatch.setattr(collector, "sample", lambda *_: final)
    args = argparse.Namespace(
        hours=1,
        min_free_gb=0.01,
        max_data_gb=30,
        max_download_gb=10,
        workers=1,
        keep_slow_peers=True,
    )
    assert collector.run_session(directory, args) == 0
    result = load_json(directory / "status.json")
    assert result["clean_shutdown"]
    assert result["stop_reason"] == "Host download budget"
    assert result["resources"]["host_received_bytes"] == 11 * collector.GIB
    assert result["node"]["num_peers"] == 4
    assert result["graph_channels"] == len(final["scids"])
    assert result["final_sampled_at"] == final["sampled_at"]
    assert result["state"] == "diagnostic" and not result["artifact_verified"]
    assert not (Path(result["capture"]) / "manifest.json").exists()


@pytest.mark.parametrize("failures", [2, 3])
def test_final_sample_is_retried_and_its_absence_is_reported_as_such(
    exported, monkeypatch, failures
):
    """One getinfo timeout at the end must not cost a session its final sample."""
    directory, _, _, final = exported
    final["getinfo"].update(synced_to_graph=False)
    monkeypatch.setattr(collector, "check_ports", lambda: None)
    monkeypatch.setattr(collector, "receive_bytes", lambda: 0)
    monkeypatch.setattr(collector, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(collector, "FINAL_RETRY_SECONDS", 0)
    attempts = 0

    def wait_for_dummy(*_, **__):
        deadline = collector.time.monotonic() + 30
        while not (directory / "bin/lnd.ready").exists():
            assert collector.time.monotonic() < deadline
            collector.time.sleep(0.01)
        return {"state": "NON_EXISTING"}

    def busy_node(*_):
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise subprocess.TimeoutExpired(["lncli", "getinfo"], collector.INFO_SECONDS)
        return final

    monkeypatch.setattr(collector, "rpc", wait_for_dummy)
    monkeypatch.setattr(collector, "sample", busy_node)
    args = argparse.Namespace(
        hours=0.0002, min_free_gb=0.01, max_data_gb=30, max_download_gb=10, workers=1
    )
    assert collector.run_session(directory, args) == 0
    result = load_json(directory / "status.json")
    error = Path(result["session"]) / "final-rpc-error.json"
    assert result["state"] == "diagnostic" and result["clean_shutdown"]
    assert attempts == 3 == collector.FINAL_ATTEMPTS
    if failures == 2:
        assert result["final_sampled_at"] == final["sampled_at"] and not error.exists()
        assert collector.NO_FINAL_SAMPLE not in result["readiness"]["reasons"]
    else:
        assert load_json(error)["attempts"] == 3 and "final_sampled_at" not in result
        assert result["readiness"]["reasons"] == sorted(
            ["need at least three samples", collector.NO_FINAL_SAMPLE]
        )


def test_missing_final_sample_reports_what_the_saved_samples_show(exported):
    """Without a final sample, the reasons are those of the saved observations."""
    directory, session, samples, _ = exported
    samples[0]["getinfo"]["num_peers"] = 0
    result = freeze((directory, session, samples, None))
    assert result["state"] == "diagnostic"
    assert result["readiness"] == {
        "ready": False,
        "reasons": ["fewer than three connected peers", collector.NO_FINAL_SAMPLE],
        "churn": [0.0, 0.0],
    }
    report = load_json(session / "capture/export-report.json")
    newest = max(int(scid) >> 40 for scid in samples[0]["scids"])
    assert result["newest_funding_lag_blocks"] == report["graph_prune_tip"]["height"] - newest
    assert not (session / "capture/manifest.json").exists()


@pytest.mark.parametrize("changes, dirty", [("", False), (" M src/file.py\n?? new.py\n", True)])
def test_revision_records_commit_and_repository_changes_without_writing_git(
    tmp_path, monkeypatch, changes, dirty
):
    replies = iter(["a" * 40 + "\n", changes])

    def git(command, **kwargs):
        assert command[:4] == ["git", "--no-optional-locks", "-C", str(tmp_path)]
        assert kwargs["timeout"] == 3
        return SimpleNamespace(stdout=next(replies))

    monkeypatch.setattr(collector.subprocess, "run", git)
    assert collector.collector_revision(tmp_path) == {"commit": "a" * 40, "dirty": dirty}


def test_missing_git_never_claims_a_clean_checkout(tmp_path, monkeypatch):
    def absent(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(collector.subprocess, "run", absent)
    assert collector.collector_revision(tmp_path) == {"commit": None, "dirty": None}
