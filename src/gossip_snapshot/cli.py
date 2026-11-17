"""uv-native Typer commands for offline analysis and the bounded collector."""

import fcntl
import json
import os
import platform
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import typer
from rich.console import Console
from rich.live import Live
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.text import Text

from . import collector, core, dashboard, peers
from .analysis import BIN_BLOCKS, analyze, verify_artifact
from .audit import counts as audit_counts
from .audit import write_audit
from .common import GIB, save, utc_now
from .figures import redraw
from .fixture import make_fixture, sample_rows
from .funding_value import RECENCY, measure_value, verify_value
from .observers import compare_observers, verify_observers
from .paper_numbers import write_numbers
from .performance import DEFAULT_BLOCK_CACHE_GB, MAX_WORKERS, check_block_cache, worker_count
from .publish import publish
from .readiness import check_readiness
from .schema import load_json

app = typer.Typer(
    help="Authenticated Lightning gossip snapshots: collect, verify, inspect.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
collect_app = typer.Typer(
    help="Bounded collection on Linux, with a live dashboard.", no_args_is_help=True
)
core_app = typer.Typer(help="Pinned Bitcoin Core chain backend on Linux.", no_args_is_help=True)
app.add_typer(collect_app, name="collect")
app.add_typer(core_app, name="core")
CoreOption = Annotated[
    Path,
    typer.Option("--core-dir", help="Bitcoin Core directory used by a new observer directory."),
]
console = Console()
error_console = Console(stderr=True)
LAUNCH_SECONDS = 60  # Wait for a detached supervisor to take its lock.
# Default time ceiling of a session. Readiness alone needs three observations half an
# hour apart, and a first synchronization takes longer than that.
DEFAULT_HOURS = 4
FOLLOW_SECONDS = 10  # Interval at which `core status --follow` asks the node again.


def describe(exc):
    """What went wrong, in one line.

    A KeyError or a TypeError carries only a field name or Python's complaint about a type.
    Both arise where a saved file or a node's reply is read, so the line says that.
    """
    if isinstance(exc, KeyError):
        field = repr(exc.args[0]) if exc.args else "(unnamed)"
        return f"an input file or a node's reply lacks the expected field {dashboard.safe(field)}"
    if isinstance(exc, TypeError):
        return (
            "an input file or a node's reply holds a value of an unexpected type "
            f"({dashboard.safe(exc)})"
        )
    return dashboard.safe(exc)


@contextmanager
def errors():
    """Report an expected failure as one red line and exit with status 2, without a traceback."""
    try:
        yield
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        error_console.print(Text(f"Error: {describe(exc)}", style="red"))
        raise typer.Exit(2) from None


def require_linux():
    """Refuse a command that runs a node on any system but Linux."""
    if platform.system() != "Linux":
        raise ValueError("Run collection on Linux.")


@app.command("fixture")
def fixture_command(
    output: Annotated[Path, typer.Option(help="New synthetic capture directory.")],
    records: Annotated[
        int,
        typer.Option(
            min=0, max=5000, help="Synthetic announcements to generate; 0 writes the minimal three."
        ),
    ] = 0,
):
    """Create signed synthetic input, without network access."""
    with errors():
        if records:
            make_fixture(output, rows=sample_rows(records))
        else:
            make_fixture(output)
        console.print(Text(f"Synthetic fixture written to {output}"))


@app.command("analyze")
def analyze_command(
    capture: Annotated[Path, typer.Option(help="Capture to verify.")],
    output: Annotated[Path, typer.Option(help="New aggregate output directory.")],
    bin_width: int = BIN_BLOCKS,
    workers: Annotated[
        int, typer.Option(min=0, max=MAX_WORKERS, help="Verification processes; 0 auto, 1 serial.")
    ] = 0,
):
    """Verify a capture and write its exact aggregates. They stay private: see publish."""
    with errors():
        print(
            json.dumps(
                analyze(capture, output, bin_width, workers=workers), indent=2, sort_keys=True
            )
        )


@app.command("verify")
def verify_command(
    capture: Annotated[Path, typer.Option()],
    output: Annotated[Path, typer.Option()],
    workers: Annotated[
        int, typer.Option(min=0, max=MAX_WORKERS, help="Verification processes; 0 auto, 1 serial.")
    ] = 0,
):
    """Check the hashes of an analysis folder and compute its aggregates again."""
    with errors():
        verify_artifact(output, capture, workers=workers)
        console.print("The hashes match, and the aggregates computed again are the same.")


@app.command("readiness")
def readiness_command(samples: Path):
    """Check saved readiness samples."""
    with errors():
        result = check_readiness(load_json(samples))
        print(json.dumps(result, indent=2))
        raise typer.Exit(0 if result["ready"] else 1)


@app.command("funding-value")
def funding_value_command(
    capture: Annotated[Path, typer.Option(help="Verified capture directory.")],
    sample: Annotated[Path, typer.Option(help="Matching saved final-sample.json.")],
    output: Annotated[
        Path, typer.Option(help="New value artifact directory; evidence is private.")
    ],
    core_directory: Annotated[
        Path | None,
        typer.Option(
            "--core-dir",
            help="Also check scripts, values and unspentness against running Core (Linux only).",
        ),
    ] = None,
    workers: Annotated[int, typer.Option(min=0, max=MAX_WORKERS)] = 0,
):
    """Sum authenticated funding-output values; optionally audit them against Core."""
    with errors():
        rpc = None
        if core_directory is not None:
            require_linux()
            core.verify(core_directory.expanduser())
            rpc = core.Client(core_directory.expanduser(), exact_amounts=True)
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=error_console,
        ) as progress:
            task = progress.add_task("Verifying the saved snapshot", total=None)

            def advance(done, total, stage="outputs"):
                progress.update(
                    task,
                    description={
                        "outputs": "Checking funding scripts and values",
                        "blocks": "Locating spends after the snapshot block",
                    }[stage],
                    total=total,
                    completed=done,
                )

            result = measure_value(
                capture, sample, output, rpc=rpc, workers=workers, progress=advance
            )
            progress.update(task, description="Funding value saved", total=1, completed=1)
        print(json.dumps(result, indent=2, sort_keys=True))


@app.command("verify-value")
def verify_value_command(
    capture: Annotated[Path, typer.Option()],
    sample: Annotated[Path, typer.Option()],
    output: Annotated[Path, typer.Option()],
    workers: Annotated[int, typer.Option(min=0, max=MAX_WORKERS)] = 0,
):
    """Recompute the value aggregate and check saved Core evidence offline."""
    with errors():
        result = verify_value(capture, sample, output, workers=workers)
        recent = result["by_update_recency"]["recent_update_observed"]
        console.print(
            Text(
                f"Verified {result['funding_value_btc']} BTC across "
                f"{result['included_outputs']:,} funding outputs, "
                f"{recent['funding_value_btc']} BTC of it in the {recent['outputs']:,} "
                "with a recent update observed."
            )
        )
        if result["core_audit"] is not None:
            confirmed = result["core_audit"]["confirmed_unspent_at_reference"]
            console.print(
                Text(
                    f"The Core evidence shows {confirmed['outputs']:,} of them "
                    f"({confirmed['funding_value_btc']} BTC) unspent at block "
                    f"{result['reference']['height']:,}."
                )
            )


def print_agreement(summary):
    """Print where the second observer has the announcements of each update-recency group."""
    for name in RECENCY:
        group = summary["observer_in_second"][name]
        console.print(
            Text(
                f"{name.replace('_', ' ').capitalize()}: {group['channels']:,} here, of which "
                f"the second observer held {group['held_with_recent_update']:,} with a recent "
                f"update and {group['held_without_recent_update']:,} without, "
                f"{group['funded_after_its_reference']:,} were funded after its snapshot and "
                f"{group['absent']:,} it lacked."
            )
        )


@app.command("compare-observers")
def compare_observers_command(
    capture: Annotated[Path, typer.Option(help="Capture of the measured snapshot.")],
    other: Annotated[Path, typer.Option(help="Verified capture of a second observer.")],
    output: Annotated[Path, typer.Option(help="New directory for the aggregate comparison.")],
    workers: Annotated[int, typer.Option(min=0, max=MAX_WORKERS)] = 0,
):
    """Count how far a second observer's capture agrees with the snapshot, group by group."""
    with errors():
        print_agreement(compare_observers(capture, other, output, workers=workers))
        console.print(Text(f"Comparison saved: {output}", style="green"))


@app.command("verify-observers")
def verify_observers_command(
    capture: Annotated[Path, typer.Option(help="Capture of the measured snapshot.")],
    other: Annotated[Path, typer.Option(help="Verified capture of a second observer.")],
    output: Annotated[Path, typer.Option(help="Existing comparison directory.")],
    workers: Annotated[int, typer.Option(min=0, max=MAX_WORKERS)] = 0,
):
    """Recompute a saved comparison from both captures."""
    with errors():
        print_agreement(verify_observers(capture, other, output, workers=workers))


@app.command("publish")
def publish_command(
    capture: Annotated[Path, typer.Option(help="Verified capture directory.")],
    output: Annotated[Path, typer.Option(help="New results folder, holding aggregates only.")],
    value: Annotated[
        Path | None, typer.Option(help="Funding-value directory of this capture.")
    ] = None,
    sample: Annotated[
        Path | None, typer.Option(help="The final-sample.json that the value was measured with.")
    ] = None,
    observers: Annotated[
        Path | None, typer.Option(help="compare-observers output for this capture.")
    ] = None,
    other: Annotated[
        Path | None, typer.Option(help="The second observer's capture of that comparison.")
    ] = None,
    session_result: Annotated[
        Path | None, typer.Option(help="The session's result.json, for readiness and coverage.")
    ] = None,
    workers: Annotated[int, typer.Option(min=0, max=MAX_WORKERS)] = 0,
):
    """Write the publishable results of a capture: aggregates only, scanned for identifiers."""
    with errors():
        snapshot = publish(
            capture,
            output,
            value=value,
            sample=sample,
            observers=observers,
            other=other,
            session_result=session_result,
            workers=workers,
        )
        counts = snapshot["counts"]
        console.print(
            Text(
                f"Aggregates of {counts['authenticated_announcements']:,} announcements and "
                f"{counts['distinct_funding_keys']:,} distinct funding keys written: {output}",
                style="green",
            )
        )


@app.command("figures")
def figures_command(
    results: Annotated[Path, typer.Option(help="Results folder written by publish.")],
    output: Annotated[Path, typer.Option(help="New directory for the redrawn figure.")],
):
    """Redraw the figure from a results folder; needs no capture or node."""
    with errors():
        record = redraw(results, output)
        print(json.dumps(record, indent=2, sort_keys=True))


@app.command("paper-numbers")
def paper_numbers_command(
    results: Annotated[Path, typer.Option(help="Results folder written by publish.")],
    output: Annotated[Path, typer.Option(help="New TeX file with one macro per reported value.")],
):
    """Write the reported numbers as TeX macros from a results folder; needs no capture or node."""
    with errors():
        record = write_numbers(results, output)
        print(output.read_text(), end="")
        error_console.print(Text(f"{record['macros']} macros written: {output}", style="green"))


@collect_app.callback()
def collect_options(
    ctx: typer.Context,
    directory: Annotated[
        Path, typer.Option("--dir", help="Private observer directory.")
    ] = collector.DEFAULT_DIRECTORY,
):
    """Resolve the observer directory that every `collect` command works on."""
    ctx.obj = directory.expanduser().absolute()


BLOCK_CACHE_HELP = (
    "LND block cache in GiB, per session. It counts serialized bytes and holds "
    "more than twice that in memory. Blocks come from the local Bitcoin Core."
)
SlowPeersOption = Annotated[
    bool,
    typer.Option(
        "--keep-slow-peers/--disconnect-slow-peers",
        help=(
            "Keep peers whose pong arrives late. A pong waits behind the channels a "
            "peer sends, so the peer serving the sync is the one that would be dropped. "
            "Recorded per session; the stall watchdog still applies."
        ),
    ),
]


SyncIntervalOption = Annotated[
    int,
    typer.Option(
        min=collector.MIN_SYNC_MINUTES,
        max=collector.LND_SYNC_MINUTES,
        help=(
            "Minutes between LND's reconciliations with a random peer. 60 is LND's "
            "default. A shorter value suits a resumed graph, not an empty one."
        ),
    ),
]


def limits(
    hours,
    min_free_gb,
    max_data_gb,
    max_download_gb,
    block_cache_gb=DEFAULT_BLOCK_CACHE_GB,
    workers=0,
    target_peers=peers.DEFAULT_TARGET_PEERS,
    stall_minutes=peers.DEFAULT_STALL_MINUTES,
    keep_slow_peers=True,
    debug_sync=False,
    historical_sync_minutes=collector.LND_SYNC_MINUTES,
):
    """Validate the options of a session and return them as the namespace `run_session` reads."""
    worker_count(workers)  # Validate without replacing the recorded auto request.
    check_block_cache(block_cache_gb)
    peers.check_policy(target_peers, stall_minutes)
    shortest, longest = collector.MIN_SYNC_MINUTES, collector.LND_SYNC_MINUTES
    if (
        type(historical_sync_minutes) is not int
        or not shortest <= historical_sync_minutes <= longest
    ):
        raise ValueError(
            f"historical sync interval must be between {shortest} and {longest} minutes"
        )
    return SimpleNamespace(
        block_cache_gb=block_cache_gb,
        workers=workers,
        target_peers=target_peers,
        stall_minutes=stall_minutes,
        keep_slow_peers=keep_slow_peers,
        debug_sync=debug_sync,
        historical_sync_minutes=historical_sync_minutes,
        **{
            name: collector.positive(value)
            for name, value in {
                "hours": hours,
                "min_free_gb": min_free_gb,
                "max_data_gb": max_data_gb,
                "max_download_gb": max_download_gb,
            }.items()
        },
    )


@collect_app.command("init")
def init_command(
    ctx: typer.Context,
    core_directory: CoreOption = core.DEFAULT_DIRECTORY,
):
    """Prepare a fresh private observer directory (Linux only)."""
    with errors():
        require_linux()
        with console.status("Preparing the private observer directory…"):
            collector.initialize(ctx.obj, core_directory=core_directory.expanduser())
        console.print(
            "[green]Observer directory prepared.[/green] "
            "Continue with [bold]./run.sh collect start[/bold]."
        )


@collect_app.command("audit-closed")
def audit_closed_command(
    ctx: typer.Context,
    session: Annotated[str, typer.Option(help="Finished session ID in this observer directory.")],
    output: Annotated[
        Path | None, typer.Option(help="New JSON path; default: beside the session's capture.")
    ] = None,
):
    """Recheck a session's closed-channel index against the running Bitcoin Core."""
    with errors():
        init = collector.verify_directory(ctx.obj)
        folder = ctx.obj / "sessions" / session
        stamp = utc_now().replace(":", "").replace("-", "")
        target = output or folder / f"closed-audit-{stamp}.json"
        with console.status("Checking closed channels against Bitcoin Core…"):
            result = write_audit(folder / "capture", collector.core_rpc(init), target)
        print(json.dumps(audit_counts(result) | {"saved": str(target)}, indent=2))


@collect_app.command("run")
def run_command(
    ctx: typer.Context,
    hours: float = DEFAULT_HOURS,
    min_free_gb: float = 10,
    max_data_gb: float = 30,
    max_download_gb: float = 10,
    block_cache_gb: Annotated[float, typer.Option(help=BLOCK_CACHE_HELP)] = DEFAULT_BLOCK_CACHE_GB,
    workers: Annotated[
        int, typer.Option(min=0, max=MAX_WORKERS, help="Offline verification; 0 auto, 1 serial.")
    ] = 0,
    target_peers: Annotated[
        int,
        typer.Option(
            min=peers.MIN_TARGET_PEERS,
            max=peers.MAX_TARGET_PEERS,
            help="Maintain public gossip connections.",
        ),
    ] = peers.DEFAULT_TARGET_PEERS,
    stall_minutes: Annotated[
        int,
        typer.Option(
            min=peers.MIN_STALL_MINUTES,
            max=peers.MAX_STALL_MINUTES,
            help="Stop with diagnostics after this long without graph progress while unsynced.",
        ),
    ] = peers.DEFAULT_STALL_MINUTES,
    keep_slow_peers: SlowPeersOption = True,
    plain: Annotated[bool, typer.Option(help="Plain supervisor output for logs.")] = False,
    debug_sync: Annotated[
        bool, typer.Option(help="Record DISC/GRPH/PEER debug logs for this session only.")
    ] = False,
    historical_sync_minutes: SyncIntervalOption = collector.LND_SYNC_MINUTES,
):
    """Run the supervisor in the foreground; Ctrl+C requests a clean stop."""
    with errors():
        args = limits(
            hours,
            min_free_gb,
            max_data_gb,
            max_download_gb,
            block_cache_gb,
            workers,
            target_peers,
            stall_minutes,
            keep_slow_peers,
            debug_sync,
            historical_sync_minutes,
        )
        require_linux()
        if plain or not console.is_terminal:
            code = collector.run_session(ctx.obj, args)
        else:
            with Live(
                console=console,
                refresh_per_second=dashboard.LIVE_REFRESH_PER_SECOND,
                get_renderable=dashboard.live_view(
                    lambda: dashboard.read_status(ctx.obj), ctx.obj, attached=False
                ),
            ):
                code = collector.run_session(ctx.obj, args, emit_result=False)
        raise typer.Exit(code)


def launch(directory, args):
    """Detach one bounded supervisor, using this exact project Python environment."""
    with (directory / "launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another terminal is already starting this collector") from None
        if collector.supervisor_running(directory):
            return None
        command = [
            sys.executable,
            "-m",
            "gossip_snapshot.cli",
            "collect",
            "--dir",
            str(directory),
            "run",
            "--plain",
        ]
        for name, value in vars(args).items():
            option = "--" + name.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    command.append(option)
                elif name == "keep_slow_peers":
                    command.append("--disconnect-slow-peers")
            else:
                command.extend([option, str(value)])
        save(
            directory / "status.json",
            {
                "state": "starting",
                "limits": vars(args),
                "updated_at": utc_now(),
            },
        )
        log_path = directory / "supervisor.log"
        with log_path.open("ab") as log:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=collector.clean_env(),
            )
        deadline = time.monotonic() + LAUNCH_SECONDS
        try:
            while not collector.supervisor_running(directory):
                if child.poll() is not None:
                    raise ValueError(f"collector exited ({child.returncode}); inspect {log_path}")
                if time.monotonic() >= deadline:
                    raise ValueError(f"collector did not start; inspect {log_path}")
                time.sleep(0.1)
        except BaseException:
            collector.stop_child(child)
            raise
        return child


@collect_app.command("start")
def start_command(
    ctx: typer.Context,
    hours: float = DEFAULT_HOURS,
    min_free_gb: float = 10,
    max_data_gb: float = 30,
    max_download_gb: float = 10,
    block_cache_gb: Annotated[float, typer.Option(help=BLOCK_CACHE_HELP)] = DEFAULT_BLOCK_CACHE_GB,
    workers: Annotated[
        int, typer.Option(min=0, max=MAX_WORKERS, help="Offline verification; 0 auto, 1 serial.")
    ] = 0,
    target_peers: Annotated[
        int,
        typer.Option(
            min=peers.MIN_TARGET_PEERS,
            max=peers.MAX_TARGET_PEERS,
            help="Maintain public gossip connections.",
        ),
    ] = peers.DEFAULT_TARGET_PEERS,
    stall_minutes: Annotated[
        int,
        typer.Option(
            min=peers.MIN_STALL_MINUTES,
            max=peers.MAX_STALL_MINUTES,
            help="Stop with diagnostics after this long without graph progress while unsynced.",
        ),
    ] = peers.DEFAULT_STALL_MINUTES,
    keep_slow_peers: SlowPeersOption = True,
    debug_sync: Annotated[
        bool, typer.Option(help="Record DISC/GRPH/PEER debug logs for this session only.")
    ] = False,
    historical_sync_minutes: SyncIntervalOption = collector.LND_SYNC_MINUTES,
    core_directory: CoreOption = core.DEFAULT_DIRECTORY,
):
    """Prepare, start or attach, unlock the wallet, and watch. Ctrl+C detaches."""
    with errors():
        args = limits(
            hours,
            min_free_gb,
            max_data_gb,
            max_download_gb,
            block_cache_gb,
            workers,
            target_peers,
            stall_minutes,
            keep_slow_peers,
            debug_sync,
            historical_sync_minutes,
        )
        require_linux()
        if not sys.stdin.isatty():
            raise ValueError("collect start needs an interactive terminal for wallet setup")
        directory = ctx.obj
        if not directory.exists():
            with console.status("Preparing the private observer directory…"):
                collector.initialize(directory, core_directory=core_directory.expanduser())
        else:
            collector.verify_directory(directory)
        with console.status("Starting the bounded collector…"):
            child = launch(directory, args)
        if child is None:
            console.print(
                "[cyan]Attaching to the running session; its settings stay unchanged.[/cyan]"
            )
        else:
            console.print(f"[green]Collector started[/green] · {hours:g} hour window")
        console.print("Wallet prompts run directly in this terminal; they are not logged.")
        try:
            if code := collector.wallet(directory):
                console.print(
                    "[yellow]Wallet setup exited. The bounded collector continues; "
                    "use ./run.sh collect wallet to retry or ./run.sh collect stop to end "
                    "it.[/yellow]"
                )
                raise typer.Exit(code)
            dashboard.watch(directory, console)
        except KeyboardInterrupt:
            console.print(
                "[dim]Detached. Use ./run.sh collect watch to return or "
                "./run.sh collect stop to end it."
            )


@collect_app.command("wallet")
def wallet_command(ctx: typer.Context):
    """Interactively create or unlock the empty observer wallet."""
    with errors():
        require_linux()
        raise typer.Exit(collector.wallet(ctx.obj))


@collect_app.command("watch")
def watch_command(ctx: typer.Context):
    """Attach a read-only live dashboard. Ctrl+C leaves collection running."""
    with errors():
        dashboard.watch(ctx.obj, console)


@collect_app.command("status")
def status_command(
    ctx: typer.Context,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print machine-readable saved status.")
    ] = False,
):
    """Show current progress; redirected output is JSON."""
    with errors():
        if as_json or not console.is_terminal:
            print(json.dumps(load_json(ctx.obj / "status.json"), indent=2))
        else:
            console.print(dashboard.render(dashboard.read_status(ctx.obj), ctx.obj))


@collect_app.command("stop")
def stop_command(ctx: typer.Context):
    """Request a clean shutdown from the owning supervisor."""
    with errors():
        if collector.request_stop(ctx.obj):
            console.print(
                "[yellow]Stop requested.[/yellow] Use ./run.sh collect watch to follow it."
            )
        else:
            console.print("No collector is running in this observer directory.")


@core_app.callback()
def core_options(
    ctx: typer.Context,
    directory: Annotated[
        Path, typer.Option("--dir", help="Private Bitcoin Core directory.")
    ] = core.DEFAULT_DIRECTORY,
):
    """Resolve the Bitcoin Core directory that every `core` command works on."""
    ctx.obj = directory.expanduser().absolute()


def core_line(info):
    """One line on the node: not running, starting, or how far it has synchronized."""
    if not info["running"]:
        return f"Bitcoin Core {info['installed_version']} is installed and not running."
    if "warming_up" in info:
        return f"Bitcoin Core is starting: {info['warming_up']}"
    synced = (
        not info["initial_block_download"]
        and info["headers"] - info["blocks"] <= core.HEADER_LEAD_BLOCKS
    )
    return (
        f"{'Synchronized' if synced else 'Synchronizing'} · block {info['blocks']:,} of "
        f"{info['headers']:,} · {info['verification_progress']:.2%} verified · "
        f"{info['size_on_disk'] / GIB:.1f} GiB on disk · {info['connections']} peers · "
        f"{info['coins_cache_bytes'] / GIB:.1f} GiB cache"
    )


@core_app.command("install")
def core_install_command(
    ctx: typer.Context,
    min_free_gb: Annotated[
        float, typer.Option(help="Refuse below this much free disk.")
    ] = core.MIN_FREE_GB,
    dbcache_gb: Annotated[
        float, typer.Option(help="Bitcoin Core database cache in GiB.")
    ] = core.DBCACHE_GB,
):
    """Download the pinned release, check its SHA-256 and prepare a full mainnet node."""
    with errors():
        require_linux()
        with Progress(
            TextColumn("Downloading Bitcoin Core"),
            BarColumn(),
            DownloadColumn(),
            console=console,
            disable=not console.is_terminal,
        ) as progress:
            task = progress.add_task("download", total=None)
            record = core.install(
                ctx.obj,
                min_free_gb=collector.positive(min_free_gb),
                dbcache_gb=dbcache_gb,
                progress=lambda done, total: progress.update(
                    task, completed=done, total=total or None
                ),
            )
        console.print(
            Text(f"Bitcoin Core {record['version']} installed in {ctx.obj}", style="green")
        )
        console.print("Next, run these two commands:")
        console.print("  ./run.sh core start", highlight=False)
        console.print("  ./run.sh core check-lnd", highlight=False)


@core_app.command("start")
def core_start_command(
    ctx: typer.Context,
    dbcache_gb: Annotated[
        float | None,
        typer.Option(
            help="Database cache in GiB for this run only, recorded in launch.json. "
            "Default: the value in the generated bitcoin.conf."
        ),
    ] = None,
):
    """Start the node in the background and wait until its RPC answers."""
    with errors():
        require_linux()
        with console.status("Starting Bitcoin Core…"):
            info = core.start(ctx.obj, dbcache_gb=dbcache_gb)
        console.print(Text(core_line(info)))


@core_app.command("status")
def core_status_command(
    ctx: typer.Context,
    follow: Annotated[
        bool, typer.Option(help="Refresh until synchronized; Ctrl+C leaves.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable status.")] = False,
):
    """Show synchronization progress."""
    with errors():
        info = core.status(ctx.obj)
        if as_json or not console.is_terminal:
            print(json.dumps(info, indent=2))
            return
        if not follow:
            console.print(Text(core_line(info)))
            return
        try:
            with Live(Text(core_line(info)), console=console, refresh_per_second=1) as live:
                while info["running"] and (
                    "warming_up" in info
                    or info["initial_block_download"]
                    or info["headers"] - info["blocks"] > core.HEADER_LEAD_BLOCKS
                ):
                    time.sleep(FOLLOW_SECONDS)
                    info = core.status(ctx.obj)
                    live.update(Text(core_line(info)))
        except KeyboardInterrupt:
            console.print("[dim]Left the view. Bitcoin Core keeps running.")


@core_app.command("stop")
def core_stop_command(ctx: typer.Context):
    """Ask the node to shut down cleanly over RPC."""
    with errors():
        with console.status("Stopping Bitcoin Core. It writes its cache to disk first…"):
            stopped = core.stop(ctx.obj)
        console.print("Bitcoin Core stopped." if stopped else "Bitcoin Core is not running.")


@core_app.command("check-lnd")
def core_check_lnd_command(
    ctx: typer.Context,
    seconds: Annotated[
        int, typer.Option(min=30, max=900, help="Give up after this long.")
    ] = collector.PROBE_SECONDS,
):
    """Probe pinned LND against this node before investing in the chain download."""
    with errors():
        require_linux()
        with console.status("Starting pinned LND in a throwaway directory…"):
            result = collector.probe_backend(ctx.obj, timeout=seconds)
        if result["outcome"] == "passed":
            console.print(
                "[green]Passed.[/green] LND completed its Bitcoin Core handshake and reached "
                "the wait for chain synchronization. The throwaway wallet was deleted."
            )
            return
        console.print(Text(f"Probe {result['outcome']}. Last LND log lines:", style="red"))
        for line in result["log_tail"]:
            console.print(Text(dashboard.safe(line)), highlight=False)
        raise typer.Exit(1)


def main(argv=None) -> int:
    """Run the command line under a private umask and return its exit status."""
    previous_umask = os.umask(0o077)
    try:
        app(args=argv, prog_name="gossip")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        os.umask(previous_umask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
