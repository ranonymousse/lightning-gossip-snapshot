"""Rich views over saved collector state. Watching never starts or stops a node."""

import re
import shlex
import time
from datetime import UTC, datetime

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .common import COMMIT_ID, GIB, MIB
from .readiness import MINIMUM_INTERVAL_SECONDS, MINIMUM_PEERS
from .schema import load_json

FINISHED = {"verified", "diagnostic", "incomplete", "failed", "stopped"}
ACTIVE = {
    "starting",
    "running",
    "sampling",
    "stopping",
    "freezing",
    "exporting",
    "auditing",
    "analyzing",
    "verifying",
}
LIVE_REFRESH_PER_SECOND = 12
WATCH_SECONDS = 0.25  # How often `watch` looks whether the session has ended.
# A week of blocks. A synchronization fetches channels oldest first and new channels
# appear within hours, so a newest funding output further below the tip than this marks
# a synchronization that stopped early.
FUNDING_LAG_BLOCKS = 1008
BUDGET_WARNING = 0.8  # Share of a budget from which its bar turns yellow.
RPC_ERROR_TAIL = 300  # Characters of the last RPC error that the footer shows.


def safe(value):
    """Text without control characters, so that a saved or logged string cannot steer a terminal."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value))


def duration(seconds):
    """Seconds as HH:MM:SS."""
    seconds = max(0, int(seconds))
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours:02}:{minutes:02}:{seconds % 60:02}"


def age(timestamp, now):
    """Seconds since an ISO timestamp, or None when it is missing or unreadable."""
    if not timestamp:
        return None
    try:
        return max(
            0, (now - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds()
        )
    except (ValueError, TypeError):
        return None


def read_status(directory):
    """The saved status of an observer directory, and whether a supervisor runs there."""
    from .collector import supervisor_running

    try:
        status = load_json(directory / "status.json")
        if not isinstance(status, dict):
            raise ValueError("status must be an object")
    except FileNotFoundError:
        status = {"state": "not_initialized"}
    except ValueError:
        status = {"state": "unreadable", "error": "Saved status could not be read."}
    status["collector_running"] = supervisor_running(directory)
    return status


def headline(status):
    """What a session is doing, in a few words, and the colour that shows it."""
    state = status.get("state", "unknown")
    if state in ACTIVE and not status.get("collector_running", True):
        return "Collector is not running", "red"
    if state == "running":
        wallet = status.get("wallet_state")
        if wallet == "NON_EXISTING":
            return "Create the observer wallet", "yellow"
        if wallet == "LOCKED":
            return "Unlock the observer wallet", "yellow"
        node = status.get("node", {})
        if not node:
            return "Waiting for LND to answer", "cyan"
        if not node.get("synced_to_chain"):
            return "Synchronizing Bitcoin", "cyan"
        if warning := status.get("peer_recovery", {}).get("warning"):
            return safe(warning), "yellow"
        if not node.get("synced_to_graph"):
            return "Synchronizing the public graph", "cyan"
        return "Observing graph stability", "cyan"
    return {
        "not_initialized": ("Ready to prepare an observer directory", "cyan"),
        "initialized": ("Observer directory prepared", "cyan"),
        "starting": ("Starting the collector", "cyan"),
        "sampling": ("Taking the final observation", "cyan"),
        "stopping": ("Stopping LND cleanly", "yellow"),
        "freezing": ("Freezing the graph snapshot", "cyan"),
        "exporting": ("Exporting signed announcements", "cyan"),
        "auditing": ("Checking closed channels against Bitcoin Core", "cyan"),
        "analyzing": ("Building the snapshot result", "cyan"),
        "verifying": ("Verifying the snapshot", "cyan"),
        "verified": ("Snapshot verified", "green"),
        "diagnostic": ("Diagnostic export saved", "yellow"),
        "incomplete": ("Session incomplete · sync progress preserved", "yellow"),
        "failed": ("Session needs attention", "red"),
        "stopped": ("Collector stopped", "yellow"),
    }.get(state, ("Status unavailable", "yellow"))


def live_view(status_provider, directory, **options):
    """Keep animation time across screen rebuilds, independently of status updates."""
    spinner = Spinner("dots")
    return lambda: render(status_provider(), directory, spinner=spinner, **options)


def revision_label(status):
    """The collector commit that the session recorded at its start, never the current checkout."""
    revision = status.get("collector_revision") or {}
    line = Text("Collector commit  ", style="dim")
    commit = revision.get("commit")
    if not isinstance(commit, str) or not COMMIT_ID.fullmatch(commit):
        line.append("not recorded", style="yellow")
        return line
    line.append(commit[:12], style="cyan")
    dirty = revision.get("dirty")
    if dirty is True:
        line.append(" · dirty at start", style="yellow")
    elif dirty is False:
        line.append(" · clean at start", style="dim")
    else:
        line.append(" · worktree state unknown", style="yellow")
    return line


def title_line(status):
    """The panel's title, marked while LND logs its synchronization in detail."""
    heading = Text("GOSSIP SNAPSHOT", style="bold white")
    heading.append("    BITCOIN MAINNET · COLLECTOR", style="dim")
    if status.get("logging", {}).get("debug_sync"):
        heading.append(" · SYNC DEBUG", style="yellow")
    return heading


def state_line(status, spinner=None):
    """The headline, behind a spinner while a supervisor works on the session."""
    title, color = headline(status)
    if status.get("state", "unknown") in ACTIVE and status.get("collector_running", True):
        indicator = spinner if spinner is not None else Spinner("dots")
        indicator.update(text=Text(title, style=color), style=color)
        return indicator
    return Text(title, style=f"bold {color}")


def session_window(status, now):
    """How much of the session's time ceiling has passed."""
    elapsed = status.get("elapsed_seconds", 0)
    if status.get("state", "unknown") == "running" and status.get("collector_running", True):
        elapsed = max(elapsed, age(status.get("started_at"), now) or 0)
    total = status.get("limits", {}).get("hours", 0) * 3600
    timer = Table.grid(expand=True, padding=(0, 1))
    timer.add_column(width=15)
    timer.add_column(ratio=1)
    timer.add_column(justify="right", width=23)
    if total:
        timer.add_row(
            "Session window",
            ProgressBar(
                total=total,
                completed=min(elapsed, total),
                complete_style="cyan",
                finished_style="cyan",
            ),
            f"{duration(elapsed)} / {duration(total)}",
        )
    else:
        timer.add_row("Session window", Text("Waiting to start", style="dim"), "")
    return timer


def sync_metrics(status):
    """Three columns: the chain, the gossip graph and the connected peers."""
    node, recovery = status.get("node", {}), status.get("peer_recovery", {})
    finished = status.get("state") in FINISHED
    metrics = Table.grid(expand=True, padding=(0, 2))
    metrics.add_column(ratio=1)
    metrics.add_column(ratio=1)
    metrics.add_column(ratio=1)
    peer_count = (
        node.get("num_peers", 0)
        if finished
        else recovery.get("connected", node.get("num_peers", 0))
    )
    # Exact counts only: a saved graph sample or the stopped database.
    if finished and status.get("python_signatures_checked") and "exported_records" in status:
        graph_text = f"{status['exported_records']:,} announcements\nfinal database export"
    elif "graph_channels" in status:
        graph_text = f"{status['graph_channels']:,} announcements\nlast graph sample"
    else:
        graph_text = "First sample pending"
    metrics.add_row(
        Text("CHAIN", style="dim"),
        Text("GOSSIP GRAPH", style="dim"),
        Text(
            "PEERS AT FINAL SAMPLE"
            if finished and status.get("final_sampled_at")
            else ("PEERS AT LAST CHECK" if finished else "CONNECTED PEERS"),
            style="dim",
        ),
    )
    metrics.add_row(
        Text(
            "Synced" if node.get("synced_to_chain") else "Syncing / waiting",
            style="green" if node.get("synced_to_chain") else "cyan",
        ),
        Text(
            "Synced" if node.get("synced_to_graph") else "Syncing / waiting",
            style="green" if node.get("synced_to_graph") else "cyan",
        ),
        Text(
            f"{peer_count} / {MINIMUM_PEERS} minimum",
            style="green" if peer_count >= MINIMUM_PEERS else "yellow",
        ),
    )
    metrics.add_row(
        f"Height {node['block_height']:,}" if node.get("block_height") else "Height pending",
        graph_text,
        "Collection stopped"
        if finished
        else (
            f"Maintaining {recovery['target']} connections" if recovery else "Minimum for readiness"
        ),
    )
    return metrics


def resource_table(status):
    """The session's budgets, the free disk and what LND takes of the host."""
    measured, limits = status.get("resources", {}), status.get("limits", {})
    resources = Table.grid(expand=True, padding=(0, 1))
    resources.add_column(width=15)
    resources.add_column(ratio=1)
    resources.add_column(width=23, justify="right")
    for label, field, limit in (
        ("Host received", "host_received_bytes", "max_download_gb"),
        ("Observer disk", "observer_disk_bytes", "max_data_gb"),
    ):
        value, budget = measured.get(field, 0) / GIB, limits.get(limit, 0)
        style = "yellow" if budget and value >= budget * BUDGET_WARNING else "cyan"
        resources.add_row(
            label,
            ProgressBar(
                total=budget or 1,
                completed=min(value, budget or 1),
                complete_style=style,
                finished_style="yellow",
            ),
            f"{value:.2f} / {budget:g} GiB" if budget else "Waiting for limits",
        )
    free = measured.get("free_bytes")
    resources.add_row(
        "Free disk",
        Text(f"Reserve: {limits.get('min_free_gb', 10):g} GiB", style="dim"),
        f"{free / GIB:.1f} GiB" if free is not None else "—",
    )
    performance = status.get("performance", {})
    cpu = measured.get("lnd_cpu_percent")
    rss = measured.get("lnd_rss_bytes")
    available = measured.get("host_memory_available_bytes")
    receive_rate = measured.get("host_receive_bytes_per_second")
    read_rate = measured.get("lnd_disk_read_bytes_per_second")
    write_rate = measured.get("lnd_disk_write_bytes_per_second")
    capacity = performance.get("cpu_capacity")
    cache = performance.get("block_cache_bytes")
    resources.add_row(
        "LND CPU",
        Text(
            f"{capacity} available CPUs · 100% = 1 core" if capacity else "100% = 1 core",
            style="dim",
        ),
        f"{cpu:.0f}%" if cpu is not None else "Waiting for samples",
    )
    resources.add_row(
        "LND RAM",
        Text(
            f"Host available: {available / GIB:.1f} GiB"
            if available is not None
            else "Resident memory",
            style="dim",
        ),
        f"{rss / GIB:.2f} GiB" if rss is not None else "—",
    )
    resources.add_row(
        "Host download",
        Text("All non-loopback interfaces", style="dim"),
        f"{receive_rate / MIB:.2f} MiB/s" if receive_rate is not None else "Waiting for samples",
    )
    resources.add_row(
        "LND disk I/O",
        Text("Read / write", style="dim"),
        (
            f"{read_rate / MIB:.1f} / {write_rate / MIB:.1f} MiB/s"
            if read_rate is not None and write_rate is not None
            else "—"
        ),
    )
    if cache is not None:
        resources.add_row(
            "Block cache",
            Text("Configured capacity", style="dim"),
            f"{cache / GIB:g} GiB",
        )
    return resources


def readiness_notes(status):
    """The readiness decision and, below it, what peer recovery reports."""
    readiness = status.get("readiness", {})
    reasons = readiness.get("reasons", [])
    evidence = Text("Readiness  ", style="bold")
    if readiness.get("ready"):
        evidence.append("criteria satisfied", style="green")
    else:
        evidence.append(safe(" · ".join(reasons[:2]) or "waiting for observations"), style="yellow")
    count = status.get("readiness_samples", 0)
    evidence.append(f"\n{count} observation{'s' if count != 1 else ''} this session", style="dim")
    evidence.append(
        f" · samples are at least {MINIMUM_INTERVAL_SECONDS // 60} minutes apart", style="dim"
    )
    if "newest_funding_lag_blocks" in status:
        lag = status["newest_funding_lag_blocks"]
        evidence.append(
            f"\nNewest funding output: {lag:,} blocks below the tip",
            style="yellow" if lag > FUNDING_LAG_BLOCKS else "dim",
        )
    recovery = status.get("peer_recovery", {})
    if recovery:
        recovery_notes(evidence, recovery, status.get("node", {}))
    return evidence


def recovery_notes(evidence, recovery, node):
    """Append what peer recovery has done: its dials, the current sync, rejections and stalls."""
    evidence.append(
        f"\nPeer recovery: {recovery.get('attempts', 0)} attempts · "
        f"{recovery.get('connections', 0)} connected · "
        f"{recovery.get('rotations', 0)} rotations · "
        f"{recovery.get('active_syncers', 0)} active syncers",
        style="dim",
    )
    if recovery.get("historical_syncs"):
        new = recovery.get("sync_new")
        evidence.append(
            f"\nSync {recovery['historical_syncs']}: "
            + (
                "waiting for the peer's channel list"
                if new is None
                else f"peer lists {recovery.get('sync_advertised') or 0:,} channels · "
                f"{new:,} new here · {min(recovery.get('sync_queried') or 0, new):,} requested"
            )
            + f"\nChannels added this session: {recovery.get('processed_channels', 0):,}",
            style="dim",
        )
    if "pong_failures" in recovery:
        evidence.append(
            f"\nPong failures: {recovery['pong_failures']} · "
            f"{recovery.get('pong_disconnects', 0)} disconnected locally",
            style="dim",
        )
    if "ban_disconnects" in recovery:
        banned = recovery["ban_disconnects"]
        evidence.append(
            f"\nRejected: {recovery.get('rejected_invalid', 0)} failed validation · "
            f"{recovery.get('rejected_outdated', 0)} known closed · "
            f"{banned} peer{'s' if banned != 1 else ''} banned",
            style="red" if banned else "dim",
        )
    if recovery.get("log_backlog_bytes", 0):
        evidence.append(
            f"\nLog reader catching up: {recovery['log_backlog_bytes'] / MIB:.1f} MiB unread",
            style="yellow",
        )
    if recovery.get("stalled_seconds", 0) >= 60 and not node.get("synced_to_graph"):
        evidence.append(
            f"\nNo graph progress for {recovery['stalled_seconds'] // 60} minutes",
            style="yellow",
        )


def closing_lines(status, directory, now, attached):
    """What the session left behind or which command comes next, and how fresh the status is."""
    from .collector import DEFAULT_DIRECTORY

    state = status.get("state", "unknown")
    command = ["./run.sh", "collect"]
    if directory != DEFAULT_DIRECTORY:
        command += ["--dir", str(directory)]
    footer = Text()
    if state in FINISHED:
        if status.get("stop_reason"):
            footer.append("Stopped: " + safe(status["stop_reason"]) + "\n", style="dim")
        if status.get("artifact_verified"):
            footer.append("Exact analysis, private\n", style="bold green")
            footer.append(safe(status.get("analysis", "")))
        else:
            footer.append("Next step  ", style="bold")
            footer.append("Review the diagnostics and resume when ready.")
        if status.get("error"):
            footer.append("\n" + safe(status["error"]), style="yellow")
    elif not status.get("collector_running", True) and state in ACTIVE:
        footer.append(
            "Collector stopped before recording a final result. Inspect supervisor.log.",
            style="yellow",
        )
    elif status.get("wallet_state") in ("NON_EXISTING", "LOCKED"):
        footer.append("Wallet action needed\n", style="bold yellow")
        footer.append(shlex.join(command + ["wallet"]))
    elif state in ("initialized", "not_initialized"):
        footer.append(shlex.join(command + ["start"]), style="cyan")
    else:
        footer.append(
            "Ctrl+C detaches this view; collection continues."
            if attached
            else "Ctrl+C requests a clean collector shutdown.",
            style="dim",
        )
        footer.append("\nStop collection: " + shlex.join(command + ["stop"]), style="dim")
    if status.get("last_rpc_error") and state not in FINISHED:
        footer.append("\nRPC: " + safe(status["last_rpc_error"])[-RPC_ERROR_TAIL:], style="yellow")
    freshness = age(status.get("updated_at"), now)
    if freshness is not None:
        footer.append(f"\nLast collector update {int(freshness)}s ago", style="dim")
    if status.get("session"):
        footer.append("\nLogs: " + safe(status["session"]), style="dim")
    return footer


def render(status, directory, *, now=None, attached=True, spinner=None):
    """The dashboard panel for one saved status.

    `attached` tells a watching view, which Ctrl+C detaches, from the foreground supervisor,
    which Ctrl+C stops.
    """
    now = now or datetime.now(UTC)
    heading = title_line(status)
    indicator = state_line(status, spinner)
    timer = session_window(status, now)
    metrics = sync_metrics(status)
    resources = resource_table(status)
    evidence = readiness_notes(status)
    footer = closing_lines(status, directory, now, attached)
    return Panel(
        Group(
            heading,
            revision_label(status),
            indicator,
            timer,
            Text(""),
            metrics,
            Text(""),
            evidence,
            Text(""),
            resources,
            Text(""),
            footer,
        ),
        box=box.ROUNDED,
        border_style="bright_black",
        padding=(0, 2),
    )


def watch(directory, console=None):
    """Show the live dashboard until the session ends or Ctrl+C detaches the view.

    Without a terminal, the panel is printed once.
    """
    console = console or Console()
    if not console.is_terminal:
        console.print(render(read_status(directory), directory))
        return
    try:
        with Live(
            console=console,
            refresh_per_second=LIVE_REFRESH_PER_SECOND,
            get_renderable=live_view(lambda: read_status(directory), directory),
        ):
            while True:
                status = read_status(directory)
                if status["state"] in FINISHED or not status["collector_running"]:
                    break
                time.sleep(WATCH_SECONDS)
    except KeyboardInterrupt:
        console.print("[dim]Dashboard detached. The collector keeps running.[/dim]")
