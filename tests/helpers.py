import json
from pathlib import Path

from gossip_snapshot.analysis import sha256_file, write_json
from gossip_snapshot.fixture import REFERENCE_TIME
from gossip_snapshot.funding_value import SCOPE, amount

# A second observer at an earlier block: channel 0 without an update, channel 1 without
# one as in ROWS, channel 2 with a recent one, and a recent channel that ROWS lacks.
SECOND_OBSERVER = (
    (800000, 0, (None, None)),
    (850000, 1, (None, None)),
    (899000, 2, (REFERENCE_TIME - 60, None)),
    (850500, 5, (REFERENCE_TIME - 60, None)),
)
SECOND_HEIGHT = 899800


def final_sample(capture: Path, path: Path) -> None:
    """Final RPC sample matching a fixture capture, edge i holding 100,000,001 + i sat."""
    manifest = json.loads((capture / "manifest.json").read_text())
    records = [json.loads(line) for line in (capture / "records.jsonl").read_text().splitlines()]
    reference = manifest["reference"]
    edges = [
        {
            "channel_id": r["scid"],
            "chan_point": r["funding_outpoint"],
            "node1_pub": r["node_key_1"],
            "node2_pub": r["node_key_2"],
            "capacity": str(100_000_001 + i),
        }
        for i, r in enumerate(records)
    ]
    write_json(
        path,
        {
            "sampled_at": manifest["captured_at"],
            "getinfo": {"block_height": reference["height"], "block_hash": reference["hash"]},
            "scids": [r["scid"] for r in records],
            "describegraph": {"edges": edges},
        },
    )


def core_audit(value: Path, confirmed, later, not_shown=(0, 0)) -> None:
    """Give a synthetic value summary a Core audit, as (outputs, satoshis) per state.

    Synthetic captures cannot be audited against a node, so tests of what reads the
    audit's outcome write it here.
    """
    summary = json.loads((value / "summary.json").read_text())
    reference = summary["reference"]
    # The audit ran for ten minutes while two blocks arrived.
    tip = {"height": reference["height"] + 2, "hash": "ee" * 32}
    summary["core_audit"] = {
        "started_at": "2026-11-10T01:00:00.250000+00:00",
        "tip_start": reference,
        "finished_at": "2026-11-10T01:10:00.750000+00:00",
        "tip_end": tip,
        "outputs_checked": confirmed[0] + not_shown[0],
        "sources": {
            "funding_transaction": later[0] + not_shown[0],
            "unspent_output": confirmed[0] - later[0],
        },
        "blocks_scanned_after_reference": 2,
        "snapshot_unspentness": {
            "unspent_when_checked": amount(confirmed[0] - later[0], confirmed[1] - later[1]),
            "spent_after_reference": amount(*later),
            "not_shown": amount(*not_shown),
        },
        "confirmed_unspent_at_reference": amount(*confirmed),
        "scope": SCOPE,
    }
    write_json(value / "summary.json", summary)
    write_json(value / "checksums.json", {"summary.json": sha256_file(value / "summary.json")})
