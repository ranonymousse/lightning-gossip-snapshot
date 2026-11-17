"""Dashboard, CLI isolation and lifecycle checks; no real node or network."""

import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from gossip_snapshot import cli, collector, dashboard

runner = CliRunner()


def printed(status, directory, **kwargs):
    stream = io.StringIO()
    console = Console(file=stream, width=80, color_system=None)
    console.print(dashboard.render(status, directory, **kwargs))
    return stream.getvalue()


def test_recovery_dashboard_shows_stalls_and_actual_stop_reason(tmp_path, demo_status):
    status = demo_status
    status["node"]["synced_to_graph"] = False
    status.pop("graph_channels", None)  # No sampled graph in this scenario.
    status["peer_recovery"] = {
        "target": 6,
        "processed_channels": 1300,
        "historical_syncs": 20,
        "sync_advertised": 50000,
        "sync_new": 30000,
        "sync_queried": 500,
        "stalled_seconds": 1300,
        "attempts": 8,
        "connections": 5,
        "rotations": 1,
        "active_syncers": 0,
        "rejected_invalid": 57,
        "rejected_outdated": 0,
        "ban_disconnects": 0,
        "warning": "Graph stalled; recovering peer connections",
    }
    text = " ".join(printed(status, tmp_path).split())
    assert "Graph stalled" in text
    assert "Maintaining 6" in text
    assert "First sample pending" in text  # No count without a sample or an export.
    assert "Sync 20: peer lists 50,000 channels · 30,000 new here · 500 requested" in text
    assert "Channels added this session: 1,300" in text
    assert "Rejected: 57 failed validation · 0 known closed · 0 peers banned" in text
    assert "No graph progress for 21 minutes" in text
    status["peer_recovery"].update(sync_advertised=None, sync_new=None, sync_queried=0)
    assert "waiting for the peer's channel list" in " ".join(printed(status, tmp_path).split())
    status.update(state="diagnostic", stop_reason="graph stalled despite peer recovery")
    assert "Stopped: graph stalled despite peer recovery" in printed(status, tmp_path)


def test_peer_policy_options_are_validated_and_preserved():
    settings = cli.limits(
        4, 10, 200, 200, target_peers=8, stall_minutes=60, historical_sync_minutes=20
    )
    assert settings.target_peers == 8 and settings.stall_minutes == 60
    assert settings.historical_sync_minutes == 20
    assert cli.limits(1, 10, 30, 10).historical_sync_minutes == 60  # LND's own default.
    for bad in [0, 9, 61, 20.5]:
        with pytest.raises(ValueError, match="historical sync interval"):
            cli.limits(1, 10, 30, 10, historical_sync_minutes=bad)
    for bad in [0, 2, 13]:
        with pytest.raises(ValueError, match="peer target"):
            cli.limits(1, 10, 30, 10, target_peers=bad)
    for bad in [0, 29, 181]:
        with pytest.raises(ValueError, match="stall timeout"):
            cli.limits(1, 10, 30, 10, stall_minutes=bad)


def test_missing_status_and_stale_supervisor_do_not_claim_collection(tmp_path):
    assert dashboard.read_status(tmp_path)["state"] == "not_initialized"
    assert not list(tmp_path.iterdir())
    collector.save(tmp_path / "status.json", {"state": "running", "wallet_state": "LOCKED"})
    text = printed(dashboard.read_status(tmp_path), tmp_path)
    assert "Collector is not running" in text
    assert "collection continues" not in text
    assert not (tmp_path / "supervisor.lock").exists()


def test_unlocked_wallet_without_a_node_answer_is_not_called_chain_sync(tmp_path):
    waiting = {"state": "running", "wallet_state": "SERVER_ACTIVE", "collector_running": True}
    text = printed(waiting, tmp_path)
    assert "Waiting for LND to answer" in text
    assert "Wallet action needed" not in text and "Synchronizing Bitcoin" not in text


def test_unreadable_status_is_reported_as_such(tmp_path):
    (tmp_path / "status.json").write_text("[]")
    assert dashboard.read_status(tmp_path)["state"] == "unreadable"


@pytest.mark.parametrize("state", ["verified", "diagnostic", "incomplete", "failed"])
def test_finished_views_only_offer_figures_for_verified_artifacts(tmp_path, state, demo_status):
    status = demo_status
    status.update(
        state=state,
        collector_running=False,
        artifact_verified=state == "verified",
        analysis="private/analysis",
        error="example diagnostic",
    )
    text = printed(status, tmp_path)
    assert ("Exact analysis, private" in text) == (state == "verified")
    assert "collection continues" not in text


def test_readonly_dashboard_does_not_render_peer_identities_or_raw_keys(tmp_path, demo_status):
    status = demo_status
    status["raw_keys"] = "PRIVATE_RAW_KEY_SENTINEL"
    status["node"]["identity_pubkey"] = "IDENTITY_SENTINEL"
    status["last_rpc_error"] = "[bold]literal[/bold]\x1b[2J"
    text = printed(status, tmp_path)
    assert "PRIVATE_RAW_KEY_SENTINEL" not in text
    assert "IDENTITY_SENTINEL" not in text
    assert "[bold]literal[/bold]" in text
    assert "\x1b" not in text
    assert all(len(line) <= 80 for line in text.splitlines())


def test_watch_ctrl_c_only_detaches(tmp_path, monkeypatch, demo_status):
    monkeypatch.setattr(dashboard, "read_status", lambda _: demo_status)

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(dashboard.time, "sleep", interrupt)
    stream = io.StringIO()
    dashboard.watch(tmp_path, Console(file=stream, force_terminal=True, width=100))
    assert "Dashboard detached" in stream.getvalue()
    assert not list(tmp_path.iterdir())


def test_redirected_status_is_json(tmp_path):
    expected = {"state": "running", "elapsed_seconds": 12}
    collector.save(tmp_path / "status.json", expected)
    for extra in ([], ["--json"]):
        result = runner.invoke(cli.app, ["collect", "--dir", str(tmp_path), "status", *extra])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == expected
        assert "\x1b" not in result.stdout


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_bad_time_budget_is_rejected_before_startup(value, monkeypatch):
    def forbidden(*args):
        pytest.fail("invalid budget reached startup")

    monkeypatch.setattr(cli, "require_linux", forbidden)
    result = runner.invoke(cli.app, ["collect", "start", "--hours", value])
    assert result.exit_code == 2
    assert "positive finite" in result.output


def test_start_requires_linux_and_interactive_wallet_terminal(tmp_path, monkeypatch):
    directory = tmp_path / "not-created"
    args = ["collect", "--dir", str(directory), "start"]
    monkeypatch.setattr(collector.platform, "system", lambda: "Darwin")
    assert "on Linux" in runner.invoke(cli.app, args).output
    monkeypatch.setattr(collector.platform, "system", lambda: "Linux")
    assert "interactive terminal" in runner.invoke(cli.app, args).output
    assert not directory.exists()


@pytest.mark.parametrize("existing", [False, True])
def test_start_preserves_existing_observer_and_guides_wallet_then_watch(
    tmp_path, monkeypatch, existing
):
    directory = tmp_path / "observer"
    events = []
    if existing:
        directory.mkdir()
    monkeypatch.setattr(cli, "require_linux", lambda: None)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        collector,
        "initialize",
        lambda path, **options: (
            events.append(("init", path)) if "core_directory" in options else None
        ),
    )
    monkeypatch.setattr(collector, "verify_directory", lambda path: events.append(("verify", path)))

    def launch(path, args):
        events.append(("launch", path))
        assert args.hours == cli.DEFAULT_HOURS == 4
        assert args.max_download_gb == 10
        assert args.block_cache_gb == 2 and args.keep_slow_peers is True
        assert args.workers == 0
        return None if existing else object()

    def wallet(path):
        events.append(("wallet", path))
        return 0

    monkeypatch.setattr(cli, "launch", launch)
    monkeypatch.setattr(collector, "wallet", wallet)
    monkeypatch.setattr(dashboard, "watch", lambda path, _: events.append(("watch", path)))
    cli.start_command(SimpleNamespace(obj=directory))
    assert events == [
        (event, directory)
        for event in ("verify" if existing else "init", "launch", "wallet", "watch")
    ]


@pytest.mark.parametrize("keep_slow_peers", [False, True])
def test_launcher_detaches_only_one_dummy_worker_using_the_project_python(
    tmp_path, monkeypatch, keep_slow_peers
):
    """Exercise real process/lock behavior while substituting an idle Python child."""
    original_popen = subprocess.Popen
    calls = []
    script = (
        "import fcntl, signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n"
        "lock = open(sys.argv[1], 'a')\n"
        "fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "while True: time.sleep(0.02)\n"
    )

    def dummy(command, **kwargs):
        calls.append((command, kwargs))
        return original_popen(
            [sys.executable, "-c", script, str(tmp_path / "supervisor.lock")], **kwargs
        )

    monkeypatch.setenv("LNCLI_RPCSERVER", "unrelated-node")
    before = dict(os.environ)
    monkeypatch.setattr(cli.subprocess, "Popen", dummy)
    args = cli.limits(1, 10, 30, 10, keep_slow_peers=keep_slow_peers, debug_sync=True)
    child = cli.launch(tmp_path, args)
    try:
        assert child.poll() is None
        assert cli.launch(tmp_path, cli.limits(4, 10, 30, 10)) is None
        assert len(calls) == 1
        command, kwargs = calls[0]
        assert command[:3] == [sys.executable, "-m", "gossip_snapshot.cli"]
        assert command[command.index("--hours") + 1] == "1.0"
        assert command[command.index("--block-cache-gb") + 1] == "2"
        assert command[command.index("--workers") + 1] == "0"
        assert command[command.index("--historical-sync-minutes") + 1] == "60"
        assert ("--keep-slow-peers" in command) == keep_slow_peers
        assert ("--disconnect-slow-peers" in command) != keep_slow_peers
        assert "--debug-sync" in command
        assert "True" not in command and "False" not in command
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"].name == str(tmp_path / "supervisor.log")
        assert "LNCLI_RPCSERVER" not in kwargs["env"]
        assert dict(os.environ) == before
        assert collector.supervisor_running(tmp_path)
    finally:
        assert collector.stop_child(child)
    assert not collector.supervisor_running(tmp_path)


def test_launcher_command_parses_as_a_registered_cli_command(tmp_path, monkeypatch):
    """The supervisor starts through the CLI, so the group and options it names must exist."""
    commands = []
    running = iter([False, True] * 2)
    monkeypatch.setattr(collector, "supervisor_running", lambda _: next(running))
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda command, **_: commands.append(command) or object()
    )
    for keep_slow_peers in (False, True):
        args = cli.limits(1, 10, 30, 10, keep_slow_peers=keep_slow_peers, debug_sync=True)
        cli.launch(tmp_path, args)
    assert len(commands) == 2
    for command in commands:
        assert command[:3] == [sys.executable, "-m", "gossip_snapshot.cli"]
        assert cli.main([*command[3:], "--help"]) == 0


def test_cli_restores_process_umask_and_environment(tmp_path):
    before = dict(os.environ)
    previous = os.umask(0o027)
    try:
        assert cli.main(["fixture", "--output", str(tmp_path / "capture")]) == 0
        observed = os.umask(0o027)
        assert observed == 0o027
        assert dict(os.environ) == before
    finally:
        os.umask(previous)


def test_dashboard_labels_cpu_memory_and_network_scope(tmp_path, demo_status):
    text = printed(demo_status, tmp_path)
    for expected in (
        "LND CPU",
        "100% = 1 core",
        "265%",
        "LND RAM",
        "3.20 GiB",
        "Host download",
        "6.40 MiB/s",
        "LND disk I/O",
        "Block cache",
        "4 GiB",
    ):
        assert expected in text


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "65"])
def test_bad_cache_is_rejected_before_startup(value, monkeypatch):
    def forbidden():
        pytest.fail("invalid cache reached startup")

    monkeypatch.setattr(cli, "require_linux", forbidden)
    result = runner.invoke(cli.app, ["collect", "start", "--block-cache-gb", value])
    assert result.exit_code == 2
    assert "block cache" in result.output


def test_finished_view_uses_export_and_final_rpc_instead_of_recovery_cache(tmp_path, demo_status):
    status = demo_status
    status.update(
        state="diagnostic",
        collector_running=False,
        artifact_verified=False,
        python_signatures_checked=True,
        exported_records=7000,
        graph_channels=7000,
        final_sampled_at="2026-11-10T00:00:00Z",
        newest_funding_lag_blocks=4300,
        peer_recovery={"target": 6, "connected": 5, "processed_channels": 5500},
    )
    status["node"]["num_peers"] = 4
    text = printed(status, tmp_path)
    assert "7,000" in text and "final database export" in text
    assert "5,500" not in text
    assert "Newest funding output: 4,300 blocks below the tip" in text
    assert "4 / 3 minimum" in text
    assert "FINAL SAMPLE" in text
    assert "Collection stopped" in text and "Maintaining" not in text
    assert "Exact analysis, private" not in text


def test_graph_panel_shows_only_exact_sample_counts(tmp_path, demo_status):
    """A newer recovery check never replaces the sampled count."""
    status = demo_status
    status.update(graph_channels=6600, graph_sampled_at="2026-11-09T23:50:00Z")
    status["peer_recovery"] = {
        "target": 6,
        "graph_channels": 5500,  # Not a sampled count, so the panel must ignore it.
        "graph_checked_at": "2026-11-09T23:52:00Z",
        "pong_failures": 12,
        "pong_disconnects": 2,
    }
    text = printed(status, tmp_path)
    assert "6,600" in text and "last graph sample" in text and "5,500" not in text
    assert "Pong failures: 12" in text and "2 disconnected locally" in text


@pytest.mark.parametrize(
    "dirty, label",
    [(False, "clean at start"), (True, "dirty at start"), (None, "worktree state unknown")],
)
def test_dashboard_uses_recorded_commit_without_reading_the_current_checkout(
    tmp_path, monkeypatch, dirty, label
):
    def forbidden(*args, **kwargs):
        pytest.fail("watching must not substitute the current checkout for the collector revision")

    monkeypatch.setattr(collector, "collector_revision", forbidden)
    revision = {"commit": "123456789abc" + "0" * 28, "dirty": dirty}
    collector.save(
        tmp_path / "status.json",
        {
            "state": "diagnostic",
            "session": str(tmp_path),
            "limits": {"hours": 8},
            "collector_revision": revision,
        },
    )
    status = dashboard.read_status(tmp_path)
    assert status["collector_revision"] == revision
    text = printed(status, tmp_path)
    assert "123456789abc" in text and label in text
    assert "Collector commit" in text
