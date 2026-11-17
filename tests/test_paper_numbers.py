"""The paper's number macros, generated from a synthetic results folder."""

import re

import pytest
from helpers import core_audit, final_sample
from typer.testing import CliRunner

from gossip_snapshot import cli
from gossip_snapshot.analysis import calculate, sha256_file, write_json
from gossip_snapshot.fixture import DAY, REFERENCE_TIME, make_fixture
from gossip_snapshot.funding_value import measure_value
from gossip_snapshot.observers import compare_observers
from gossip_snapshot.paper_numbers import (
    numbers_text,
    ordinal_date,
    significant,
    word,
    write_numbers,
    years,
)
from gossip_snapshot.publish import (
    coarse_cdf,
    collection_document,
    load_results,
    observers_document,
    recent_ages,
    snapshot_document,
    value_document,
    write_results,
)
from gossip_snapshot.schema import load_json

RECENT = (REFERENCE_TIME - 3600, None)
# A capture of 110 announcements at block 900,000. One hundred carry a recent update: ten
# funded 50,000 blocks ago, just under one nominal year, 68 funded 100,000 blocks ago, ten
# funded 260,000 blocks ago, just under five nominal years, and twelve funded 300,000
# blocks ago. Of the other ten, one has only stale updates and nine have none at all.
# Every row shares one funding key and adds one of its own.
ROWS = (
    *((850000, index, RECENT) for index in range(100, 110)),
    *((800000, index, RECENT) for index in range(110, 178)),
    *((640000, index, RECENT) for index in range(200, 210)),
    *((600000, index, RECENT) for index in range(300, 312)),
    (850000, 400, (REFERENCE_TIME - 20 * DAY, REFERENCE_TIME - 15 * DAY)),
    *((899000, index, (None, None)) for index in range(401, 410)),
)
# A second observer at block 899,800. It holds the 78 youngest channels without an
# update, five of the ten middle ones with a recent update, the stale channel, and a
# channel that the first observer lacks.
OTHER = (
    *((850000, index, (None, None)) for index in range(100, 110)),
    *((800000, index, (None, None)) for index in range(110, 178)),
    *((640000, index, (REFERENCE_TIME - 60, None)) for index in range(200, 205)),
    (850000, 400, (None, None)),
    (850500, 500, (REFERENCE_TIME - 60, None)),
)
OTHER_HEIGHT = 899800
# Two interrupted syncs, then one that asked for all it lacked, then two more.
SYNCS = [
    {"advertised": 50, "new": 50, "queried": 10},
    {"advertised": None, "new": None, "queried": 0},
    {"advertised": 52, "new": 38, "queried": 38},
    {"advertised": 52, "new": 2, "queried": 2},
    {"advertised": 52, "new": 0, "queried": 0},
]
# Capacities are 100,000,001 + i satoshis for record i: 11,000,006,105 over the 110
# announcements, of which the hundred recent ones hold 10,000,005,050.
TOTAL = 110 * 100_000_001 + sum(range(110))
RECENT_VALUE = 100 * 100_000_001 + sum(range(100))


def session_result(path, summary):
    write_json(
        path,
        {
            "state": "verified",
            "stop_reason": "readiness reached",
            "limits": {"hours": 4, "max_download_gb": 50, "max_data_gb": 60, "min_free_gb": 10},
            "logging": {"debug_sync": False},
            "included_records": summary["included_records"],
            "final_sampled_at": summary["captured_at"],
            "closed_audit": {"entries": 1},
            "readiness": {"churn": [0.0002, 0.00031]},
            "sync_coverage": [{**sync, "seen_at": "2026-11-09T22:00:00Z"} for sync in SYNCS],
        },
    )


def results_folder(tmp_path, name, value, observers, result, kind="synthetic"):
    """A results folder as publish writes it, with a value summary that carries an audit.

    A synthetic capture cannot be audited, so `publish` itself would refuse this summary.
    `kind` relabels the documents, to reach what `publish` does for a mainnet capture.
    """
    capture = tmp_path / "capture"
    summary, exact = calculate(capture, 1, workers=1)
    ages = recent_ages(exact)
    rows = coarse_cdf(ages)
    output = tmp_path / name
    documents = (
        snapshot_document(summary, ages, rows),
        value_document(load_json(value / "summary.json")),
        observers_document(load_json(observers / "summary.json")),
        collection_document(summary, capture, result),
    )
    snapshot, funding, cross, session = ({**d, "dataset_kind": kind} for d in documents)
    write_results(output, snapshot, rows, value=funding, observers=cross, collection=session)
    return output


@pytest.fixture
def parts(tmp_path):
    capture, other, sample = tmp_path / "capture", tmp_path / "other", tmp_path / "sample.json"
    value, result, observers = tmp_path / "value", tmp_path / "result.json", tmp_path / "observers"
    make_fixture(capture, rows=ROWS)
    make_fixture(other, rows=OTHER, height=OTHER_HEIGHT)
    summary, _ = calculate(capture, workers=1)
    final_sample(capture, sample)
    measure_value(capture, sample, value, workers=1)
    # Everything shown unspent, one output through a later spend.
    core_audit(value, (110, TOTAL), (1, 100_000_002))
    session_result(result, summary)
    compare_observers(capture, other, observers, workers=1)
    return value, observers, result


@pytest.fixture
def results(tmp_path, parts):
    return results_folder(tmp_path, "results", *parts)


def macros(text):
    return dict(re.findall(r"^\\newcommand\{\\gossip([A-Za-z]+)\}\{(.*)\}% .+$", text, re.M))


def change(folder, name, edit):
    """Edit one data file of a results folder and record its new checksum."""
    document = load_json(folder / name)
    edit(document)
    write_json(folder / name, document)
    checksums = load_json(folder / "checksums.json")
    checksums[name] = sha256_file(folder / name)
    write_json(folder / "checksums.json", checksums)


def test_every_reported_value_becomes_one_macro(results):
    text, record = numbers_text(load_results(results), synthetic=True)
    lines = text.splitlines()
    assert lines[0] == "% SYNTHETIC DATA. Never use this file in the paper."
    # Nothing but comments and macro definitions, so the paper can input the file as it is.
    assert all(line.startswith(("%", "\\newcommand{\\gossip")) for line in lines)
    assert record["macros"] == len(macros(text)) == sum(not line.startswith("%") for line in lines)
    assert macros(text) == {
        "SnapshotDate": "10\\textsuperscript{\\upshape th}~November 2026",
        "ReferenceBlock": "\\num{900000}",
        "Announcements": "\\num{110}",
        # The shared funding key and one further key per announcement.
        "FundingKeys": "\\num{111}",
        # 110.00006105 BTC, of which the hundred recent channels hold 90.9%.
        "ValueBTC": "\\num{110}",
        "RecentAnnouncements": "\\num{100}",
        "RecentValueShare": "90\\%",
        "NoRecentAnnouncements": "\\num{10}",
        # 100,000 blocks are 1.90 and 300,000 blocks 5.71 nominal years of 52,560 blocks.
        # The 50th of a hundred ages is the median, the 90th the 90th percentile.
        "RecentAgeMedian": "1.9",
        "RecentAgeNinetieth": "5.7",
        # Ten outputs just under one year and ten just under five years each end a row
        # there, so the shares on either side follow from the published rows.
        "RecentYoungShare": "10\\%",
        "OldRecentOutputs": "\\num{12}",
        "OldRecentShare": "12\\%",
        "AuditSpentLater": "one",
        "Churn": "0.031\\%",
        "LaterSyncNew": "two",
        "ClosedEntries": "one",
        "ClosedAuditBlock": "\\num{900000}",
        # The second observer holds 83 of the hundred recent channels and the stale one.
        "CrossRecentHeld": "\\num{83}",
        "CrossNoRecentHeld": "\\num{1}",
    }
    # The header carries what a macro cannot rewrite, and the inputs it was made from.
    assert "new channels per synchronization: 50, no list, 38, 2, 0" in text
    assert "every channel it lacked: number 3, followed by 2 with a channel list" in text
    assert "no stored update at all: 9 of 10" in text
    assert (
        "Core audit of the funding outputs: 110 of 110 outputs shown unspent at the "
        "reference block (110.00006105 BTC), 1 of them through a later spend, 0 not shown" in text
    )
    assert "second observer, captured 2026-11-10T00:00:00Z at block 899800" in text
    # Each group here in the second observer: held, not held.
    assert "%     recent_update_observed: 83, 17" in text
    assert "%     no_recent_update_observed: 1, 9" in text
    # Each group of the second observer here: five of its six recent channels, and all 79
    # that it held without a recent update.
    assert "%     recent_update_observed: 5, 1" in text
    assert "%     no_recent_update_observed: 79, 0" in text
    for name in ("snapshot.json", "funding-value.json", "observers.json", "collection.json"):
        assert f"% {name} {sha256_file(results / name)}" in text
    assert f"exactly {TOTAL} satoshis" in text
    # No extreme age is reported.
    assert "Oldest" not in text


def test_values_never_differ_by_the_value_of_a_few_outputs(tmp_path, parts):
    value, observers, result = parts
    # The ten announcements without a recent update were not shown unspent. Both totals
    # are published, since they differ by ten outputs. The split is not: a group and the
    # audited total could differ by fewer.
    core_audit(value, (100, RECENT_VALUE), (0, 0), (10, TOTAL - RECENT_VALUE))
    folder = results_folder(tmp_path, "ten-not-shown", value, observers, result)
    funding = load_json(folder / "funding-value.json")
    audit = funding["core_audit"]
    assert funding["funding_value_satoshis"] == TOTAL
    assert audit["confirmed_unspent_at_reference"]["funding_value_satoshis"] == RECENT_VALUE
    assert audit["outputs_by_state"] == {
        "unspent_when_checked": 100,
        "spent_after_reference": 0,
        "not_shown": 10,
    }
    assert all(
        funding["by_update_recency"][name]["funding_value_satoshis"] is None
        for name in ("recent_update_observed", "no_recent_update_observed")
    )
    with pytest.raises(ValueError, match="withheld"):
        numbers_text(load_results(folder), synthetic=True)
    # One output not shown: the two totals would differ by its value alone, so only the
    # audited one is published.
    core_audit(value, (109, TOTAL - 100_000_003), (0, 0), (1, 100_000_003))
    folder = results_folder(tmp_path, "one-not-shown", value, observers, result)
    funding = load_json(folder / "funding-value.json")
    audit = funding["core_audit"]
    assert audit["outputs_by_state"]["not_shown"] == 1
    assert audit["confirmed_unspent_at_reference"] == {
        "outputs": 109,
        "funding_value_satoshis": TOTAL - 100_000_003,
        "funding_value_btc": "109.00006102",
    }
    assert funding["included_outputs"] == 110 and funding["funding_value_satoshis"] is None
    assert all(
        funding["by_update_recency"][name]["funding_value_btc"] is None
        for name in ("recent_update_observed", "no_recent_update_observed")
    )
    assert "100000003" not in (folder / "funding-value.json").read_text()
    # The audit's times are published to the second, and no hash of the private summary.
    assert audit["started_at"] == "2026-11-10T01:00:00Z"
    assert set(funding["inputs"]) == {
        "manifest_sha256",
        "export_report_sha256",
        "records_sha256",
        "final_sample_sha256",
    }
    with pytest.raises(ValueError, match="withheld"):
        numbers_text(load_results(folder), synthetic=True)


def test_a_complete_mainnet_folder_is_written_with_or_without_the_macro_file(tmp_path, parts):
    value, observers, result = parts
    folder = results_folder(tmp_path, "complete", value, observers, result, kind="mainnet")
    assert (folder / "gossip_numbers.tex").read_text() == numbers_text(load_results(folder))[0]
    assert "No macro file" not in (folder / "README.md").read_text()
    # With a withheld value the numbers do not follow from the folder. The data files are
    # written all the same, and the folder's README says why the macro file is missing.
    core_audit(value, (109, TOTAL - 100_000_003), (0, 0), (1, 100_000_003))
    folder = results_folder(tmp_path, "withheld", value, observers, result, kind="mainnet")
    assert not (folder / "gossip_numbers.tex").exists()
    assert (
        "No macro file was written: a funding value is withheld"
        in (folder / "README.md").read_text()
    )
    assert load_results(folder)["value"]["funding_value_satoshis"] is None


def test_inconsistent_or_incomplete_results_yield_no_file(results, tmp_path):
    output = tmp_path / "gossip_numbers.tex"
    with pytest.raises(ValueError, match="mainnet"):
        write_numbers(results, output)

    def refused(name, edit, message):
        folder = tmp_path / f"case-{len(list(tmp_path.glob('case-*')))}"
        folder.mkdir()
        for path in results.iterdir():
            (folder / path.name).write_bytes(path.read_bytes())
        change(folder, name, edit)
        with pytest.raises(ValueError, match=message):
            numbers_text(load_results(folder), synthetic=True)

    # A session, a value or a comparison of another capture is refused, even with
    # matching checksums.
    refused("collection.json", lambda d: d.update(included_records=15), "collection record")
    refused("collection.json", lambda d: d.update(captured_at="2026-11-10T00:30:00Z"), "collection")
    refused("funding-value.json", lambda d: d.update(included_outputs=111), "different capture")
    refused(
        "observers.json",
        lambda d: d["observer"].update(inputs=d["second_observer"]["inputs"]),
        "observer comparison",
    )
    # The paper's value needs the Core audit, and coverage needs one complete sync.
    refused("funding-value.json", lambda d: d.update(core_audit=None), "Core audit")
    refused("collection.json", lambda d: d.update(sync_coverage=SYNCS[:1]), "every channel")
    # A results folder without the value has no numbers to report.
    folder = tmp_path / "without-value"
    folder.mkdir()
    for path in results.iterdir():
        if path.name != "funding-value.json":
            (folder / path.name).write_bytes(path.read_bytes())
    checksums = load_json(folder / "checksums.json")
    del checksums["funding-value.json"]
    write_json(folder / "checksums.json", checksums)
    with pytest.raises(ValueError, match="funding value"):
        numbers_text(load_results(folder), synthetic=True)
    # An edited file no longer matches its checksum.
    snapshot = load_json(results / "snapshot.json")
    write_json(results / "snapshot.json", {**snapshot, "included_records": 15})
    with pytest.raises(ValueError, match="checksum"):
        write_numbers(results, output)
    assert not output.exists()
    output.write_text("kept")
    with pytest.raises(ValueError, match="exists"):
        write_numbers(results, output)
    assert output.read_text() == "kept"


def test_cli_refuses_synthetic_results(results, tmp_path):
    output = tmp_path / "gossip_numbers.tex"
    outcome = CliRunner().invoke(
        cli.app, ["paper-numbers", "--results", str(results), "--output", str(output)]
    )
    assert outcome.exit_code == 2 and not output.exists()


def test_dates_counts_and_rounding_follow_the_papers_conventions():
    dates = {
        "2026-11-01T23:59:59Z": "1\\textsuperscript{\\upshape st}~November 2026",
        "2026-11-02T00:00:00Z": "2\\textsuperscript{\\upshape nd}~November 2026",
        "2026-11-03T12:00:00Z": "3\\textsuperscript{\\upshape rd}~November 2026",
        "2026-11-11T12:00:00Z": "11\\textsuperscript{\\upshape th}~November 2026",
        "2026-11-13T12:00:00Z": "13\\textsuperscript{\\upshape th}~November 2026",
        "2026-11-21T12:00:00Z": "21\\textsuperscript{\\upshape st}~November 2026",
        "2026-12-23T12:00:00Z": "23\\textsuperscript{\\upshape rd}~December 2026",
    }
    assert {stamp: ordinal_date(stamp) for stamp in dates} == dates
    assert [word(n) for n in (0, 3, 12, 13)] == ["zero", "three", "twelve", "\\num{13}"]
    assert [significant(x) for x in (0, 0.0436, 0.5, 1.23, 12.3)] == [
        "0",
        "0.044",
        "0.50",
        "1.2",
        "12",
    ]
    assert [years(t) for t in (0, 8, 48, 100)] == ["0.0", "0.8", "4.8", "10.0"]
