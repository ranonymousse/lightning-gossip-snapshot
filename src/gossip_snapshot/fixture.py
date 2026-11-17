"""Deterministic signed test data; these keys must never hold real funds."""

import hashlib
import json
from pathlib import Path

from coincurve import PrivateKey
from coincurve.ecdsa import der_to_cdata, serialize_compact

from .common import sha256_file, write_json
from .schema import LND_COMMIT, MAINNET, YEAR_BLOCKS

REFERENCE_TIME = 1794268800  # 2026-11-10T00:00:00Z, the fixture's capture time.
DAY = 24 * 3600
# (funding height, index, update times): one recent update, two stale updates, and no
# stored update at all. Index 3 is the unsupported record that every fixture adds.
ROWS = (
    (800000, 0, (REFERENCE_TIME - 3600, None)),
    (850000, 1, (REFERENCE_TIME - 20 * DAY, REFERENCE_TIME - 15 * DAY)),
    (899000, 2, (None, None)),
)


# (share of the announcements, age in blocks): the sample's ages follow straight lines
# between these points, so half are younger than one nominal year and a tenth older than
# four. The shape is made up. It resembles no capture.
SAMPLE_AGES = ((0.0, 150), (0.5, YEAR_BLOCKS), (0.9, 4 * YEAR_BLOCKS), (1.0, 8 * YEAR_BLOCKS))


def sample_rows(count: int) -> tuple:
    """Rows for a larger synthetic capture whose age distribution can be published.

    Funding heights and update times follow fixed arithmetic, so the same count always
    gives the same capture. Four in five announcements carry a recent update.
    """
    rows = []
    for index in range(count):
        share = ((index * 7919) % 1000) / 1000
        for (low, young), (high, old) in zip(SAMPLE_AGES, SAMPLE_AGES[1:], strict=False):
            if low <= share <= high:
                age = young + int((old - young) * (share - low) / (high - low))
                break
        recent = index % 5 != 4
        updates = (REFERENCE_TIME - (1 + index % 13) * DAY if recent else None, None)
        # Index 3 belongs to the unsupported record that every fixture adds.
        rows.append((900000 - age, 10 + index, updates))
    return tuple(rows)


def signed_record(
    height: int,
    index: int,
    *,
    features: bytes = b"",
    extra: bytes = b"",
    updates: tuple[int | None, int | None] = (None, None),
) -> dict:
    """One exported channel announcement, signed with four public test keys."""
    # Deliberately public, deterministic test secrets. Reuse a funding key to
    # test distinct-key counting rather than assuming two new keys per edge.
    nodes = sorted(
        [PrivateKey.from_int(1), PrivateKey.from_int(2)],
        key=lambda key: key.public_key.format(),
    )
    funding = [PrivateKey.from_int(3), PrivateKey.from_int(10 + index)]
    keys = nodes + funding
    scid = (height << 40) | (index << 16) | 1
    body = (
        len(features).to_bytes(2, "big")
        + features
        + bytes.fromhex(MAINNET)[::-1]
        + scid.to_bytes(8, "big")
        + b"".join(key.public_key.format() for key in keys)
        + extra
    )
    digest = hashlib.sha256(hashlib.sha256(body).digest()).digest()
    sigs = b"".join(serialize_compact(der_to_cdata(key.sign(digest, hasher=None))) for key in keys)
    return {
        "schema_version": 2,
        "provenance": "reconstructed_from_lnd_graph",
        "gossip_version": 1,
        "chain_hash": MAINNET,
        "scid": str(scid),
        "funding_outpoint": hashlib.sha256(f"fixture-{index}".encode()).hexdigest() + ":1",
        "node_key_1": nodes[0].public_key.format().hex(),
        "node_key_2": nodes[1].public_key.format().hex(),
        "bitcoin_key_1": funding[0].public_key.format().hex(),
        "bitcoin_key_2": funding[1].public_key.format().hex(),
        "announcement_hex": (b"\x01\x00" + sigs + body).hex(),
        "signature_verified": True,
        "funding_construction": "unsupported" if any(features) else "p2wsh",
        "exclusion_reason": "unsupported_features" if any(features) else None,
        "last_update_1": updates[0],
        "last_update_2": updates[1],
    }


def make_fixture(target: Path, rows=ROWS, height: int = 900000) -> None:
    """Signed capture of one synthetic observer at block `height`, holding `rows`."""
    target.mkdir(parents=True, exist_ok=False)
    records = [signed_record(funded, i, updates=updates) for funded, i, updates in rows]
    records.append(signed_record(height - 500, 3, features=b"\x02"))
    closed = str((700000 << 40) | (7 << 16))
    (target / "closed-scids.txt").write_text(closed + "\n")
    write_json(
        target / "closed-audit.json",
        {
            "schema_version": 1,
            "audited_at": "2026-11-10T00:00:00Z",
            "closed_scids_sha256": sha256_file(target / "closed-scids.txt"),
            "tip": {"height": height, "hash": "11" * 32},
            "entries": 1,
            "not_in_utxo_set": 1,
            "unspent_at_audit": 0,
            "unresolved": 0,
            "unspent_scids": [],
            "unresolved_reasons": {},
        },
    )
    (target / "records.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in records)
    )
    reference = {"height": height, "hash": "11" * 32}
    write_json(
        target / "export-report.json",
        {
            "schema_version": 2,
            "lnd_commit": LND_COMMIT,
            "database_sha256": "00" * 32,
            "records_sha256": sha256_file(target / "records.jsonl"),
            "graph_prune_tip": reference,
            "exported_records": len(records),
            "included_records": len(rows),
            "exclusions": {"unsupported_features": 1},
            "closed_scids": 1,
            "closed_scids_sha256": sha256_file(target / "closed-scids.txt"),
        },
    )
    write_json(
        target / "manifest.json",
        {
            "schema_version": 1,
            "dataset_kind": "synthetic",
            "captured_at": "2026-11-10T00:00:00Z",
            "reference": reference,
            "backend": "fixture",
            "validation_enabled_from_start": False,
            "clean_shutdown": False,
            "records_sha256": sha256_file(target / "records.jsonl"),
            "export_report_sha256": sha256_file(target / "export-report.json"),
            "closed_audit_sha256": sha256_file(target / "closed-audit.json"),
        },
    )
