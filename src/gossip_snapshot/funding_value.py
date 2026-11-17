"""Reproducible value of authenticated funding outputs, with an optional Core audit.

The value is measured after collection and never alters a capture or its readiness.
The folder it writes is private: the Core evidence holds funding outpoints and scripts.
`publish` writes the part of the summary that may be shared.
"""

import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .analysis import calculate
from .common import sha256_file, write_json
from .schema import Record, decode_scid, load_json

SATOSHIS_PER_BTC = 100_000_000
# Bitcoin's supply bound. No output and no sum of outputs can hold more.
MAX_SATOSHIS = 21_000_000 * SATOSHIS_PER_BTC
VALUE_VERSION = 2  # Format of the value summary. An artifact in another format is refused.
# The two groups of the main analysis, in its fixed wording.
RECENCY = ("recent_update_observed", "no_recent_update_observed")
# What the Core evidence shows about an output at the reference block. The first two
# establish that it was unspent there.
STATES = ("unspent_when_checked", "spent_after_reference", "not_shown")
SCOPE = "scripts_values_and_unspentness_at_reference"
EVIDENCE_KEYS = {
    "scid",
    "outpoint",
    "script_hex",
    "value_satoshis",
    "source",
    "block_hash",
    "funding_height",
    "spent_block",
}
AUDIT_KEYS = {
    "started_at",
    "tip_start",
    "finished_at",
    "tip_end",
    "outputs_checked",
    "sources",
    "blocks_scanned_after_reference",
    "snapshot_unspentness",
    "confirmed_unspent_at_reference",
    "scope",
}
# The note is part of every stored value summary, and a summary is compared in full when it
# is verified, so its wording stays as it is. `publish` writes a note of its own.
NOTE = (
    "Funding value in the authenticated snapshot cohort, conditional on recovery of both "
    "funding private keys while the output remains spendable under those keys. Not a "
    "network census, a demonstrated attack or a forecast of losses. Without a Core audit, "
    "unspentness at the reference block relies on LND's validated graph. The audit shows it "
    "per output: one still unspent when checked was unspent at the reference block, and for "
    "one spent since, the spending input is located in a later block. Publish summary.json "
    "only: core-evidence.jsonl is private."
)


def satoshis(value):
    """Accept only exact, canonical integer amounts from the saved LND graph."""
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("capacity must be a positive integer number of satoshis")
    result = int(value)
    if result > MAX_SATOSHIS:
        raise ValueError("capacity exceeds Bitcoin's supply bound")
    return result


def btc(value):
    """Satoshis as a decimal BTC string with eight places, without floating point."""
    return f"{value // SATOSHIS_PER_BTC}.{value % SATOSHIS_PER_BTC:08d}"


def amount(outputs, value):
    """A number of outputs with their summed value, in satoshis and in BTC."""
    return {"outputs": outputs, "funding_value_satoshis": value, "funding_value_btc": btc(value)}


def core_satoshis(value):
    """A BTC amount from Bitcoin Core as satoshis.

    Binary floats are refused: the RPC client must decode amounts as exact decimals.
    """
    if isinstance(value, (bool, float)):
        raise ValueError("Core amounts must be parsed as exact decimals")
    try:
        exact = Decimal(value) * SATOSHIS_PER_BTC
    except (ArithmeticError, TypeError):
        raise ValueError("Core amount is not a decimal number") from None
    if not exact.is_finite() or exact != exact.to_integral_value():
        raise ValueError("Core amount is not an exact number of satoshis")
    return satoshis(int(exact))


def funding_script(record):
    """BOLT3 sorted 2-of-2 witness script, wrapped in a v0 SHA256 witness program."""
    a, b = sorted(bytes.fromhex(key) for key in (record.bitcoin_key_1, record.bitcoin_key_2))
    witness = b"\x52\x21" + a + b"\x21" + b + b"\x52\xae"
    return (b"\x00\x20" + hashlib.sha256(witness).digest()).hex()


def recency_threshold(checked):
    """Earliest update time that the main analysis counts as recent."""
    recency = checked["counts"]["update_recency"]
    reference_time = datetime.fromisoformat(recency["reference_time"].replace("Z", "+00:00"))
    return int(reference_time.timestamp()) - recency["window_seconds"]


def recency_group(record, recent_after):
    """The update-recency group of a record, by the later of its two stored update times."""
    updates = [t for t in (record.last_update_1, record.last_update_2) if t is not None]
    return RECENCY[not (updates and max(updates) >= recent_after)]


def joined_snapshot(capture, sample_path, workers=0):
    """Match a capture to the node's final sample and sum the funding values, each output once.

    Every exported record must appear in the sample with the same outpoint and node keys,
    and the sample must hold no other channel. Returns the value summary, still without
    an audit, and the included records with their values in capture order.
    """
    # This verifies all signatures, hashes, exclusions and capture readiness first.
    checked, _ = calculate(capture, workers=workers)
    sample_hash = sha256_file(sample_path)
    sample = load_json(sample_path)
    info = sample["getinfo"]
    if {"height": info["block_height"], "hash": info["block_hash"]} != checked[
        "reference"
    ] or sample["sampled_at"] != checked["captured_at"]:
        raise ValueError("RPC sample time/reference differs from the capture")
    edges = sample["describegraph"]["edges"]
    by_id = {e["channel_id"]: e for e in edges}
    if len(by_id) != len(edges) or len({e["chan_point"] for e in edges}) != len(edges):
        raise ValueError("duplicate RPC channel ID or funding outpoint")
    recency = checked["counts"]["update_recency"]
    recent_after = recency_threshold(checked)
    rows, ids, groups = [], set(), {name: [0, 0] for name in RECENCY}
    with (capture / "records.jsonl").open() as stream:
        for line in stream:
            record = Record.model_validate_json(line)
            ids.add(record.scid)
            edge = by_id.get(record.scid)
            if edge is None or (edge["chan_point"], edge["node1_pub"], edge["node2_pub"]) != (
                record.funding_outpoint,
                record.node_key_1,
                record.node_key_2,
            ):
                raise ValueError("RPC channel does not match the signed export")
            if record.exclusion_reason is None:
                value = satoshis(edge["capacity"])
                rows.append((record, value))
                group = groups[recency_group(record, recent_after)]
                group[0] += 1
                group[1] += value
    if ids != set(by_id) or ids != set(sample["scids"]) or len(sample["scids"]) != len(ids):
        raise ValueError("RPC channel set differs from the signed export")
    if sha256_file(sample_path) != sample_hash or (
        sha256_file(capture / "records.jsonl") != checked["inputs"]["records_sha256"]
    ):
        raise ValueError("inputs changed during value analysis")
    # The value groups must be the very groups the main analysis counted.
    if any(groups[name][0] != recency[name]["channels"] for name in RECENCY):
        raise ValueError("update-recency groups differ from the main analysis")
    total = sum(value for _, value in rows)
    if total > MAX_SATOSHIS:
        raise ValueError("total funding value exceeds Bitcoin's supply bound")
    return {
        "schema_version": VALUE_VERSION,
        "dataset_kind": checked["dataset_kind"],
        "captured_at": checked["captured_at"],
        "reference": checked["reference"],
        "included_outputs": len(rows),
        "funding_value_satoshis": total,
        "funding_value_btc": btc(total),
        "by_update_recency": {
            "window_seconds": recency["window_seconds"],
            **{name: amount(*groups[name]) for name in RECENCY},
        },
        "value_source": "matched_per_output_lnd_capacities",
        "core_audit": None,
        "inputs": {**checked["inputs"], "final_sample_sha256": sample_hash},
        "interpretation": NOTE,
    }, rows


def block_id(value):
    """Whether a value has the form of a block hash."""
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


class ReferenceChain:
    """Blocks shown to build on the capture's reference block, with their heights.

    Ancestry never changes. A reply that Core ties to one of these blocks therefore
    describes a chain that contains the reference block, whatever the active chain does
    while the audit runs.
    """

    def __init__(self, reference, rpc):
        self.reference, self.rpc = reference, rpc
        self.blocks = {reference["hash"]: (reference["height"], None)}

    def height(self, block_hash):
        """The height of a block, once its ancestors lead back to the reference block."""
        path, cursor = [], block_hash
        while cursor not in self.blocks:
            header = self.rpc("getblockheader", cursor)
            if header["height"] <= self.reference["height"]:
                raise ValueError(
                    "Core answered from a chain without the capture reference; run the audit again"
                )
            path.append((cursor, header["height"], header["previousblockhash"]))
            cursor = header["previousblockhash"]
        expected = self.blocks[cursor][0]
        for block, height, parent in reversed(path):
            expected += 1
            if height != expected:
                raise ValueError("Core returned inconsistent block heights")
            self.blocks[block] = (height, parent)
        return self.blocks[block_hash][0]

    def tip(self):
        """Core's best block, which must build on the reference block."""
        block_hash = self.rpc("getbestblockhash")
        return {"height": self.height(block_hash), "hash": block_hash}

    def after_reference(self, block_hash):
        """The blocks after the reference block up to this one, oldest first."""
        self.height(block_hash)
        path = []
        while block_hash != self.reference["hash"]:
            path.append(block_hash)
            block_hash = self.blocks[block_hash][1]
        return path[::-1]


def check_evidence(row, record, value):
    """Require an evidence row to show the output that the cohort expects.

    That is the script of the two announced funding keys, the recorded value and the
    funding height that the channel identifier encodes.
    """
    if (
        set(row) != EVIDENCE_KEYS
        or row["scid"] != record.scid
        or row["outpoint"] != record.funding_outpoint
        or row["script_hex"] != funding_script(record)
        or satoshis(row["value_satoshis"]) != value
        or row["source"] not in ("unspent_output", "funding_transaction")
        or row["funding_height"] != decode_scid(record.scid)[0]
        or not block_id(row["block_hash"])
    ):
        raise ValueError("Core output script/value/height does not match the authenticated cohort")


def snapshot_state(row, reference):
    """What one evidence row shows about its output at the reference block."""
    spent = row["spent_block"]
    if row["source"] == "unspent_output":
        if spent is not None:
            raise ValueError("an unspent output cannot carry a spending block")
        # Funded by the reference block and still unspent later on the same chain.
        return "unspent_when_checked"
    if spent is None:
        return "not_shown"
    if (
        type(spent) is not dict
        or set(spent) != {"height", "hash"}
        or type(spent["height"]) is not int
        or spent["height"] <= reference["height"]
        or not block_id(spent["hash"])
    ):
        raise ValueError("spending block does not follow the reference block")
    return "spent_after_reference"


def unspentness(evidence, rows, reference):
    """Outputs and value per state at the reference block, and the total shown unspent there."""
    states = {name: [0, 0] for name in STATES}
    for row, (_, value) in zip(evidence, rows, strict=True):
        state = states[snapshot_state(row, reference)]
        state[0] += 1
        state[1] += value
    confirmed = [sum(states[name][part] for name in STATES[:2]) for part in (0, 1)]
    return {
        "snapshot_unspentness": {name: amount(*states[name]) for name in STATES},
        "confirmed_unspent_at_reference": amount(*confirmed),
    }


def spends_after(chain, end, pending, rpc, progress):
    """Find the blocks after the reference block that spend the pending outputs.

    The blocks are read along their ancestry from the reference block. Each spend must
    name, from the block's own undo data, the output the cohort expects: its funding
    height, script and value. An output funded by the reference block and spent in such
    a block was unspent at the reference block.
    """
    found, scanned = {}, 0
    blocks = chain.after_reference(end["hash"])
    for block_hash in blocks:
        if len(found) == len(pending):
            break
        block = rpc("getblock", block_hash, 3)
        if block["hash"] != block_hash:
            raise ValueError("Core returned a different block")
        for transaction in block["tx"]:
            for spent in transaction["vin"]:
                row = pending.get(f"{spent.get('txid')}:{spent.get('vout')}")
                if row is None:
                    continue
                previous = spent.get("prevout")
                if previous is None:
                    raise ValueError("Core has no undo data for a spending block; is it pruned?")
                if (
                    previous["height"],
                    previous["scriptPubKey"]["hex"],
                    core_satoshis(previous["value"]),
                ) != (row["funding_height"], row["script_hex"], row["value_satoshis"]):
                    raise ValueError("spent output differs from the authenticated cohort")
                found[row["outpoint"]] = {"height": chain.height(block_hash), "hash": block_hash}
        scanned += 1
        progress(scanned, len(blocks), "blocks")
    return found, scanned


def audit_core(rows, reference, rpc, stream, progress):
    """Check every funding output against a synchronized Bitcoin Core and write the evidence.

    An output that is still unspent is read from the set of unspent outputs. A spent one
    is read from its funding transaction, and its spend is then located in the blocks
    after the reference block. Returns the audit's part of the value summary.
    """
    info = rpc("getblockchaininfo")
    if info["chain"] != "main" or info["initialblockdownload"]:
        raise ValueError("Core must be synchronized on mainnet")
    if rpc("getblockhash", reference["height"]) != reference["hash"]:
        raise ValueError("capture reference is not on Core's active chain")
    chain = ReferenceChain(reference, rpc)
    audit = {"started_at": datetime.now(UTC).isoformat(), "tip_start": chain.tip()}
    sources, evidence, spent = Counter(), [], {}
    for number, (record, value) in enumerate(rows, 1):
        txid, index = record.funding_outpoint.split(":")
        height, _, _ = decode_scid(record.scid)
        output = rpc("gettxout", txid, int(index), False)
        if output is not None:
            source = "unspent_output"
            # Core reports the coin together with the chain tip it belongs to.
            block_hash = output["bestblock"]
            funded = chain.height(block_hash) - output["confirmations"] + 1
        else:
            # Channels may close after capture. Verify their original script/value
            # from the funding transaction. Whether they closed after the reference
            # block is settled below, from the blocks that follow it.
            source = "funding_transaction"
            block_hash = rpc("getblockhash", height)
            transaction = rpc("getrawtransaction", txid, True, block_hash)
            if transaction["txid"] != txid or transaction["blockhash"] != block_hash:
                raise ValueError("Core returned a different funding transaction")
            if int(index) >= len(transaction["vout"]):
                raise ValueError("Core funding transaction has no such output")
            output = transaction["vout"][int(index)]
            if output["n"] != int(index):
                raise ValueError("Core returned a different output index")
            funded = height
        row = {
            "scid": record.scid,
            "outpoint": record.funding_outpoint,
            "script_hex": output["scriptPubKey"]["hex"],
            "value_satoshis": core_satoshis(output["value"]),
            "source": source,
            "block_hash": block_hash,
            "funding_height": funded,
            "spent_block": None,
        }
        check_evidence(row, record, value)
        evidence.append(row)
        if source == "funding_transaction":
            spent[record.funding_outpoint] = row
        sources[source] += 1
        progress(number, len(rows), "outputs")
    end = chain.tip()
    found, scanned = spends_after(chain, end, spent, rpc, progress)
    for outpoint, block in found.items():
        spent[outpoint]["spent_block"] = block
    if rpc("getblockhash", reference["height"]) != reference["hash"]:
        raise ValueError("capture reference changed during the Core audit")
    for row in evidence:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
    return {
        **audit,
        "finished_at": datetime.now(UTC).isoformat(),
        "tip_end": end,
        "outputs_checked": len(rows),
        "sources": dict(sorted(sources.items())),
        "blocks_scanned_after_reference": scanned,
        **unspentness(evidence, rows, reference),
        "scope": SCOPE,
    }


def measure_value(capture, sample, output, *, rpc=None, workers=0, progress=lambda *_: None):
    """Write the funding value of a capture to a new private folder; returns the summary.

    With `rpc`, every output is also audited against Bitcoin Core and the evidence is kept.
    """
    if output.exists() or output.is_symlink():
        raise ValueError("value output directory exists; choose a new destination")
    summary, rows = joined_snapshot(capture, sample, workers)
    if rpc is not None and summary["dataset_kind"] != "mainnet":
        raise ValueError("synthetic captures cannot be audited against mainnet Core")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".gossip-value-", dir=output.parent))
    try:
        if rpc is not None:
            with (staging / "core-evidence.jsonl").open("x") as stream:
                summary["core_audit"] = audit_core(
                    rows, summary["reference"], rpc, stream, progress
                )
        write_json(staging / "summary.json", summary)
        write_json(
            staging / "checksums.json",
            {p.name: sha256_file(p) for p in sorted(staging.iterdir())},
        )
        for path in staging.iterdir():
            path.chmod(0o600)
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return summary


def evidence_row(line):
    """One line of the Core evidence. A field that appears twice is an error."""

    def unique(pairs):
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicate field in the Core evidence")
        return fields

    return json.loads(line, object_pairs_hook=unique)


def verify_value(capture, sample, output, *, workers=0):
    """Recompute the value of a capture and replay the saved Core evidence, without a node.

    The saved summary must equal the recomputed one, and the counts of a Core audit must
    follow from its evidence. Returns the summary.
    """
    checksums = load_json(output / "checksums.json")
    stored = load_json(output / "summary.json")
    if stored.get("schema_version") != VALUE_VERSION:
        raise ValueError("value artifact is in another format; create it again")
    expected_files = {"summary.json"}
    if stored["core_audit"] is not None:
        expected_files.add("core-evidence.jsonl")
    if set(checksums) != expected_files or {p.name for p in output.iterdir()} != (
        expected_files | {"checksums.json"}
    ):
        raise ValueError("unexpected value artifact files")
    for name, digest in checksums.items():
        if (output / name).is_symlink() or sha256_file(output / name) != digest:
            raise ValueError("value artifact checksum mismatch")
    calculated, rows = joined_snapshot(capture, sample, workers)
    if stored["core_audit"] is not None:
        if calculated["dataset_kind"] != "mainnet":
            raise ValueError("synthetic captures cannot carry a mainnet Core audit")
        audit, reference = stored["core_audit"], calculated["reference"]
        if set(audit) != AUDIT_KEYS or audit["scope"] != SCOPE:
            raise ValueError("unexpected Core audit summary")
        lines = (output / "core-evidence.jsonl").read_text().splitlines()
        if len(lines) != len(rows):
            raise ValueError("Core evidence does not cover every included output")
        evidence, sources = [], Counter()
        for line, (record, value) in zip(lines, rows, strict=True):
            row = evidence_row(line)
            check_evidence(row, record, value)
            evidence.append(row)
            sources[row["source"]] += 1
        shown = unspentness(evidence, rows, reference)
        end = audit["tip_end"]["height"]
        spends = [row["spent_block"]["height"] for row in evidence if row["spent_block"]]
        if type(end) is not int or end < reference["height"] or any(h > end for h in spends):
            raise ValueError("spending block lies beyond the audited chain tip")
        # The search reads every block up to the tip unless all spends turn up earlier.
        last = (
            end
            if shown["snapshot_unspentness"]["not_shown"]["outputs"]
            else max(spends, default=reference["height"])
        )
        if (
            audit["outputs_checked"] != len(rows)
            or audit["sources"] != dict(sources)
            or any(audit[name] != part for name, part in shown.items())
            or audit["blocks_scanned_after_reference"] != last - reference["height"]
        ):
            raise ValueError("Core audit counts do not reconcile")
        calculated["core_audit"] = audit
    if calculated != stored:
        raise ValueError("recomputed funding value differs from the saved aggregate")
    return calculated
