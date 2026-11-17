"""Agreement between two observers of the same gossip network, as counts only.

Both captures are verified as in the main analysis and compared by channel identifier,
group by group. A channel that one observer lacks may have been funded after that
observer's snapshot, closed in between, or never have reached it. Only the first case
is told apart, by funding height. Only counts are written, and `publish` reduces them
to what each observer held and did not hold of the other's groups.
"""

import shutil
import tempfile
from pathlib import Path

from .analysis import calculate
from .common import sha256_file, write_json
from .funding_value import RECENCY, recency_group, recency_threshold
from .schema import Record, decode_scid, load_json

FILES = ("summary.json", "checksums.json")
NOTE = (
    "Each group of one observer is located in the other observer's capture: held with a "
    "recent update observed, held without one, absent but funded after that capture's "
    "reference block, or absent. Absent channels may have closed between the two captures "
    "or never reached that observer. Agreement shows that a group does not depend on one "
    "collection. It is not a network census, because both observers run the same LND "
    "selection rules on the same gossip network."
)


def groups(capture, workers):
    """Verified summary and the channel identifiers of each update-recency group."""
    checked, _ = calculate(capture, workers=workers)
    recent_after = recency_threshold(checked)
    ids = {name: set() for name in RECENCY}
    with (capture / "records.jsonl").open() as stream:
        for line in stream:
            record = Record.model_validate_json(line)
            if record.exclusion_reason is None:
                ids[recency_group(record, recent_after)].add(record.scid)
    if sha256_file(capture / "records.jsonl") != checked["inputs"]["records_sha256"]:
        raise ValueError("inputs changed during the comparison")
    counts = checked["counts"]["update_recency"]
    if any(len(ids[name]) != counts[name]["channels"] for name in RECENCY):
        raise ValueError("update-recency groups differ from the main analysis")
    return checked, ids


def located(ids, held, reference_height):
    """Where the other observer has each channel of one group."""
    absent = ids - held[RECENCY[0]] - held[RECENCY[1]]
    later = {scid for scid in absent if decode_scid(scid)[0] > reference_height}
    return {
        "channels": len(ids),
        "held_with_recent_update": len(ids & held[RECENCY[0]]),
        "held_without_recent_update": len(ids & held[RECENCY[1]]),
        "funded_after_its_reference": len(later),
        "absent": len(absent - later),
    }


def describe(checked):
    """What identifies a capture in a comparison: its time, reference block and input hashes."""
    return {key: checked[key] for key in ("captured_at", "reference", "inputs")}


def compare(capture, other, *, workers=0):
    """Locate each update-recency group of either capture in the other one, as counts."""
    here, mine = groups(capture, workers)
    there, theirs = groups(other, workers)
    if here["dataset_kind"] != there["dataset_kind"]:
        raise ValueError("both captures must be of the same data set kind")
    if here["inputs"]["manifest_sha256"] == there["inputs"]["manifest_sha256"]:
        raise ValueError("the second observer must be a different capture")
    return {
        "schema_version": 1,
        "dataset_kind": here["dataset_kind"],
        "observer": describe(here),
        "second_observer": describe(there),
        "observer_in_second": {
            name: located(mine[name], theirs, there["reference"]["height"]) for name in RECENCY
        },
        "second_in_observer": {
            name: located(theirs[name], mine, here["reference"]["height"]) for name in RECENCY
        },
        "note": NOTE,
    }


def compare_observers(capture, other, output, *, workers=0):
    """Write the comparison of two captures to a new folder; returns the summary."""
    if output.exists() or output.is_symlink():
        raise ValueError("comparison output directory exists; choose a new destination")
    summary = compare(capture, other, workers=workers)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".gossip-observers-", dir=output.parent))
    try:
        write_json(staging / "summary.json", summary)
        write_json(
            staging / "checksums.json", {"summary.json": sha256_file(staging / "summary.json")}
        )
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return summary


def verify_observers(capture, other, output, *, workers=0):
    """Recompute the comparison from both captures and require the saved summary."""
    if {p.name for p in output.iterdir()} != set(FILES):
        raise ValueError("unexpected comparison artifact files")
    path = output / "summary.json"
    if path.is_symlink() or sha256_file(path) != load_json(output / "checksums.json").get(
        "summary.json"
    ):
        raise ValueError("comparison summary does not match its recorded checksum")
    calculated = compare(capture, other, workers=workers)
    if calculated != load_json(path):
        raise ValueError("recomputed comparison differs from the saved summary")
    return calculated
