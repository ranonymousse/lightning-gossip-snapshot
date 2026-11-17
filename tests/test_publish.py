"""Published results hold aggregates only, by rule and by scan."""

import csv
import json
import shutil
from collections import Counter
from contextlib import contextmanager
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from itertools import combinations

import pytest
from helpers import core_audit, final_sample

from gossip_snapshot import figures
from gossip_snapshot.analysis import nearest_rank, sha256_file, write_json
from gossip_snapshot.fixture import REFERENCE_TIME, make_fixture, sample_rows
from gossip_snapshot.funding_value import measure_value
from gossip_snapshot.observers import compare_observers
from gossip_snapshot.publish import (
    CDF_STEP_BLOCKS,
    MINIMUM_AGES,
    MINIMUM_ROW,
    OLD_YEARS,
    coarse_cdf,
    load_results,
    percent,
    publish,
    scan,
    share_younger,
    tenths,
    value_document,
)
from gossip_snapshot.schema import YEAR_BLOCKS, load_json

TEXT_FILES = {".json", ".csv", ".md", ".tex"}
FIGURE = {"age-cdf.pdf", "age-cdf.png"}
SAMPLE = sample_rows(300)


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    """A synthetic capture of 300 announcements and its published results."""
    base = tmp_path_factory.mktemp("sample")
    capture, results = base / "capture", base / "results"
    make_fixture(capture, rows=SAMPLE)
    publish(capture, results, workers=1)
    return capture, results


@pytest.fixture
def folder(sample, tmp_path):
    """A copy of the published results that a test may spoil."""
    return shutil.copytree(sample[1], tmp_path / "results")


def table(path):
    with path.open(newline="") as stream:
        return [{key: float(value) for key, value in row.items()} for row in csv.DictReader(stream)]


def test_ages_are_published_in_tenths_of_a_nominal_year_with_ties_to_even():
    assert tenths(0) == 0 and tenths(YEAR_BLOCKS) == 10
    # A twentieth of a nominal year is a whole number of blocks, so every odd multiple of
    # it lies exactly between two tenths.
    twentieth, rest = divmod(YEAR_BLOCKS, 20)
    assert rest == 0
    # 0.25 nominal years go down to the even tenth and 0.35 up to it.
    assert tenths(5 * twentieth) == 2 and tenths(7 * twentieth) == 4
    # One block off the tie, the nearer tenth wins.
    assert tenths(5 * twentieth + 1) == 3 and tenths(5 * twentieth - 1) == 2
    assert tenths(7 * twentieth + 1) == 4 and tenths(7 * twentieth - 1) == 3
    # The same rule in decimal arithmetic, over a range of ages.
    for blocks in range(0, 3 * YEAR_BLOCKS, 997):
        exact = Decimal(10 * blocks) / YEAR_BLOCKS
        assert tenths(blocks) == int(exact.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def test_distribution_rows_never_describe_fewer_than_ten_announcements():
    # Nine announcements in the first step, thirteen in the second, then seven spread
    # thinly, seventy around 5.7 nominal years and two older ones.
    ages = Counter({100: 9, 4380: 1, 5000: 12, 9000: 3, 20000: 4, 300000: 70, 310000: 2})
    rows = coarse_cdf(ages)
    assert [(row["age_from_blocks"], row["age_below_blocks"], row["count"]) for row in rows] == [
        (0, 2 * CDF_STEP_BLOCKS, 22),
        # The two oldest join the last full row, which ends at six whole nominal years.
        (2 * CDF_STEP_BLOCKS, 6 * YEAR_BLOCKS, 79),
    ]
    assert [row["cumulative_count"] for row in rows] == [22, 101]
    assert [row["cumulative_fraction"] for row in rows] == [22 / 101, 1.0]
    # An age exactly at a step boundary belongs to the row that starts there.
    edge = coarse_cdf(Counter({CDF_STEP_BLOCKS - 1: 50, CDF_STEP_BLOCKS: 50}))
    assert [(row["age_from_blocks"], row["count"]) for row in edge] == [
        (0, 50),
        (CDF_STEP_BLOCKS, 50),
    ]
    assert edge[-1]["age_below_blocks"] == YEAR_BLOCKS
    # Too small a group gets no distribution at all.
    assert coarse_cdf(Counter({5: MINIMUM_AGES - 1})) == [] and coarse_cdf(Counter()) == []


def test_a_share_is_read_only_where_a_row_ends():
    # Fifty outputs in the first step, thirty just above one nominal year, twenty old ones.
    rows = coarse_cdf(Counter({1000: 50, 60000: 30, 300000: 20}))
    assert [row["age_below_blocks"] for row in rows] == [4380, 14 * 4380, 6 * YEAR_BLOCKS]
    # No row ends at one year, so the share younger than that is not known.
    assert share_younger(rows, 1) is None
    assert share_younger(rows, 6) == 1
    # Forty outputs just under one year end a row there.
    rows = coarse_cdf(Counter({50000: 40, 60000: 60}))
    assert share_younger(rows, 1) == Fraction(2, 5) and percent(Fraction(2, 5)) == 40
    assert percent(Fraction(1, 8)) == 12 and percent(Fraction(3, 8)) == 38


def test_published_quantiles_lie_ten_announcements_from_either_end():
    # The analysis takes a quantile as an observed age, by nearest rank. In a group that
    # is large enough for ages, neither quantile is among the ten youngest or oldest.
    for size in range(MINIMUM_AGES, 2000):
        ranks = list(range(1, size + 1))
        for share in (50, 90):
            rank = nearest_rank(ranks, share)
            assert rank - 1 >= MINIMUM_ROW and size - rank >= MINIMUM_ROW
    # One announcement fewer, and the 90th percentile is the tenth oldest age.
    below = MINIMUM_AGES - 1
    assert below - nearest_rank(list(range(1, below + 1)), 90) == MINIMUM_ROW - 1


def test_count_of_old_outputs_is_published_only_where_it_follows_from_the_rows(tmp_path):
    recent = (REFERENCE_TIME - 3600, None)

    def published(name, rows):
        capture = tmp_path / name
        make_fixture(capture, rows=rows)
        return publish(capture, tmp_path / f"{name}-results", workers=1)["funding_output_age"]

    young = [(800000, index, recent) for index in range(100, 188)]
    # Nine outputs just under five nominal years and three above it share one row, so
    # their split would describe three announcements. The count is withheld.
    spanning = [
        *young,
        *((650000, index, recent) for index in range(200, 209)),
        *((630000, index, recent) for index in range(300, 303)),
    ]
    age = published("spanning", spanning)
    assert age["outputs_at_least_5_years"] is None and age["p90_tenths"] == tenths(250000)
    # Ten outputs just under five years end a row at the boundary: the count follows.
    bounded = [
        *young[:78],
        *((640000, index, recent) for index in range(200, 210)),
        *((600000, index, recent) for index in range(300, 312)),
    ]
    assert published("bounded", bounded)["outputs_at_least_5_years"] == 12
    # No output of five years: the rows end before the boundary, and the count is zero.
    more = [(800000, index, recent) for index in range(400, 412)]
    assert published("young", [*young, *more])["outputs_at_least_5_years"] == 0
    # One announcement short of a hundred: no ages at all.
    assert published("small", spanning[:-1]) is None


def test_sample_rows_are_fixed_by_their_count():
    assert sample_rows(300) == SAMPLE and len(SAMPLE) == 300
    assert sum(updates[0] is not None for _, _, updates in SAMPLE) == 240
    assert len({index for _, index, _ in SAMPLE}) == 300


def test_published_folder_holds_aggregates_and_nothing_of_the_records(sample):
    capture, results = sample
    assert {path.name for path in results.iterdir()} == {
        "snapshot.json",
        "age-cdf.csv",
        "checksums.json",
        "README.md",
        *FIGURE,
    }
    assert "Synthetic data, not a measurement" in (results / "README.md").read_text()
    snapshot = load_json(results / "snapshot.json")
    assert snapshot["counts"]["authenticated_announcements"] == 300
    # Counted independently from the fixture's rows, at block 900,000.
    recent = [900000 - height for height, _, updates in SAMPLE if updates[0] is not None]
    age = snapshot["funding_output_age"]
    ordered = sorted(recent)
    assert age["median_tenths"] == tenths(ordered[len(ordered) // 2 - 1])
    assert "maximum" not in json.dumps(snapshot) and "pilot" not in json.dumps(snapshot)
    rows = table(results / "age-cdf.csv")
    assert all(row["count"] >= MINIMUM_ROW for row in rows)
    # The sample's old outputs are spread thinly, so no row starts at five nominal years
    # and their count is withheld.
    assert all(row["age_from_blocks"] != 5 * YEAR_BLOCKS for row in rows)
    assert sum(a >= 5 * YEAR_BLOCKS for a in recent) > 0
    assert age["outputs_at_least_5_years"] is None
    assert sum(row["count"] for row in rows) == len(recent) == rows[-1]["cumulative_count"]
    assert rows[0]["age_from_blocks"] == 0 and rows[-1]["age_below_blocks"] % YEAR_BLOCKS == 0
    assert all(
        earlier["age_below_blocks"] == later["age_from_blocks"]
        for earlier, later in zip(rows, rows[1:], strict=False)
    )
    assert snapshot["age_cdf"] == {
        "group": "recent_update_observed",
        "step_blocks": CDF_STEP_BLOCKS,
        "minimum_row_count": MINIMUM_ROW,
        "rows": len(rows),
    }
    # No key, channel identifier or outpoint of any record is in a published file.
    records = [json.loads(line) for line in (capture / "records.jsonl").read_text().splitlines()]
    for path in results.iterdir():
        if path.suffix in TEXT_FILES:
            text = path.read_text()
            for record in records:
                for field in ("scid", "funding_outpoint", "bitcoin_key_1", "bitcoin_key_2"):
                    assert record[field] not in text
                assert record["node_key_1"] not in text and record["node_key_2"] not in text


def test_publishing_twice_gives_the_same_files(sample, tmp_path):
    capture, results = sample
    again = tmp_path / "again"
    publish(capture, again, workers=1)
    for name in ("snapshot.json", "age-cdf.csv", "checksums.json", "README.md"):
        assert (again / name).read_bytes() == (results / name).read_bytes()
    with pytest.raises(ValueError, match="exists"):
        publish(capture, again, workers=1)


def test_a_group_below_ten_announcements_is_not_described_by_age(tmp_path):
    capture, results = tmp_path / "capture", tmp_path / "results"
    make_fixture(capture)
    snapshot = publish(capture, results, workers=1)
    assert snapshot["funding_output_age"] is None and snapshot["age_cdf"]["rows"] == 0
    assert (results / "age-cdf.csv").read_text().splitlines() == [
        "age_from_blocks,age_below_blocks,count,cumulative_count,cumulative_fraction"
    ]
    assert not FIGURE & {path.name for path in results.iterdir()}
    # The counts are published all the same.
    assert snapshot["counts"]["update_recency"]["recent_update_observed"]["channels"] == 1
    with pytest.raises(ValueError, match="no age distribution"):
        figures.redraw(results, tmp_path / "figures")


def test_value_comparison_and_session_are_published_as_checked_aggregates(sample, tmp_path):
    capture, _ = sample
    other, value, observers = tmp_path / "other", tmp_path / "value", tmp_path / "observers"
    sample_json, result = tmp_path / "final-sample.json", tmp_path / "result.json"
    # The second observer stopped 100 blocks earlier and holds the first 250 channels.
    make_fixture(other, rows=SAMPLE[:250], height=899900)
    final_sample(capture, sample_json)
    measured = measure_value(capture, sample_json, value, workers=1)
    stored = compare_observers(capture, other, observers, workers=1)
    session = {
        "state": "verified",
        "session": "/home/someone/.local/state/observer/sessions/1",
        "collector_revision": {"commit": "ab" * 20, "dirty": False},
        "stop_reason": "readiness reached",
        "limits": {"hours": 4.0, "max_download_gb": 50, "max_data_gb": 60, "min_free_gb": 10},
        "logging": {"debug_sync": True, "file": "lnd.log"},
        "performance": {"cpu_capacity": 64},
        "included_records": 300,
        "final_sampled_at": "2026-11-10T00:00:00Z",
        "closed_audit": {"entries": 1},
        "readiness": {"ready": True, "reasons": [], "churn": [0.0002, 1 / 3000]},
        "sync_coverage": [
            {"advertised": 300, "new": 300, "queried": 300, "seen_at": "2026-11-09T22:00:00Z"}
        ],
    }
    write_json(result, session)
    results = tmp_path / "results"
    arguments = {
        "value": value,
        "sample": sample_json,
        "observers": observers,
        "other": other,
        "session_result": result,
        "workers": 1,
    }
    publish(capture, results, **arguments)
    # A synthetic capture gets no macro file.
    assert {path.name for path in results.iterdir()} == {
        "snapshot.json",
        "age-cdf.csv",
        "funding-value.json",
        "observers.json",
        "collection.json",
        "checksums.json",
        "README.md",
        *FIGURE,
    }
    # The value is published field by field, tied to the private summary by its hash.
    funding = load_json(results / "funding-value.json")
    assert set(funding) == {
        "schema_version",
        "dataset_kind",
        "captured_at",
        "reference",
        "included_outputs",
        "funding_value_satoshis",
        "funding_value_btc",
        "by_update_recency",
        "value_source",
        "core_audit",
        "inputs",
        "interpretation",
    }
    for key in ("included_outputs", "funding_value_satoshis", "funding_value_btc"):
        assert funding[key] == measured[key]
    assert funding["by_update_recency"] == measured["by_update_recency"]
    assert funding["core_audit"] is None
    # No hash of the private summary: its content could be guessed and so give a withheld
    # value away.
    assert funding["inputs"] == measured["inputs"]
    # The private summary's note names a private file. The published one has its own.
    assert "core-evidence" in measured["interpretation"]
    assert "core-evidence" not in funding["interpretation"]
    # Both directions of the comparison, each as held and not held.
    seen = load_json(results / "observers.json")
    assert set(seen) == {
        "schema_version",
        "dataset_kind",
        "observer",
        "second_observer",
        "observer_in_second",
        "second_in_observer",
        "note",
    }
    for direction in ("observer_in_second", "second_in_observer"):
        for name, group in stored[direction].items():
            held = group["held_with_recent_update"] + group["held_without_recent_update"]
            assert seen[direction][name] == {
                "channels": group["channels"],
                "held": held,
                "not_held": group["channels"] - held,
            }
    assert seen["second_in_observer"]["recent_update_observed"]["not_held"] == 0
    collection = load_json(results / "collection.json")
    assert set(collection) == {
        "schema_version",
        "dataset_kind",
        "captured_at",
        "reference",
        "included_records",
        "session",
        "readiness",
        "sync_coverage",
        "inputs",
    }
    assert collection["session"] == {
        "stopped_at_readiness": True,
        "limits": {"hours": 4.0, "max_download_gb": 50, "max_data_gb": 60, "min_free_gb": 10},
        "debug_log": True,
    }
    assert collection["sync_coverage"] == [{"advertised": 300, "new": 300, "queried": 300}]
    assert collection["readiness"]["churn"] == [0.0002, 1 / 3000]
    assert set(collection["inputs"]) == {"manifest_sha256", "session_result_sha256"}
    # Nothing of the session's paths, revision, hardware or event times is copied.
    text = (results / "collection.json").read_text()
    assert "/home" not in text and "ab" * 20 not in text and "22:00" not in text
    assert "cpu" not in text and "lnd.log" not in text
    # Half-given pairs and a session of another capture are refused, and a refusal
    # leaves nothing behind.
    refused = tmp_path / "refused"
    for change, message in (
        ({"sample": None}, "final sample"),
        ({"other": None}, "other capture"),
    ):
        with pytest.raises(ValueError, match=message):
            publish(capture, refused, **{**arguments, **change})
    write_json(result, {**session, "included_records": 299})
    with pytest.raises(ValueError, match="does not belong"):
        publish(capture, refused, **arguments)
    assert not refused.exists()
    assert not list(tmp_path.glob(".gossip-results-*"))


def test_a_value_of_fewer_than_ten_outputs_is_withheld(tmp_path):
    capture, sample_json, value = tmp_path / "capture", tmp_path / "sample.json", tmp_path / "value"
    make_fixture(capture)
    final_sample(capture, sample_json)
    measured = measure_value(capture, sample_json, value, workers=1)
    assert measured["included_outputs"] == 3 and measured["funding_value_satoshis"] > 0
    results = tmp_path / "results"
    publish(capture, results, value=value, sample=sample_json, workers=1)
    funding = load_json(results / "funding-value.json")
    # Counts are published, and no value is: the total and both groups are too small.
    assert funding["included_outputs"] == 3
    assert funding["funding_value_satoshis"] is None and funding["funding_value_btc"] is None
    for name in ("recent_update_observed", "no_recent_update_observed"):
        group = funding["by_update_recency"][name]
        assert group["outputs"] == measured["by_update_recency"][name]["outputs"]
        assert group["funding_value_satoshis"] is None and group["funding_value_btc"] is None
    assert "not published" in (results / "README.md").read_text()


def test_no_two_published_values_differ_by_the_value_of_a_few_outputs(tmp_path):
    recent, silent = (REFERENCE_TIME - 3600, None), (None, None)
    capture, sample_json, value = tmp_path / "capture", tmp_path / "sample.json", tmp_path / "value"
    rows = [
        *((800000, index, recent) for index in range(100, 120)),
        *((850000, index, silent) for index in range(200, 210)),
    ]
    make_fixture(capture, rows=rows)
    final_sample(capture, sample_json)
    stored = measure_value(capture, sample_json, value, workers=1)
    # Without an audit the total and its split are published: thirty outputs, twenty and ten.
    plain = value_document(stored)
    groups = plain["by_update_recency"]
    assert plain["funding_value_satoshis"] == sum(
        groups[name]["funding_value_satoshis"]
        for name in ("recent_update_observed", "no_recent_update_observed")
    )
    # The audit did not show eleven outputs unspent: the ten without a recent update and
    # one of the others. Both totals are published. The split is not, because the recent
    # group and the audited total would differ by that one output.
    total, one = stored["funding_value_satoshis"], 100_000_001
    hidden = groups["no_recent_update_observed"]["funding_value_satoshis"] + one
    core_audit(value, (19, total - hidden), (0, 0), (11, hidden))
    audited = value_document(load_json(value / "summary.json"))
    assert audited["funding_value_satoshis"] == total
    assert audited["core_audit"]["confirmed_unspent_at_reference"]["funding_value_satoshis"] == (
        total - hidden
    )
    published = [
        audited["funding_value_satoshis"],
        audited["core_audit"]["confirmed_unspent_at_reference"]["funding_value_satoshis"],
        *(
            group["funding_value_satoshis"]
            for group in audited["by_update_recency"].values()
            if isinstance(group, dict)
        ),
    ]
    assert [amount for amount in published if amount is not None] == [total, total - hidden]


def test_a_capture_with_unchecked_closed_entries_is_not_published(tmp_path):
    capture = tmp_path / "capture"
    make_fixture(capture)
    # The audit could not look up the one closed entry.
    audit = load_json(capture / "closed-audit.json")
    audit.update(not_in_utxo_set=0, unresolved=1, unresolved_reasons={"output lookup failed": 1})
    write_json(capture / "closed-audit.json", audit)
    manifest = load_json(capture / "manifest.json")
    manifest["closed_audit_sha256"] = sha256_file(capture / "closed-audit.json")
    write_json(capture / "manifest.json", manifest)
    with pytest.raises(ValueError, match="1 entries unchecked"):
        publish(capture, tmp_path / "results", workers=1)
    assert not (tmp_path / "results").exists()


# Forms that a capture or a session holds, each written in letters and digits that no
# real key, channel or node has.
KEY = "02" + "ab" * 32
SCRIPT = "5221" + KEY + "21" + "03" + "cd" * 32 + "52ae"


@pytest.mark.parametrize(
    ("what", "text"),
    [
        ("a public key", KEY),
        ("a public key", KEY.upper()),
        ("an unexpected hash or key", "04" + "ab" * 64),  # an uncompressed key
        ("an unexpected hash or key", SCRIPT),  # a two-of-two witness script
        ("an unexpected hash or key", "0020" + "ef" * 32),  # a P2WSH output script
        ("an unexpected hash or key", "12" * 64),  # a signature
        ("an unexpected hash or key", "0100" + "34" * 430),  # a signed announcement
        ("an unexpected hash or key", "ef" * 32),
        ("an unexpected hash or key", "0123456789" * 4),
        ("an outpoint", "cd" * 32 + ":1"),
        ("a channel identifier", "879609302220800000"),
        ("a channel identifier", "879609302220800000."),  # at the end of a sentence
        ("a channel identifier", "879,609,302,220,800,000"),
        ("a channel identifier", "0x0c35000000010000"),
        ("a channel identifier", "800000x1x0"),
        ("a channel identifier", "800000:1:0"),
        ("a Bitcoin address", "bc1q" + "qpzry9x8gf2tvdw0s3jn" * 2),
        ("a Bitcoin address", "1" + "Bq" * 16),
        ("a network address", "203.0.113.7"),
        ("a network address", "203.0.113.7."),
        ("a network address", "[2001:db8::1]:9735"),
        ("a network address", "2001:db8:0:0:0:0:0:1"),
        ("a network address", "2001:db8::1"),
        ("a network address", "2001:db8:1::"),
        ("a network address", "abcdefghij234567.onion"),
        ("an e-mail address", "someone@example.org"),
        ("a session identifier", "20261110T000000.000000Z"),
        ("a local path", "/home/someone/observer"),
        ("a local path", "/workspace/observer"),
        ("a local path", "C:\\Users\\someone"),
        ("a local path", "sessions/1/lnd.log"),
    ],
)
def test_scan_refuses_anything_shaped_like_private_data(folder, what, text):
    scan(folder)
    snapshot = load_json(folder / "snapshot.json")
    write_json(folder / "snapshot.json", {**snapshot, "cohort": f"remark {text} end"})
    with pytest.raises(ValueError, match=what):
        scan(folder)


def test_scan_reads_escaped_strings_and_refuses_files_it_does_not_know(folder):
    # A key written in JSON escapes is not a run of hexadecimal digits in the file's text.
    escaped = "".join(f"\\u{ord(character):04x}" for character in KEY)
    text = (folder / "snapshot.json").read_text()
    (folder / "snapshot.json").write_text(text.replace("{", '{"remark": "' + escaped + '", ', 1))
    assert KEY not in (folder / "snapshot.json").read_text()
    assert load_json(folder / "snapshot.json")["remark"] == KEY
    with pytest.raises(ValueError, match="a public key"):
        scan(folder)
    (folder / "snapshot.json").write_text(text)
    scan(folder)
    # Only the files of a results folder may be in it, whatever they hold.
    (folder / "notes.txt").write_text("nothing private\n")
    with pytest.raises(ValueError, match="does not belong"):
        scan(folder)


def test_scan_accepts_a_hash_only_where_a_field_names_one(folder):
    snapshot = load_json(folder / "snapshot.json")
    # A block is a height with a hash. A lone field called hash is not.
    write_json(folder / "snapshot.json", {**snapshot, "remark": {"hash": "ef" * 32}})
    with pytest.raises(ValueError, match="unexpected hash or key"):
        scan(folder)
    # A field named as a hash must hold one.
    write_json(folder / "snapshot.json", {**snapshot, "remark_sha256": KEY})
    with pytest.raises(ValueError, match="named as a hash"):
        scan(folder)
    # A value that is a path is refused wherever it starts.
    write_json(folder / "snapshot.json", {**snapshot, "remark": "~observer"})
    with pytest.raises(ValueError, match="local path"):
        scan(folder)
    write_json(folder / "snapshot.json", snapshot)
    scan(folder)


def test_scan_refuses_a_small_row_and_accepts_digits_inside_a_hash(folder):
    # A recorded hash may hold a long run of decimal digits without being a channel.
    snapshot = load_json(folder / "snapshot.json")
    snapshot["inputs"]["records_sha256"] = "ab" + "1234567890123456" + "cd" * 23
    write_json(folder / "snapshot.json", snapshot)
    scan(folder)
    lines = (folder / "age-cdf.csv").read_text().splitlines()
    first = lines[1].split(",")
    first[2] = str(MINIMUM_ROW - 1)
    (folder / "age-cdf.csv").write_text("\n".join([lines[0], ",".join(first), *lines[2:]]) + "\n")
    with pytest.raises(ValueError, match="fewer than 10"):
        scan(folder)


def test_results_are_loaded_only_when_they_match_their_checksums(folder):
    loaded = load_results(folder)
    assert loaded["value"] is None and loaded["observers"] is None and loaded["collection"] is None
    assert len(loaded["rows"]) == loaded["snapshot"]["age_cdf"]["rows"]
    checksums = load_json(folder / "checksums.json")
    # A file the folder does not define is not accepted through the checksum list.
    write_json(folder / "checksums.json", {**checksums, "records.jsonl": "00" * 32})
    with pytest.raises(ValueError, match="unexpected set of files"):
        load_results(folder)
    snapshot = load_json(folder / "snapshot.json")
    write_json(folder / "snapshot.json", {**snapshot, "schema_version": 2})
    write_json(
        folder / "checksums.json",
        {**checksums, "snapshot.json": sha256_file(folder / "snapshot.json")},
    )
    with pytest.raises(ValueError, match="another format"):
        load_results(folder)
    write_json(folder / "checksums.json", checksums)
    with pytest.raises(ValueError, match="checksum"):
        load_results(folder)


def figure_rows(*steps):
    """Rows of an age distribution from (nominal years at which a row ends, its count)."""
    total, rows, start, cumulative = sum(count for _, count in steps), [], 0, 0
    for years, count in steps:
        cumulative += count
        rows.append(
            {
                "age_from_blocks": start,
                "age_below_blocks": round(years * YEAR_BLOCKS),
                "count": count,
                "cumulative_count": cumulative,
                "cumulative_fraction": cumulative / total,
            }
        )
        start = rows[-1]["age_below_blocks"]
    return rows


def test_figure_reads_the_published_curve_at_two_ages(sample, tmp_path, monkeypatch):
    from matplotlib.colors import to_hex
    from matplotlib.offsetbox import AnnotationBbox, TextArea
    from matplotlib.text import Text

    _, results = sample
    loaded = load_results(results)
    group = loaded["snapshot"]["counts"]["update_recency"]["recent_update_observed"]
    drawn, canvas = [], figures.canvas

    @contextmanager
    def kept(*args):
        with canvas(*args) as ax:
            yield ax
            # Where everything stands, measured before the figure is saved and closed.
            ax.figure.canvas.draw()
            renderer = ax.figure.canvas.get_renderer()
            shares = ax.transData.inverted()
            (curve,) = [line for line in ax.lines if line.get_linewidth() == 1.8]
            drawn.append(
                {
                    "ax": ax,
                    "point": renderer.points_to_pixels(1),
                    "frame": ax.get_window_extent(renderer),
                    "curve": ax.transData.transform_path(curve.get_path()),
                    "markers": [
                        ax.transData.transform((line.get_xdata()[0], line.get_ydata()[0]))
                        for line in ax.lines
                        if line.get_marker() == "o"
                    ],
                    "labels": [
                        box.get_window_extent(renderer) for box in ax.findobj(AnnotationBbox)
                    ],
                    "lines": [part.get_window_extent(renderer) for part in ax.findobj(TextArea)],
                    "rows": [
                        [shares.transform((0, edge))[1] for edge in (extent.y0, extent.y1)]
                        for extent in (
                            part.get_window_extent(renderer) for part in ax.findobj(TextArea)
                        )
                    ],
                    "arrows": [
                        text.arrow_patch.get_window_extent(renderer)
                        for text in ax.texts
                        if text.arrow_patch is not None
                    ],
                }
            )

    monkeypatch.setattr(figures, "canvas", kept)

    def draw(rows, name):
        target = tmp_path / name
        target.mkdir()
        note = figures.draw({**loaded, "rows": rows}, target)
        assert note == "SYNTHETIC FIXTURE — NOT A MEASUREMENT"
        assert {path.name for path in target.iterdir()} == FIGURE
        return drawn.pop()

    def parts(figure):
        every = figure["ax"].findobj(TextArea)
        bold = [each for each in every if each.get_children()[0].get_fontweight() == "bold"]
        return [each.get_text() for each in every], [each.get_text() for each in bold]

    def clear(figure):
        """No label or arrow reaches the frame, the curve, a marker, a guide or another."""
        frame, point = figure["frame"], figure["point"]
        things = figure["labels"] + figure["arrows"]
        for extent in things:
            assert frame.x0 < extent.x0 and extent.x1 < frame.x1
            assert frame.y0 < extent.y0 and extent.y1 < frame.y1
            # The curve is 1.8 points wide, and a marker with its ring 6.3 points.
            assert not figure["curve"].intersects_bbox(extent.padded(point), filled=False)
            for x, y in figure["markers"]:
                across = max(extent.x0 - x, 0, x - extent.x1)
                assert (across**2 + max(extent.y0 - y, 0, y - extent.y1) ** 2) ** 0.5 > 3.15 * point
        for extent in figure["labels"]:
            assert not any(extent.x0 <= x <= extent.x1 for x, _ in figure["markers"])
        assert not any(one.overlaps(other) for one, other in combinations(things, 2))
        # A grid line passes between two lines of words and through none.
        for low, high in figure["rows"]:
            assert low // figures.ROW == high // figures.ROW

    totals = [
        f"{group['channels']:,}",
        "announcements",
        "with a recent update",
        f"{group['distinct_funding_keys']:,}",
        "distinct public",
        "funding keys",
    ]

    # Rows that end at one and at five nominal years: the curve is read at both ages.
    both = figure_rows(
        (0.25, 62), (0.5, 31), (0.75, 21), (1, 17), (2, 43), (3, 22), (5, 22), (6, 12), (9, 10)
    )
    assert (figures.YOUNG_YEARS, figures.OLD_YEARS) == (1, 5) and figures.OLD_YEARS is OLD_YEARS
    figure = draw(both, "both")
    ax = figure["ax"]
    assert ax.get_title(loc="left") == "SYNTHETIC FIXTURE — NOT A MEASUREMENT"
    assert ax.get_ylabel() == "Share of announcements"
    assert ax.get_xlabel() == "Channel funding-output age (nominal years)"
    # Each reading is one plain sentence over two lines. Its share is the exact count of
    # the rows on one side of the age, in whole percent: 131 of 240 are younger than one
    # year, and 22 of 240 are of five years or more.
    words, bold = parts(figure)
    assert words[:6] == ["55%", "are younger", "than 1 year", "9%", "are older", "than 5 years"]
    # A share needs its base: the group's totals stand under the curve, once each and
    # over two lines each.
    assert words[6:] == totals
    # Bold marks the values alone: the measured shares and the two totals.
    assert bold == ["55%", "9%", totals[0], totals[3]]
    # The two markers sit on the curve where a row ends at the age, with guides to both axes.
    low, high = (100 * both[row]["cumulative_fraction"] for row in (3, 6))
    assert (both[3]["cumulative_count"], both[6]["cumulative_count"]) == (131, 240 - 22)
    markers = [line for line in ax.lines if line.get_marker() == "o"]
    assert [(marker.get_xdata()[0], marker.get_ydata()[0]) for marker in markers] == [
        (1, low),
        (5, high),
    ]
    guides = [line for line in ax.lines if line.get_linewidth() == 0.6]
    assert [(guide.get_xdata().tolist(), guide.get_ydata().tolist()) for guide in guides] == [
        ([0, 1, 1], [low, low, 0]),
        ([0, 5, 5], [high, high, 0]),
    ]
    # A label is centred on its rows between the grid lines: each reading on the two rows
    # below its point, the two totals on the four rows above the age axis.
    boxes = ax.findobj(AnnotationBbox)
    assert [box.xy for box in boxes] == [(1, 37.5), (5, 75), (1, 37.5), (1, 12.5)]
    # A label stands six points from its guide or from the frame. The second reading stands
    # beyond the arrow that points to its side.
    assert [box.xybox for box in boxes] == [(6, 0), (9, 0), (-6, 0), (-6, 0)]
    # A tiny arrow starts on each vertical guide and is perpendicular to it. It points
    # left to the younger outputs and right to the older ones, five points long, in the
    # colour of the guides and no heavier than they are.
    arrows = [text for text in ax.texts if text.arrow_patch is not None]
    assert [(arrow.xy, arrow.xyann) for arrow in arrows] == [
        ((1, 37.5), (-5, 0)),
        ((5, 75), (5, 0)),
    ]
    for arrow, guide in zip(arrows, guides, strict=True):
        assert (
            to_hex(arrow.arrow_patch.get_edgecolor()) == to_hex(guide.get_color()) == figures.DARK
        )
        assert arrow.arrow_patch.get_linewidth() <= guide.get_linewidth()
    point = figure["point"]
    (left, right), ((young, _), (old, _)) = figure["arrows"], figure["markers"]
    assert left.x1 == pytest.approx(young, abs=point) and right.x0 == pytest.approx(old, abs=point)
    assert left.x0 == pytest.approx(young - 5 * point, abs=point)
    assert right.x1 == pytest.approx(old + 5 * point, abs=point)
    # Each arrow stands level with the second line of its reading, the one with the age.
    for extent, line in zip(
        figure["arrows"], (figure["lines"][2], figure["lines"][5]), strict=True
    ):
        assert (extent.y0 + extent.y1) / 2 == pytest.approx((line.y0 + line.y1) / 2, abs=point / 2)
    clear(figure)
    # The curve has one point per published row and ends at the edge of the axis.
    (curve,) = [line for line in ax.lines if line.get_linewidth() == 1.8]
    assert len(curve.get_xdata()) == len(both) + 1
    assert curve.get_xdata()[-1] == ax.get_xlim()[1] and curve.get_ydata()[-1] == 100
    # One unlabelled grid line halves each band, and the share axis ends at 100%.
    assert ax.get_yticks().tolist() == [0, 25, 50, 75, 100]
    assert ax.get_yticks(minor=True).tolist() == [12.5, 37.5, 62.5, 87.5]
    assert ax.spines["left"].get_visible() and ax.spines["left"].get_bounds() == (0, 100)
    # One column wide. The age axis title stands two points below its tick labels, and
    # the figure is lower by the two points this saves.
    assert ax.figure.get_size_inches().tolist() == [figures.WIDTH, figures.HEIGHT]
    assert figures.WIDTH == 3.45 and figures.HEIGHT * 72 == pytest.approx(120.4)
    assert ax.xaxis.labelpad == 2
    # Every word and number is set in one size.
    sizes = {text.get_fontsize() for text in ax.findobj(Text) if text.get_text()}
    assert sizes == {figures.TEXT, 6.5}

    # The sample's rows end at one nominal year and not at five, where two rows were merged.
    # The figure reads the first age and shows nothing for the second: no share is read off
    # the line between two rows.
    rows = loaded["rows"]
    (first,) = [row for row in rows if row["age_below_blocks"] == YEAR_BLOCKS]
    assert not any(row["age_below_blocks"] == OLD_YEARS * YEAR_BLOCKS for row in rows)
    figure = draw(rows, "sample")
    ax = figure["ax"]
    younger = f"{round(100 * first['cumulative_count'] / rows[-1]['cumulative_count'])}%"
    assert parts(figure) == (
        [younger, "are younger", "than 1 year", *totals],
        [younger, totals[0], totals[3]],
    )
    (marker,) = [line for line in ax.lines if line.get_marker() == "o"]
    assert (marker.get_xdata()[0], marker.get_ydata()[0]) == (
        1,
        100 * first["cumulative_fraction"],
    )
    (arrow,) = [text for text in ax.texts if text.arrow_patch is not None]
    assert arrow.xy[0] == 1 and arrow.xyann == (-5, 0)
    assert len(ax.lines) == 3 and len(ax.findobj(AnnotationBbox)) == 3
    clear(figure)

    # No row ends at one year, and the last row ends at five, the edge of the axis: the
    # figure shows the curve and the totals alone.
    figure = draw(figure_rows((2, 140), (5, 100)), "neither")
    ax = figure["ax"]
    assert parts(figure) == (totals, [totals[0], totals[3]])
    assert len(ax.lines) == 1 and not [text for text in ax.texts if text.arrow_patch is not None]
    assert ax.get_xlim() == (0, 5)
    clear(figure)
