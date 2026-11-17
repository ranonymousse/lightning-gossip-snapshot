import json

import pytest

from gossip_snapshot.analysis import analyze, calculate, sha256_file, verify_artifact, write_json
from gossip_snapshot.collector import inspect_export
from gossip_snapshot.fixture import make_fixture, signed_record
from gossip_snapshot.schema import load_json
from gossip_snapshot.verification import BATCH_SIZE, verified_records


@pytest.fixture
def large_capture(tmp_path):
    capture = tmp_path / "capture"
    make_fixture(capture)
    rows = [
        signed_record(800000 + i, i, features=b"\x02" if i % 7 == 0 else b"")
        for i in range(BATCH_SIZE * 2 + 1)
    ]
    path = capture / "records.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    report = load_json(capture / "export-report.json")
    excluded = sum(row["exclusion_reason"] is not None for row in rows)
    report.update(
        exported_records=len(rows),
        included_records=len(rows) - excluded,
        exclusions={"unsupported_features": excluded},
        records_sha256=sha256_file(path),
    )
    write_json(capture / "export-report.json", report)
    manifest = load_json(capture / "manifest.json")
    manifest.update(
        records_sha256=report["records_sha256"],
        export_report_sha256=sha256_file(capture / "export-report.json"),
    )
    write_json(capture / "manifest.json", manifest)
    return capture


def reseal(capture):
    report = load_json(capture / "export-report.json")
    report["records_sha256"] = sha256_file(capture / "records.jsonl")
    write_json(capture / "export-report.json", report)
    manifest = load_json(capture / "manifest.json")
    manifest.update(
        records_sha256=report["records_sha256"],
        export_report_sha256=sha256_file(capture / "export-report.json"),
    )
    write_json(capture / "manifest.json", manifest)


def test_parallel_results_match_serial(large_capture, tmp_path):
    serial, parallel = tmp_path / "serial", tmp_path / "parallel"
    assert calculate(large_capture, workers=1) == calculate(large_capture, workers=2)
    assert inspect_export(large_capture, workers=1) == inspect_export(large_capture, workers=2)
    analyze(large_capture, serial, workers=1)
    analyze(large_capture, parallel, workers=2)
    for name in ("summary.json", "age-cdf.csv"):
        assert (serial / name).read_bytes() == (parallel / name).read_bytes()
    assert load_json(parallel / "analysis-environment.json")["verification_worker_limit"] == 2
    verify_artifact(serial, large_capture, workers=2)
    verify_artifact(parallel, large_capture, workers=1)


@pytest.mark.parametrize("failure", ["signature", "duplicate_json", "blank", "duplicate_channel"])
def test_parallel_errors_fail_closed_across_batches(large_capture, failure):
    path = large_capture / "records.jsonl"
    rows = path.read_text().splitlines()
    if failure == "signature":
        row = json.loads(rows[-1])
        signed = bytearray.fromhex(row["announcement_hex"])
        signed[2] ^= 1
        row["announcement_hex"] = signed.hex()
        rows[-1] = json.dumps(row)
    elif failure == "duplicate_json":
        rows[-1] = '{"schema_version": 1,' + rows[-1][1:]
    elif failure == "blank":
        rows[-1] = ""
    else:
        rows[-1] = rows[0]
    path.write_text("\n".join(rows) + "\n")
    reseal(large_capture)
    messages = []
    for workers in (1, 2):
        with pytest.raises(ValueError) as exc:
            calculate(large_capture, workers=workers)
        messages.append(str(exc.value))
    assert messages[0] == messages[1]
    assert f"line {len(rows)}" in messages[0]


def test_early_consumer_close_joins_workers(large_capture):
    from multiprocessing import active_children

    before = {child.pid for child in active_children()}
    records = verified_records(large_capture / "records.jsonl", workers=2)
    assert next(records).line_number == 1
    records.close()
    assert {child.pid for child in active_children()} == before
