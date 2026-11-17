"""Agreement between two synthetic observers, counted group by group."""

import json

import pytest
from helpers import SECOND_HEIGHT, SECOND_OBSERVER
from typer.testing import CliRunner

from gossip_snapshot import cli
from gossip_snapshot.analysis import sha256_file, write_json
from gossip_snapshot.fixture import REFERENCE_TIME, ROWS, make_fixture
from gossip_snapshot.observers import compare_observers, verify_observers
from gossip_snapshot.schema import load_json

# This observer adds a recent channel funded after the second observer's snapshot.
OBSERVER = (*ROWS, (899900, 4, (REFERENCE_TIME - 60, None)))


@pytest.fixture
def captures(tmp_path):
    here, there = tmp_path / "here", tmp_path / "there"
    make_fixture(here, rows=OBSERVER)
    make_fixture(there, rows=SECOND_OBSERVER, height=SECOND_HEIGHT)
    return here, there


def test_each_group_is_located_in_the_other_capture(captures, tmp_path):
    output = tmp_path / "observers"
    summary = compare_observers(*captures, output, workers=1)
    assert {p.name for p in output.iterdir()} == {"summary.json", "checksums.json"}
    assert summary["observer"]["reference"]["height"] == 900000
    assert summary["second_observer"]["reference"]["height"] == 899800
    assert summary["observer_in_second"] == {
        # Channels 0 and 4: one held there without an update, one funded after its block.
        "recent_update_observed": {
            "channels": 2,
            "held_with_recent_update": 0,
            "held_without_recent_update": 1,
            "funded_after_its_reference": 1,
            "absent": 0,
        },
        # Channel 1 has only stale updates here, channel 2 none at all.
        "no_recent_update_observed": {
            "channels": 2,
            "held_with_recent_update": 1,
            "held_without_recent_update": 1,
            "funded_after_its_reference": 0,
            "absent": 0,
        },
    }
    assert summary["second_in_observer"]["recent_update_observed"] == {
        "channels": 2,
        "held_with_recent_update": 0,
        "held_without_recent_update": 1,
        "funded_after_its_reference": 0,
        "absent": 1,
    }
    # Counts only: no channel identifier leaves the comparison.
    text = (output / "summary.json").read_text()
    for line in (captures[0] / "records.jsonl").read_text().splitlines():
        assert json.loads(line)["scid"] not in text
    assert verify_observers(*captures, output, workers=1) == summary


def test_saved_comparison_must_match_both_captures(captures, tmp_path):
    output = tmp_path / "observers"
    compare_observers(*captures, output, workers=1)
    with pytest.raises(ValueError, match="exists"):
        compare_observers(*captures, output, workers=1)
    stored = load_json(output / "summary.json")
    stored["observer_in_second"]["recent_update_observed"]["absent"] = 2
    write_json(output / "summary.json", stored)
    with pytest.raises(ValueError, match="checksum"):
        verify_observers(*captures, output, workers=1)
    write_json(output / "checksums.json", {"summary.json": sha256_file(output / "summary.json")})
    with pytest.raises(ValueError, match="differs"):
        verify_observers(*captures, output, workers=1)
    # A capture compared with itself proves nothing.
    with pytest.raises(ValueError, match="different capture"):
        compare_observers(captures[0], captures[0], tmp_path / "self", workers=1)
    assert not (tmp_path / "self").exists()


def test_cli_prints_the_agreement(captures, tmp_path):
    here, there = captures
    arguments = ["--capture", str(here), "--other", str(there), "--workers", "1"]
    outcome = CliRunner().invoke(
        cli.app, ["compare-observers", *arguments, "--output", str(tmp_path / "observers")]
    )
    assert outcome.exit_code == 0, outcome.output
    assert "Recent update observed: 2 here" in outcome.output
    outcome = CliRunner().invoke(
        cli.app, ["verify-observers", *arguments, "--output", str(tmp_path / "observers")]
    )
    assert outcome.exit_code == 0, outcome.output
