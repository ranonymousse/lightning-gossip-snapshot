"""Exact-value accounting and script matching on synthetic snapshots only."""

import hashlib
import io
import json
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from gossip_snapshot import cli
from gossip_snapshot.analysis import sha256_file, write_json
from gossip_snapshot.fixture import make_fixture
from gossip_snapshot.funding_value import (
    audit_core,
    core_satoshis,
    funding_script,
    joined_snapshot,
    measure_value,
    verify_value,
)
from gossip_snapshot.schema import decode_scid, load_json


@pytest.fixture
def snapshot(tmp_path):
    capture, sample = tmp_path / "capture", tmp_path / "final-sample.json"
    make_fixture(capture)
    manifest = load_json(capture / "manifest.json")
    records = [json.loads(s) for s in (capture / "records.jsonl").read_text().splitlines()]
    write_json(
        sample,
        {
            "sampled_at": manifest["captured_at"],
            "getinfo": {"block_height": 900000, "block_hash": "11" * 32},
            "scids": [r["scid"] for r in records],
            "describegraph": {
                "edges": [
                    {
                        "channel_id": r["scid"],
                        "chan_point": r["funding_outpoint"],
                        "node1_pub": r["node_key_1"],
                        "node2_pub": r["node_key_2"],
                        "capacity": str(100_000_001 + i),
                    }
                    for i, r in enumerate(records)
                ]
            },
            # Deliberately wrong. This aggregate must never supply our value total.
            "getnetworkinfo": {"total_network_capacity": "999999999999"},
        },
    )
    return capture, sample, tmp_path / "value"


def test_counts_unique_outputs_not_keys_or_rpc_network_summary(snapshot):
    capture, sample, output = snapshot
    result = measure_value(capture, sample, output, workers=1)
    # Three eligible outputs, one excluded feature vector, one reused funding key.
    assert result["included_outputs"] == 3
    assert result["funding_value_satoshis"] == 300_000_006
    assert result["funding_value_btc"] == "3.00000006"
    assert result["dataset_kind"] == "synthetic" and result["core_audit"] is None
    # The fixture has one recent update, one stale pair and one channel without any update.
    assert result["by_update_recency"] == {
        "window_seconds": 14 * 24 * 3600,
        "recent_update_observed": {
            "outputs": 1,
            "funding_value_satoshis": 100_000_001,
            "funding_value_btc": "1.00000001",
        },
        "no_recent_update_observed": {
            "outputs": 2,
            "funding_value_satoshis": 200_000_005,
            "funding_value_btc": "2.00000005",
        },
    }
    assert verify_value(capture, sample, output, workers=1) == result
    assert {p.name for p in output.iterdir()} == {"summary.json", "checksums.json"}
    _, rows = joined_snapshot(capture, sample, workers=1)
    public = (output / "summary.json").read_text()
    for record, _ in rows:
        assert record.funding_outpoint not in public and record.bitcoin_key_1 not in public
    with pytest.raises(ValueError, match="exists"):
        measure_value(capture, sample, output, workers=1)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "missing",
        "outpoint",
        "node",
        "tip",
        "time",
        "float",
        "negative",
        "zero",
        "huge",
    ],
)
def test_inconsistent_sample_never_produces_a_value(snapshot, case):
    capture, sample, output = snapshot
    data = load_json(sample)
    edges = data["describegraph"]["edges"]
    if case == "duplicate":
        edges.append(deepcopy(edges[0]))
    elif case == "missing":
        edges.pop()
    elif case == "outpoint":
        edges[0]["chan_point"] = "00" * 32 + ":1"
    elif case == "node":
        edges[0]["node1_pub"] = edges[0]["node2_pub"]
    elif case == "tip":
        data["getinfo"]["block_height"] += 1
    elif case == "time":
        data["sampled_at"] = "2026-11-10T00:01:00Z"
    else:
        edges[0]["capacity"] = {
            "float": 100.1,
            "negative": "-1",
            "zero": "0",
            "huge": "2100000000000001",
        }[case]
    write_json(sample, data)
    with pytest.raises(ValueError):
        measure_value(capture, sample, output, workers=1)
    assert not output.exists()


def test_rehashed_false_total_and_changed_sample_are_rejected(snapshot):
    capture, sample, output = snapshot
    measure_value(capture, sample, output, workers=1)
    result = load_json(output / "summary.json")
    result["funding_value_satoshis"] += 1
    write_json(output / "summary.json", result)
    write_json(output / "checksums.json", {"summary.json": sha256_file(output / "summary.json")})
    with pytest.raises(ValueError, match="recomputed"):
        verify_value(capture, sample, output, workers=1)


def fake_core(rows, *, spent_at=900004, **faults):
    """A node ten blocks past the reference in which the first channel has closed.

    `spent_at` is the block that spends its funding output, or None when no block after
    the reference does. A fault makes the node answer wrongly in one respect.
    """
    assert set(faults) <= {
        "wrong_script",
        "wrong_value",
        "reorg",
        "fork",
        "stale_tip",
        "no_undo",
        "other_coin",
    }
    by_txid = {r.funding_outpoint.split(":")[0]: (r, value) for r, value in rows}
    closed_txid, closed_index = rows[0][0].funding_outpoint.split(":")
    ref_calls = 0

    def block_hash(height):
        return "11" * 32 if height == 900000 else f"{height:064x}"

    def output_of(txid, *, true=False):
        record, amount = by_txid[txid]
        # Build the expected script independently of the implementation under test.
        keys = sorted((record.bitcoin_key_1, record.bitcoin_key_2))
        script = bytes.fromhex("5221" + keys[0] + "21" + keys[1] + "52ae")
        output = {
            "value": Decimal(amount + (not true and "wrong_value" in faults)) / 100_000_000,
            "scriptPubKey": {"hex": "0020" + hashlib.sha256(script).hexdigest()},
        }
        if "wrong_script" in faults and not true:
            output["scriptPubKey"]["hex"] = "0020" + "00" * 32
        return record, output

    def rpc(method, *args):
        nonlocal ref_calls
        if method == "getblockchaininfo":
            return {"chain": "main", "initialblockdownload": False}
        if method == "getbestblockhash":
            return block_hash(900010)
        if method == "getblockhash":
            if args[0] == 900000:
                ref_calls += 1
                return "22" * 32 if "reorg" in faults and ref_calls > 1 else "11" * 32
            return block_hash(args[0])
        if method == "getblockheader":
            # The reference block is never asked for: ancestry is walked back to it.
            if args[0] in ("33" * 32, "55" * 32):
                # Blocks of a competing chain that does not contain the reference block.
                return {"height": 900000, "previousblockhash": "44" * 32}
            height = int(args[0], 16)
            assert 900000 < height <= 900010
            parent = "33" * 32 if "fork" in faults and height == 900002 else block_hash(height - 1)
            return {"height": height, "previousblockhash": parent}
        if method == "getblock":
            assert args[1] == 3
            height = int(args[0], 16)
            transactions = [{"vin": [{"coinbase": "00", "sequence": 0}]}]
            if height == spent_at:
                record, output = output_of(closed_txid, true=True)
                funded = decode_scid(record.scid)[0] + ("other_coin" in faults)
                spend = {"txid": closed_txid, "vout": int(closed_index)}
                if "no_undo" not in faults:
                    spend["prevout"] = {"generated": False, "height": funded, **output}
                # Another input of the same transaction and a different output index
                # of the funding transaction must not be mistaken for the spend.
                transactions.append(
                    {
                        "vin": [
                            {"txid": "ab" * 32, "vout": int(closed_index)},
                            {"txid": closed_txid, "vout": int(closed_index) + 1},
                            spend,
                        ]
                    }
                )
            return {"hash": args[0], "height": height, "tx": transactions}
        record, output = output_of(args[0])
        if method == "gettxout":
            assert args[2] is False
            if record == rows[0][0]:
                return None  # Closed since capture, so the historical fallback is needed.
            return {
                **output,
                "bestblock": "55" * 32 if "stale_tip" in faults else block_hash(900010),
                "confirmations": 900010 - decode_scid(record.scid)[0] + 1,
            }
        assert method == "getrawtransaction" and args[1] is True
        return {"txid": args[0], "blockhash": args[2], "vout": [{}, {**output, "n": 1}]}

    return rpc


def test_core_checks_scripts_and_exact_values_even_if_channel_closed_later(snapshot):
    capture, sample, _ = snapshot
    summary, rows = joined_snapshot(capture, sample, workers=1)
    evidence = io.StringIO()
    audit = audit_core(rows, summary["reference"], fake_core(rows), evidence, lambda *_: None)
    assert audit["outputs_checked"] == 3
    assert audit["sources"] == {"funding_transaction": 1, "unspent_output": 2}
    assert len(evidence.getvalue().splitlines()) == 3
    assert audit["scope"] == "scripts_values_and_unspentness_at_reference"
    # Sorting the funding keys, rather than their announcement order, is essential.
    record = rows[0][0]
    swapped = record.model_copy(
        update={"bitcoin_key_1": record.bitcoin_key_2, "bitcoin_key_2": record.bitcoin_key_1}
    )
    assert funding_script(record) == funding_script(swapped)
    # A wrong answer, or one from a chain without the reference block, yields no audit.
    for option, message in (
        ("wrong_script", "script/value"),
        ("wrong_value", "script/value"),
        ("reorg", "reference changed"),
        ("fork", "without the capture reference"),
        ("stale_tip", "without the capture reference"),
        ("no_undo", "undo data"),
        ("other_coin", "differs from the authenticated cohort"),
    ):
        with pytest.raises(ValueError, match=message):
            audit_core(
                rows,
                summary["reference"],
                fake_core(rows, **{option: True}),
                io.StringIO(),
                lambda *_: None,
            )


def test_core_shows_each_output_unspent_at_the_reference_block(snapshot):
    capture, sample, _ = snapshot
    summary, rows = joined_snapshot(capture, sample, workers=1)
    closed_value = rows[0][1]
    stages = []

    def run(**options):
        evidence = io.StringIO()
        audit = audit_core(
            rows,
            summary["reference"],
            fake_core(rows, **options),
            evidence,
            lambda done, total, stage: stages.append((stage, done, total)),
        )
        return audit, [json.loads(line) for line in evidence.getvalue().splitlines()]

    # Two outputs are still unspent, and the third was spent four blocks after the reference:
    # all three were unspent at the reference block.
    audit, evidence = run()
    states = audit["snapshot_unspentness"]
    assert states["unspent_when_checked"]["outputs"] == 2
    assert states["spent_after_reference"] == {
        "outputs": 1,
        "funding_value_satoshis": closed_value,
        "funding_value_btc": f"{closed_value // 10**8}.{closed_value % 10**8:08d}",
    }
    assert states["not_shown"]["outputs"] == 0
    confirmed = audit["confirmed_unspent_at_reference"]
    assert confirmed["outputs"] == 3
    assert confirmed["funding_value_satoshis"] == summary["funding_value_satoshis"]
    assert [row["spent_block"] for row in evidence] == [
        {"height": 900004, "hash": f"{900004:064x}"},
        None,
        None,
    ]
    # The search stops at the block that holds the last missing spend.
    assert audit["blocks_scanned_after_reference"] == 4
    assert stages[-1] == ("blocks", 4, 10) and ("outputs", 3, 3) in stages

    # No block after the reference spends it: the node held a channel that had already
    # closed. That is reported with its value and never counted as confirmed.
    audit, evidence = run(spent_at=None)
    assert audit["blocks_scanned_after_reference"] == 10
    assert audit["snapshot_unspentness"]["not_shown"]["funding_value_satoshis"] == closed_value
    assert audit["snapshot_unspentness"]["spent_after_reference"]["outputs"] == 0
    confirmed = audit["confirmed_unspent_at_reference"]
    assert confirmed["outputs"] == 2
    assert confirmed["funding_value_satoshis"] == (summary["funding_value_satoshis"] - closed_value)
    assert all(row["spent_block"] is None for row in evidence)


def test_decimal_amounts_preserve_single_satoshis_and_reject_floats():
    assert core_satoshis(Decimal("20999999.99999999")) == 2099999999999999
    assert core_satoshis(Decimal("0.00000001")) == 1
    for invalid in (0.1, True, Decimal("0.000000001"), Decimal("NaN")):
        with pytest.raises(ValueError):
            core_satoshis(invalid)


def test_audited_artifact_replays_offline_and_failure_leaves_no_result(snapshot):
    capture, sample, output = snapshot
    # Synthetic mainnet-shaped test fixture: no live RPC or real funding keys.
    data = load_json(sample)
    data["getinfo"].update(synced_to_chain=True, synced_to_graph=True, num_peers=3)
    write_json(sample, data)
    samples = []
    for minutes in (60, 30, 0):
        observation = deepcopy(data)
        observation["sampled_at"] = (
            (datetime.fromisoformat(data["sampled_at"]) - timedelta(minutes=minutes))
            .isoformat()
            .replace("+00:00", "Z")
        )
        samples.append(observation)
    write_json(capture / "readiness.json", samples)
    manifest = load_json(capture / "manifest.json")
    manifest.update(
        dataset_kind="mainnet",
        backend="bitcoind",
        clean_shutdown=True,
        validation_enabled_from_start=True,
        readiness_sha256=sha256_file(capture / "readiness.json"),
    )
    write_json(capture / "manifest.json", manifest)
    _, rows = joined_snapshot(capture, sample, workers=1)
    with pytest.raises(ValueError, match="script/value"):
        measure_value(capture, sample, output, rpc=fake_core(rows, wrong_value=True), workers=1)
    assert not output.exists()
    result = measure_value(capture, sample, output, rpc=fake_core(rows), workers=1)
    assert result["core_audit"]["confirmed_unspent_at_reference"]["outputs"] == 3
    assert verify_value(capture, sample, output, workers=1) == result
    evidence = output / "core-evidence.jsonl"
    original = evidence.read_text()

    def rewrite(change):
        lines = original.splitlines()
        row = json.loads(lines[0])
        change(row)
        lines[0] = json.dumps(row)
        evidence.write_text("\n".join(lines) + "\n")
        checksums = load_json(output / "checksums.json")
        checksums[evidence.name] = sha256_file(evidence)
        write_json(output / "checksums.json", checksums)

    # Rehashed evidence that no longer supports the saved audit is rejected offline.
    for change, message in (
        (lambda row: row.update(script_hex="0020" + "00" * 32), "script/value"),
        (lambda row: row.update(funding_height=row["funding_height"] + 1), "script/value"),
        (lambda row: row.update(note="unexpected"), "script/value"),
        # A spend at the reference block would not show the output unspent there.
        (lambda row: row["spent_block"].update(height=900000), "follow the reference"),
        (lambda row: row["spent_block"].update(height=900011), "beyond the audited"),
        (lambda row: row.update(spent_block=None), "do not reconcile"),
        (lambda row: row.update(source="unspent_output"), "cannot carry"),
    ):
        rewrite(change)
        with pytest.raises(ValueError, match=message):
            verify_value(capture, sample, output, workers=1)
    rewrite(lambda row: None)
    assert verify_value(capture, sample, output, workers=1) == result
    evidence.write_text(original.replace('"spent_block":', '"source": "x", "spent_block":', 1))
    checksums = load_json(output / "checksums.json")
    checksums[evidence.name] = sha256_file(evidence)
    write_json(output / "checksums.json", checksums)
    with pytest.raises(ValueError, match="duplicate field"):
        verify_value(capture, sample, output, workers=1)
    rewrite(lambda row: None)
    # The audit summary must match what the evidence supports, block count included.
    for key, value, message in (
        ("blocks_scanned_after_reference", 3, "do not reconcile"),
        ("scope", "scripts_and_values", "unexpected Core audit summary"),
        ("note", "unexpected", "unexpected Core audit summary"),
    ):
        stored = load_json(output / "summary.json")
        stored["core_audit"][key] = value
        write_json(output / "summary.json", stored)
        checksums = load_json(output / "checksums.json")
        checksums["summary.json"] = sha256_file(output / "summary.json")
        write_json(output / "checksums.json", checksums)
        with pytest.raises(ValueError, match=message):
            verify_value(capture, sample, output, workers=1)
        write_json(output / "summary.json", result)
        checksums["summary.json"] = sha256_file(output / "summary.json")
        write_json(output / "checksums.json", checksums)
    assert verify_value(capture, sample, output, workers=1) == result
    # An artifact in another format than the current one is refused.
    stored = load_json(output / "summary.json")
    stored["schema_version"] = 1
    write_json(output / "summary.json", stored)
    with pytest.raises(ValueError, match="create it again"):
        verify_value(capture, sample, output, workers=1)


def test_value_cli_is_offline_by_default_and_rechecks_artifact(snapshot):
    capture, sample, output = snapshot
    args = [
        "--capture",
        str(capture),
        "--sample",
        str(sample),
        "--output",
        str(output),
        "--workers",
        "1",
    ]
    runner = CliRunner()
    result = runner.invoke(cli.app, ["funding-value", *args])
    assert result.exit_code == 0, result.output
    assert '"funding_value_btc": "3.00000006"' in result.stdout
    verified = runner.invoke(cli.app, ["verify-value", *args])
    assert verified.exit_code == 0, verified.output
    assert "1.00000001 BTC of it in the 1 with a recent update observed" in " ".join(
        verified.stdout.split()
    )
