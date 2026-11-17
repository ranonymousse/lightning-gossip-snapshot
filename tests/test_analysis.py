import json

import pytest
from pydantic import ValidationError

from gossip_snapshot.analysis import calculate, sha256_file, write_json
from gossip_snapshot.fixture import REFERENCE_TIME, make_fixture, signed_record
from gossip_snapshot.schema import ExportReport, Manifest, Record, decode_scid
from gossip_snapshot.wire import verify_record


@pytest.fixture
def capture(tmp_path):
    path = tmp_path / "capture"
    make_fixture(path)
    return path


def reseal(capture):
    report = json.loads((capture / "export-report.json").read_text())
    report["records_sha256"] = sha256_file(capture / "records.jsonl")
    write_json(capture / "export-report.json", report)
    manifest = json.loads((capture / "manifest.json").read_text())
    manifest["records_sha256"] = report["records_sha256"]
    manifest["export_report_sha256"] = sha256_file(capture / "export-report.json")
    write_json(capture / "manifest.json", manifest)


def test_expected_statistics_and_reused_funding_key(capture):
    summary, cdf = calculate(capture)
    assert summary["included_records"] == 3
    assert summary["excluded_records"] == 1
    assert summary["distinct_funding_keys"] == 4
    assert summary["schema_version"] == 3
    assert "pilot" not in summary
    assert summary["funding_output_age_blocks"] == {
        "median": 50000,
        "p90": 100000,
        "maximum": 100000,
    }
    # The published ages: only channel 0, 100,000 blocks old, has a recent update.
    assert summary["funding_output_age_blocks_recent_update_observed"] == {
        "median": 100000,
        "p90": 100000,
        "maximum": 100000,
    }
    assert cdf[-1]["cumulative_fraction"] == 1
    assert sum(row["count"] for row in cdf) == 3
    assert [row["recent_cumulative_fraction"] for row in cdf] == [0, 0, 1]
    assert [row["recent_count"] for row in cdf] == [0, 0, 1]
    assert calculate(capture) == (summary, cdf)


def test_counts_lead_and_split_by_update_recency_without_claiming_dormancy(capture):
    summary, _ = calculate(capture)
    counts = summary["counts"]
    assert counts["authenticated_announcements"] == 3 and counts["distinct_funding_keys"] == 4
    recency = counts["update_recency"]
    assert recency["window_seconds"] == 14 * 24 * 3600
    assert recency["reference_time"] == "2026-11-10T00:00:00Z"
    # One update an hour old; one channel with updates 15 and 20 days old; one with none.
    assert recency["recent_update_observed"] == {"channels": 1, "distinct_funding_keys": 2}
    assert recency["no_recent_update_observed"] == {
        "channels": 2,
        "distinct_funding_keys": 3,
        "channels_without_any_update": 1,
    }
    assert "does not establish that a channel is dormant" in recency["note"]
    assert summary["closed_index"]["entries"] == 1


def test_age_bands_carry_the_same_split_in_nominal_years(capture):
    summary, _ = calculate(capture)
    bands = summary["funding_output_age_bands"]
    assert bands["band_blocks"] == 144 * 365
    # Ages 0 and 50,000 blocks are under one nominal year; 100,000 blocks is in the second.
    assert bands["bands"] == [
        {"band": 0, "recent_update_observed": 0, "no_recent_update_observed": 2},
        {"band": 1, "recent_update_observed": 1, "no_recent_update_observed": 0},
    ]
    recency = summary["counts"]["update_recency"]
    for group in ("recent_update_observed", "no_recent_update_observed"):
        assert sum(band[group] for band in bands["bands"]) == recency[group]["channels"]


def test_only_synthetic_data_is_drawn_with_a_corner_label():
    from gossip_snapshot.figures import label

    assert label({"dataset_kind": "mainnet", "included_records": 3}) is None
    synthetic = {"dataset_kind": "synthetic", "included_records": 3}
    assert label(synthetic) == "SYNTHETIC FIXTURE — NOT A MEASUREMENT"


def test_update_exactly_at_the_window_edge_is_recent(capture):
    path = capture / "records.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[2]["last_update_2"] = REFERENCE_TIME - 14 * 24 * 3600
    rows[1]["last_update_2"] = REFERENCE_TIME - 14 * 24 * 3600 - 1
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    reseal(capture)
    recency = calculate(capture)[0]["counts"]["update_recency"]
    assert recency["recent_update_observed"]["channels"] == 2
    assert recency["no_recent_update_observed"]["channels_without_any_update"] == 0


def test_records_and_reports_need_every_field_of_the_single_schema(capture):
    row = json.loads((capture / "records.jsonl").read_text().splitlines()[0])
    Record.model_validate(row)
    for change in ({"schema_version": 1}, {"last_update_1": "now"}):
        with pytest.raises(ValidationError):
            Record.model_validate(row | change)
    del row["last_update_2"]
    with pytest.raises(ValidationError):
        Record.model_validate(row)
    report = json.loads((capture / "export-report.json").read_text())
    ExportReport.model_validate(report)
    del report["closed_scids"]
    with pytest.raises(ValidationError):
        ExportReport.model_validate(report)


def test_mainnet_capture_cannot_be_declared_without_its_audit(capture):
    manifest = json.loads((capture / "manifest.json").read_text())
    manifest.update(
        dataset_kind="mainnet",
        backend="bitcoind",
        validation_enabled_from_start=True,
        clean_shutdown=True,
        readiness_sha256="00" * 32,
    )
    Manifest.model_validate(manifest)
    del manifest["closed_audit_sha256"]
    with pytest.raises(ValidationError, match="closed-index audit"):
        Manifest.model_validate(manifest)
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest | {"backend": "other"})


@pytest.mark.parametrize(
    "height,tx,out", [(1, 0, 0), (900000, 1234, 1), (0xFFFFFF, 0xFFFFFF, 65535)]
)
def test_scid_integer_boundaries(height, tx, out):
    assert decode_scid(str(height << 40 | tx << 16 | out)) == (height, tx, out)


@pytest.mark.parametrize("scid", ["0", "01", "-1", str(1 << 64), "1.0", "1e8", 123, True])
def test_scid_rejects_ambiguous_values(scid):
    row = signed_record(900000, 0)
    row["scid"] = scid
    with pytest.raises((ValueError, TypeError)):
        Record.model_validate(row)


@pytest.mark.parametrize("byte_offset", [2, 66, 130, 194, 268, 300, 431])
def test_signed_field_or_signature_tamper_fails(byte_offset):
    row = signed_record(800000, 0)
    wire = bytearray.fromhex(row["announcement_hex"])
    wire[byte_offset] ^= 1
    row["announcement_hex"] = wire.hex()
    with pytest.raises(ValueError):
        verify_record(Record.model_validate(row))


def test_opaque_signed_data_is_verified():
    row = signed_record(800000, 0, extra=b"\x01\x01\x42")
    assert verify_record(Record.model_validate(row)) is None
    row["announcement_hex"] = row["announcement_hex"][:-2] + "43"
    with pytest.raises(ValueError, match="signature"):
        verify_record(Record.model_validate(row))


def test_funding_keys_cannot_be_substituted_with_node_ids():
    row = signed_record(800000, 0)
    row["bitcoin_key_1"] = row["node_key_1"]
    with pytest.raises(ValueError, match="keys differ"):
        verify_record(Record.model_validate(row))


def test_hash_tampering_fails(capture):
    with (capture / "records.jsonl").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        calculate(capture)


def test_duplicate_channel_fails_even_with_resealed_hash(capture):
    path = capture / "records.jsonl"
    path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    reseal(capture)
    with pytest.raises(ValueError, match="duplicate/conflicting"):
        calculate(capture)


def test_future_funding_height_fails(capture):
    path = capture / "records.jsonl"
    rows = path.read_text().splitlines()
    rows[0] = json.dumps(signed_record(900001, 0))
    path.write_text("\n".join(rows) + "\n")
    reseal(capture)
    with pytest.raises(ValueError, match="exceeds snapshot"):
        calculate(capture)


def test_inconsistent_prune_tip_fails(capture):
    path = capture / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["reference"]["height"] += 1
    write_json(path, manifest)
    with pytest.raises(ValueError, match="prune tip"):
        calculate(capture)


def test_cannot_relabel_fixture_mainnet(capture):
    manifest = json.loads((capture / "manifest.json").read_text())
    manifest["dataset_kind"] = "mainnet"
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest)


def test_missing_proof_is_explicit_exclusion():
    row = signed_record(800000, 0)
    row.update(
        announcement_hex=None,
        signature_verified=False,
        funding_construction="unknown",
        exclusion_reason="missing_proof",
    )
    assert verify_record(Record.model_validate(row)) == "missing_proof"


def test_nonempty_features_cannot_be_claimed_as_plain_p2wsh():
    row = signed_record(800000, 0, features=b"\x02")
    assert verify_record(Record.model_validate(row)) == "unsupported_features"
    row.update(funding_construction="p2wsh", exclusion_reason=None)
    with pytest.raises(ValueError, match="classification"):
        verify_record(Record.model_validate(row))
