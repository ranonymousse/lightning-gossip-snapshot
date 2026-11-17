"""The committed `results/` folder obeys the publication rules and is self-consistent."""

from pathlib import Path

import pytest

from gossip_snapshot.funding_value import RECENCY, STATES
from gossip_snapshot.paper_numbers import numbers_text
from gossip_snapshot.publish import (
    CDF_STEP_BLOCKS,
    MINIMUM_ROW,
    OLD_YEARS,
    load_results,
    overview,
    scan,
)
from gossip_snapshot.schema import YEAR_BLOCKS

RESULTS = Path(__file__).resolve().parents[1] / "results"
pytestmark = pytest.mark.skipif(not RESULTS.is_dir(), reason="no published results here")


@pytest.fixture(scope="module")
def results():
    # Every data file must match the checksum recorded for it.
    return load_results(RESULTS)


def test_published_folder_passes_the_scan_and_holds_the_expected_files(results):
    scan(RESULTS)
    assert {path.name for path in RESULTS.iterdir()} == {
        "snapshot.json",
        "age-cdf.csv",
        "funding-value.json",
        "observers.json",
        "collection.json",
        "checksums.json",
        "README.md",
        "age-cdf.pdf",
        "age-cdf.png",
        "gossip_numbers.tex",
    }
    assert results["snapshot"]["dataset_kind"] == "mainnet"


def test_published_counts_add_up(results):
    snapshot, value = results["snapshot"], results["value"]
    counts, recency = snapshot["counts"], snapshot["counts"]["update_recency"]
    groups = [recency[name]["channels"] for name in RECENCY]
    assert sum(groups) == counts["authenticated_announcements"] == snapshot["included_records"]
    assert snapshot["exported_records"] - snapshot["excluded_records"] == sum(groups)
    assert sum(snapshot["exclusions"].values()) == snapshot["excluded_records"]
    # Two keys per announcement at most, and a key of one group may recur in the other.
    keys = [recency[name]["distinct_funding_keys"] for name in RECENCY]
    assert max(keys) <= counts["distinct_funding_keys"] <= sum(keys) <= 2 * sum(groups)
    closed = snapshot["closed_index"]["audit"]
    assert closed["unresolved"] == 0
    assert (
        closed["not_in_utxo_set"] + closed["unspent_at_audit"]
        == (snapshot["closed_index"]["entries"])
    )
    # The value belongs to the same announcements, and its parts sum to the whole.
    split, audit = value["by_update_recency"], value["core_audit"]
    assert value["included_outputs"] == sum(groups)
    assert [split[name]["outputs"] for name in RECENCY] == groups
    assert (
        sum(split[name]["funding_value_satoshis"] for name in RECENCY)
        == (value["funding_value_satoshis"])
    )
    states = audit["outputs_by_state"]
    assert sum(states[name] for name in STATES) == audit["outputs_checked"] == sum(groups)
    confirmed = audit["confirmed_unspent_at_reference"]
    assert confirmed["outputs"] == states["unspent_when_checked"] + states["spent_after_reference"]
    assert confirmed["funding_value_satoshis"] <= value["funding_value_satoshis"]
    assert (
        audit["tip_end"]["height"] - snapshot["reference"]["height"]
        >= (audit["blocks_scanned_after_reference"])
    )
    for document in (value, results["collection"]):
        assert document["reference"] == snapshot["reference"]
        assert document["inputs"]["manifest_sha256"] == snapshot["inputs"]["manifest_sha256"]


def test_published_age_distribution_follows_the_row_rule(results):
    snapshot, rows = results["snapshot"], results["rows"]
    recent = snapshot["counts"]["update_recency"][RECENCY[0]]["channels"]
    assert all(row["count"] >= MINIMUM_ROW for row in rows)
    assert sum(row["count"] for row in rows) == recent == rows[-1]["cumulative_count"]
    assert rows[0]["age_from_blocks"] == 0
    assert all(
        earlier["age_below_blocks"] == later["age_from_blocks"]
        for earlier, later in zip(rows, rows[1:], strict=False)
    )
    assert all(row["age_below_blocks"] % CDF_STEP_BLOCKS == 0 for row in rows[:-1])
    assert rows[-1]["age_below_blocks"] % YEAR_BLOCKS == 0
    running = 0
    for row in rows:
        running += row["count"]
        assert row["cumulative_count"] == running
        assert row["cumulative_fraction"] == running / recent
    # The count of old outputs and both quantiles follow from the rows.
    age = snapshot["funding_output_age"]
    old = sum(row["count"] for row in rows if row["age_from_blocks"] >= OLD_YEARS * YEAR_BLOCKS)
    assert age[f"outputs_at_least_{OLD_YEARS}_years"] == old
    for share, name in ((0.5, "median_tenths"), (0.9, "p90_tenths")):
        row = next(row for row in rows if row["cumulative_count"] >= share * recent)
        low, high = row["age_from_blocks"], row["age_below_blocks"] - 1
        assert 10 * low / YEAR_BLOCKS - 0.5 <= age[name] <= 10 * high / YEAR_BLOCKS + 0.5


def test_published_observer_comparison_adds_up(results):
    snapshot, cross = results["snapshot"], results["observers"]
    recency = snapshot["counts"]["update_recency"]
    assert cross["observer"]["inputs"] == snapshot["inputs"]
    assert cross["observer"]["reference"] == snapshot["reference"]
    for direction in ("observer_in_second", "second_in_observer"):
        for name in RECENCY:
            group = cross[direction][name]
            assert group["held"] + group["not_held"] == group["channels"]
    for name in RECENCY:
        assert cross["observer_in_second"][name]["channels"] == recency[name]["channels"]
    # What each observer held of the other's announcements is one and the same set.
    assert sum(cross["observer_in_second"][name]["held"] for name in RECENCY) == sum(
        cross["second_in_observer"][name]["held"] for name in RECENCY
    )


def test_generated_files_follow_from_the_data_files(results):
    assert (RESULTS / "README.md").read_text() == overview(results["snapshot"], results["value"])
    assert (RESULTS / "gossip_numbers.tex").read_text() == numbers_text(results)[0]
