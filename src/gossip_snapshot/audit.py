"""Check the observer's closed-channel index against Bitcoin Core's UTXO set.

Pinned LND marks a channel closed after any failed funding-output lookup and
never exports it. An entry whose output is still unspent is therefore a live
channel missing from the snapshot. The index itself is never repaired.
"""

import time
from collections import Counter, defaultdict

from .common import sha256_file, utc_now, write_json
from .schema import ClosedAudit, ExportReport, decode_scid, load_json

# Time budget of one audit in seconds. Entries it does not reach are reported as unresolved.
AUDIT_SECONDS = 3600


def audit_closed(capture, rpc, *, time_budget=AUDIT_SECONDS, clock=time.monotonic):
    """Return aggregate counts and the still-unspent entries. `rpc(method, *params)`."""
    report = ExportReport.model_validate(load_json(capture / "export-report.json"))
    path = capture / "closed-scids.txt"
    if sha256_file(path) != report.closed_scids_sha256:
        raise ValueError("closed-channel index hash mismatch")
    scids = path.read_text().split()
    if len(scids) != report.closed_scids or len(set(scids)) != len(scids):
        raise ValueError("closed-channel index count does not reconcile")
    by_height = defaultdict(list)
    for scid in scids:
        by_height[decode_scid(scid)[0]].append(scid)
    tip_height = rpc("getblockcount")
    tip = {"height": tip_height, "hash": rpc("getblockhash", tip_height)}
    deadline = clock() + time_budget
    unspent, absent, reasons = [], 0, Counter()
    for height in sorted(by_height):
        entries = by_height[height]
        if clock() >= deadline:
            reasons["not checked within the time budget"] += len(entries)
            continue
        if height > tip_height:
            reasons["funding height beyond the chain tip"] += len(entries)
            continue
        try:
            transactions = rpc("getblock", rpc("getblockhash", height), 1)["tx"]
        except ValueError:
            reasons["block lookup failed"] += len(entries)
            continue
        for scid in entries:
            _, index, output = decode_scid(scid)
            if index >= len(transactions):
                reasons["transaction index out of range"] += 1
                continue
            try:
                # Confirmed chain state only; mempool spends do not close a channel.
                found = rpc("gettxout", transactions[index], output, False)
            except ValueError:
                reasons["output lookup failed"] += 1
                continue
            if found is None:
                absent += 1
            else:
                unspent.append(scid)
    return ClosedAudit(
        schema_version=1,
        audited_at=utc_now(),
        closed_scids_sha256=report.closed_scids_sha256,
        tip=tip,
        entries=len(scids),
        not_in_utxo_set=absent,
        unspent_at_audit=len(unspent),
        unresolved=sum(reasons.values()),
        unspent_scids=sorted(unspent, key=int),
        unresolved_reasons=dict(sorted(reasons.items())),
    ).model_dump()


def counts(result):
    """The aggregate part that may leave the private capture."""
    return {
        key: result[key]
        for key in ("audited_at", "entries", "not_in_utxo_set", "unspent_at_audit", "unresolved")
    }


def write_audit(capture, rpc, target=None, **options):
    """Audit a capture's closed-channel index and save the result as a private file.

    The default target is the capture's closed-audit.json. An existing file is never replaced.
    """
    target = target or capture / "closed-audit.json"
    if target.exists():
        raise ValueError("closed-index audit already exists; choose a new destination")
    result = audit_closed(capture, rpc, **options)
    write_json(target, result)
    target.chmod(0o600)
    return result
