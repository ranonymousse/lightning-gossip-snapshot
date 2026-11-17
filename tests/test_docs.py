"""The documentation names only commands that exist."""

import re
from pathlib import Path

from typer.main import get_command

from gossip_snapshot import cli
from gossip_snapshot.readiness import MINIMUM_INTERVAL_SECONDS

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
GROUPS = ("collect", "core")


def code(text):
    """The fenced blocks and inline code spans of a Markdown document."""
    blocks = re.findall(r"```.*?\n(.*?)```", text, re.S)
    spans = re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", text, flags=re.S))
    return "\n".join([*blocks, *spans])


def test_every_documented_command_exists():
    app = get_command(cli.app)
    script = (ROOT / "run.sh").read_text()
    wrapped = {
        name
        for labels in re.findall(r"^  ([a-z|-]+)\)", script, re.M)
        for name in labels.split("|")
    }
    seen = set()
    for document in DOCUMENTS:
        for tool, first, second in re.findall(
            r"(\./run\.sh|gossip) ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", code(document.read_text())
        ):
            known = wrapped if tool == "./run.sh" else set(app.commands)
            assert first in known, f"{document.name}: {tool} {first}"
            if first in GROUPS and second:
                assert second in app.commands[first].commands, f"{document.name}: {first} {second}"
            seen.add(first)
    # The check has teeth only if the documents actually name the main commands.
    assert {"reproduce", "demo", "test", "collect", "core", "publish", "funding-value"} <= seen


def test_documented_session_defaults_are_the_real_ones():
    collect = get_command(cli.app).commands["collect"].commands
    defaults = {
        option: parameter.default
        for parameter in collect["start"].params
        for option in parameter.opts
        if isinstance(parameter.default, (int, float)) and not isinstance(parameter.default, bool)
    }
    table = re.findall(
        r"^\| `(--[a-z-]+)` \| ([0-9.]+) \|", (ROOT / "docs/collecting.md").read_text(), re.M
    )
    documented = {option: float(value) for option, value in table}
    assert len(documented) >= 9
    assert documented == {option: float(defaults[option]) for option in documented}
    # A session needs more than an hour to reach readiness, so the default allows it.
    assert defaults["--hours"] * 3600 > 2 * MINIMUM_INTERVAL_SECONDS
    # The foreground command has the same defaults.
    run = {option: p.default for p in collect["run"].params for option in p.opts}
    assert all(run[option] == defaults[option] for option in documented if option in run)
