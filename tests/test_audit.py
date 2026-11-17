"""Closed-index audit against a fake Bitcoin Core; no node and no network."""

import pytest

from gossip_snapshot.analysis import calculate, sha256_file, write_json
from gossip_snapshot.audit import audit_closed, counts, write_audit
from gossip_snapshot.fixture import make_fixture
from gossip_snapshot.schema import load_json


def scid(height, index, output=0):
    return str((height << 40) | (index << 16) | output)


SPENT, LIVE, NO_TX, FUTURE, NO_BLOCK, NO_OUTPUT = (
    scid(700000, 7),
    scid(700000, 3, 1),
    scid(700001, 50),
    scid(900001, 0),
    scid(700002, 0),
    scid(700003, 1),
)


def core(method, *params):
    if method == "getblockcount":
        return 900000
    if method == "getblockhash":
        return f"{params[0]:064x}"
    if method == "getblock":
        height = int(params[0], 16)
        if height == 700002:
            raise ValueError("Bitcoin Core RPC error -5: Block not found")
        return {"tx": [f"{height:056x}{index:08x}" for index in range(10)]}
    assert method == "gettxout" and params[2] is False  # confirmed state only
    if params[0].startswith(f"{700003:056x}"):
        raise ValueError("Bitcoin Core RPC is unreachable")
    return {"value": 0.01} if (params[0], params[1]) == (f"{700000:056x}{3:08x}", 1) else None


@pytest.fixture
def capture(tmp_path):
    path = tmp_path / "capture"
    make_fixture(path)
    (path / "closed-audit.json").unlink()
    entries = sorted([SPENT, LIVE, NO_TX, FUTURE, NO_BLOCK, NO_OUTPUT], key=int)
    (path / "closed-scids.txt").write_text("".join(f"{entry}\n" for entry in entries))
    report = load_json(path / "export-report.json")
    report.update(closed_scids=6, closed_scids_sha256=sha256_file(path / "closed-scids.txt"))
    write_json(path / "export-report.json", report)
    return path


def test_unspent_closed_entry_is_a_live_channel_and_failures_stay_unresolved(capture):
    result = audit_closed(capture, core)
    assert result["tip"] == {"height": 900000, "hash": f"{900000:064x}"}
    assert (result["entries"], result["not_in_utxo_set"], result["unspent_at_audit"]) == (6, 1, 1)
    assert result["unspent_scids"] == [LIVE]
    assert result["unresolved_reasons"] == {
        "block lookup failed": 1,
        "funding height beyond the chain tip": 1,
        "output lookup failed": 1,
        "transaction index out of range": 1,
    }
    # Only aggregate counts may leave the private capture.
    assert LIVE not in str(counts(result)) and counts(result)["unresolved"] == 4


def test_time_budget_leaves_the_rest_unresolved_instead_of_guessing(capture):
    ticks = iter([0, 0, 10, 10, 10, 10, 10])
    result = audit_closed(capture, core, time_budget=5, clock=lambda: next(ticks))
    assert result["not_in_utxo_set"] + result["unspent_at_audit"] == 2
    assert result["unresolved_reasons"] == {"not checked within the time budget": 4}


def test_audit_binds_to_the_exported_index_and_never_overwrites(capture):
    saved = write_audit(capture, core)
    assert load_json(capture / "closed-audit.json") == saved
    assert (capture / "closed-audit.json").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="already exists"):
        write_audit(capture, core)
    (capture / "closed-scids.txt").write_text(SPENT + "\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        audit_closed(capture, core)


def test_summary_reports_audit_counts_and_rejects_a_foreign_audit(capture):
    manifest = load_json(capture / "manifest.json")

    def seal():
        manifest.update(
            export_report_sha256=sha256_file(capture / "export-report.json"),
            closed_audit_sha256=sha256_file(capture / "closed-audit.json"),
        )
        write_json(capture / "manifest.json", manifest)

    write_audit(capture, core)
    seal()
    closed = calculate(capture)[0]["closed_index"]
    assert closed["entries"] == 6 and closed["audit"]["unspent_at_audit"] == 1
    assert "unspent_scids" not in closed["audit"]
    audit = load_json(capture / "closed-audit.json")
    audit["closed_scids_sha256"] = "00" * 32
    write_json(capture / "closed-audit.json", audit)
    seal()
    with pytest.raises(ValueError, match="different index"):
        calculate(capture)
    audit["unspent_at_audit"] = 0  # counts must add up
    write_json(capture / "closed-audit.json", audit)
    seal()
    with pytest.raises(ValueError):
        calculate(capture)
