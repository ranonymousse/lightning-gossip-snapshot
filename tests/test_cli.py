import json
import subprocess
import sys

from gossip_snapshot.analysis import analyze, sha256_file, verify_artifact, write_json
from gossip_snapshot.fixture import make_fixture

FIGURES = ["age-cdf.pdf", "age-cdf.png"]


def invoke(*args):
    return subprocess.run(
        [sys.executable, "-m", "gossip_snapshot.cli", *map(str, args)],
        capture_output=True,
        text=True,
    )


def test_cli_outputs_are_reproducible_and_contain_only_aggregates(tmp_path):
    capture = tmp_path / "capture"
    make_fixture(capture)
    first, second = tmp_path / "first", tmp_path / "second"
    for output in (first, second):
        result = invoke("analyze", "--capture", capture, "--output", output)
        assert result.returncode == 0, result.stderr
        assert invoke("verify", "--capture", capture, "--output", output).returncode == 0
    for name in ["summary.json", "age-cdf.csv"]:
        assert (first / name).read_bytes() == (second / name).read_bytes()
    records = [json.loads(line) for line in (capture / "records.jsonl").read_text().splitlines()]
    for path in first.iterdir():
        data = path.read_bytes()
        for record in records:
            for field in ["bitcoin_key_1", "bitcoin_key_2", "node_key_1", "funding_outpoint"]:
                assert record[field].encode() not in data
    repeated = invoke("analyze", "--capture", capture, "--output", first)
    assert repeated.returncode == 2
    assert "exists" in repeated.stderr
    verify_artifact(first, capture)
    # Rehashing a tampered statistic still fails the independent reanalysis.
    summary = json.loads((first / "summary.json").read_text())
    summary["distinct_funding_keys"] = 100
    write_json(first / "summary.json", summary)
    checksums = json.loads((first / "checksums.json").read_text())
    checksums["summary.json"] = sha256_file(first / "summary.json")
    write_json(first / "checksums.json", checksums)
    assert invoke("verify", "--capture", capture, "--output", first).returncode == 2


def test_results_are_published_drawn_and_numbered_from_the_command_line(tmp_path):
    capture, results = tmp_path / "capture", tmp_path / "results"
    assert invoke("fixture", "--output", capture, "--records", 300).returncode == 0
    published = invoke("publish", "--capture", capture, "--output", results)
    assert published.returncode == 0, published.stderr
    assert {path.name for path in results.iterdir()} == {
        "snapshot.json",
        "age-cdf.csv",
        "checksums.json",
        "README.md",
        *FIGURES,
    }
    first, second = tmp_path / "figures", tmp_path / "again"
    for output in (first, second):
        made = invoke("figures", "--results", results, "--output", output)
        assert made.returncode == 0, made.stderr
    record = json.loads((first / "figures.json").read_text())
    # Synthetic data always keeps its label.
    assert record["label"].startswith("SYNTHETIC FIXTURE")
    assert record["inputs_sha256"]["snapshot.json"] == sha256_file(results / "snapshot.json")
    assert {path.name for path in first.iterdir()} == {*FIGURES, "figures.json"}
    for name in FIGURES:
        drawn = (first / name).read_bytes()
        assert drawn == (second / name).read_bytes() == (results / name).read_bytes()
    assert invoke("figures", "--results", results, "--output", first).returncode == 2
    assert invoke("publish", "--capture", capture, "--output", results).returncode == 2
    # The macro file is for mainnet results with a value, a comparison and a session.
    numbers = tmp_path / "gossip_numbers.tex"
    refused = invoke("paper-numbers", "--results", results, "--output", numbers)
    assert refused.returncode == 2 and not numbers.exists()
    # An edited aggregate is refused instead of drawn.
    snapshot = json.loads((results / "snapshot.json").read_text())
    snapshot["counts"]["authenticated_announcements"] = 1
    write_json(results / "snapshot.json", snapshot)
    refused = invoke("figures", "--results", results, "--output", tmp_path / "bad")
    assert refused.returncode == 2 and "checksum" in refused.stderr
    assert not (tmp_path / "bad").exists()


def test_minimal_fixture_publishes_counts_without_an_age_distribution(tmp_path):
    capture, results = tmp_path / "capture", tmp_path / "results"
    assert invoke("fixture", "--output", capture).returncode == 0
    assert invoke("publish", "--capture", capture, "--output", results).returncode == 0
    # One announcement with a recent update is too few to describe by age.
    assert not (results / "age-cdf.png").exists()
    assert json.loads((results / "snapshot.json").read_text())["funding_output_age"] is None
    refused = invoke("figures", "--results", results, "--output", tmp_path / "figures")
    assert refused.returncode == 2 and "no age distribution" in refused.stderr


def test_verified_artifact_can_be_verified_after_relocation(tmp_path):
    capture, results = tmp_path / "capture", tmp_path / "results"
    make_fixture(capture)
    analyze(capture, results, workers=1)
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    capture.rename(moved / "capture")
    results.rename(moved / "results")
    verify_artifact(moved / "results", moved / "capture", workers=1)


def test_failed_input_does_not_leave_a_publishable_artifact(tmp_path):
    capture = tmp_path / "capture"
    make_fixture(capture)
    (capture / "records.jsonl").write_text("corrupt")
    output = tmp_path / "out"
    result = invoke("analyze", "--capture", capture, "--output", output)
    assert result.returncode == 2
    assert not output.exists()


def test_a_missing_field_or_a_wrong_type_in_an_input_is_reported_as_such(tmp_path):
    samples = tmp_path / "samples.json"
    samples.write_text("[{}, {}, {}]")
    missing = invoke("readiness", samples)
    assert missing.returncode == 2
    assert "lacks the expected field 'sampled_at'" in " ".join(missing.stderr.split())
    samples.write_text("[1, 2, 3]")
    mistyped = invoke("readiness", samples)
    assert mistyped.returncode == 2
    assert "holds a value of an unexpected type" in " ".join(mistyped.stderr.split())
    # Any other failure keeps its own message.
    samples.write_text("{")
    broken = invoke("readiness", samples)
    assert broken.returncode == 2 and "input file" not in broken.stderr
    assert "Traceback" not in missing.stderr + mistyped.stderr + broken.stderr


def test_core_status_reports_a_missing_node(tmp_path):
    status = invoke("core", "--dir", tmp_path / "core", "status")
    assert status.returncode == 2 and "not installed" in status.stderr
