"""Fail-closed ingestion and deterministic aggregate outputs."""

import csv
import io
import platform
import shutil
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from .common import PROJECT, sha256_file, write_json
from .performance import worker_count
from .schema import (
    RECENT_UPDATE_SECONDS,
    YEAR_BLOCKS,
    BlockRef,
    ClosedAudit,
    ExportReport,
    Manifest,
    load_json,
)
from .verification import verified_records

UPDATE_NOTE = (
    "A channel has a recent update when either node's stored channel_update is dated "
    "within the window before the reference time. No recent update observed means this "
    "observer's LND held no such update. It does not establish that a channel is dormant "
    "or abandoned. LND removes channels whose updates are all older than the window, so "
    "this group mainly holds announcements without any stored update. Timestamps are as "
    "recorded by LND and are not re-verified offline. A reused funding key can count in "
    "both groups."
)
# Format of summary.json. A results folder written in another format is analyzed again.
SUMMARY_VERSION = 3
BIN_BLOCKS = 1000  # Width of an age bin in the exact analysis, unless another is asked for.
CLOSED_NOTE = (
    "Channels the observer marked closed are not exported. An entry whose funding output "
    "is still unspent at the audit is a live channel that the observer excluded."
)


def nearest_rank(ordered: list[int], numerator: int, denominator: int = 100) -> int:
    """The quantile of sorted values by the nearest-rank method, which returns an observed value."""
    return ordered[(len(ordered) * numerator + denominator - 1) // denominator - 1]


def quantiles(ordered: list[int]) -> dict:
    """Median, 90th percentile and maximum of sorted ages."""
    return {
        "median": nearest_rank(ordered, 50),
        "p90": nearest_rank(ordered, 90),
        "maximum": ordered[-1],
    }


def current(summary: dict) -> dict:
    """Refuse a summary in another format than this analysis writes."""
    if summary.get("schema_version") != SUMMARY_VERSION:
        raise ValueError("results are in another format; analyze the capture again")
    return summary


def closed_index(capture: Path, manifest: Manifest, report: ExportReport) -> dict:
    """Reconcile the exported closed-channel index and its Bitcoin Core audit."""
    closed_path = capture / "closed-scids.txt"
    if sha256_file(closed_path) != report.closed_scids_sha256:
        raise ValueError("closed-channel index hash mismatch")
    if len(closed_path.read_text().splitlines()) != report.closed_scids:
        raise ValueError("closed-channel index count does not reconcile")
    audit = None
    if manifest.closed_audit_sha256:
        audit_path = capture / "closed-audit.json"
        if sha256_file(audit_path) != manifest.closed_audit_sha256:
            raise ValueError("closed-index audit hash mismatch")
        checked = ClosedAudit.model_validate(load_json(audit_path))
        if (checked.closed_scids_sha256, checked.entries) != (
            report.closed_scids_sha256,
            report.closed_scids,
        ):
            raise ValueError("closed-index audit covers a different index")
        audit = checked.model_dump(
            include={"audited_at", "tip", "not_in_utxo_set", "unspent_at_audit", "unresolved"}
        )
    return {"entries": report.closed_scids, "audit": audit, "note": CLOSED_NOTE}


def calculate(capture: Path, bin_width: int = BIN_BLOCKS, *, workers=0) -> tuple[dict, list[dict]]:
    """Verify a capture and compute its summary and age distribution. Nothing is written.

    Hashes, signatures and counts are reconciled first, and a mainnet capture must also
    satisfy the readiness rule. A row of the distribution counts the funding outputs whose
    age falls into one bin of `bin_width` blocks, named by the bin's upper bound.
    """
    if bin_width <= 0:
        raise ValueError("bin width must be positive")
    manifest = Manifest.model_validate(load_json(capture / "manifest.json"))
    records_path = capture / "records.jsonl"
    report_path = capture / "export-report.json"
    if sha256_file(records_path) != manifest.records_sha256:
        raise ValueError("records hash mismatch")
    if sha256_file(report_path) != manifest.export_report_sha256:
        raise ValueError("export report hash mismatch")
    report = ExportReport.model_validate(load_json(report_path))
    if report.records_sha256 != manifest.records_sha256:
        raise ValueError("export report/manifest record hashes differ")
    if report.graph_prune_tip != manifest.reference:
        raise ValueError("graph prune tip differs from reference block")
    if manifest.dataset_kind == "mainnet":
        from .readiness import check_readiness

        readiness_path = capture / "readiness.json"
        if sha256_file(readiness_path) != manifest.readiness_sha256:
            raise ValueError("readiness hash mismatch")
        samples = load_json(readiness_path)
        readiness = check_readiness(samples)
        if not readiness["ready"]:
            raise ValueError("capture did not satisfy readiness criteria")
        final = samples[-1]["getinfo"]
        if BlockRef(height=final["block_height"], hash=final["block_hash"]) != manifest.reference:
            raise ValueError("final readiness reference differs from snapshot reference")

    closed = closed_index(capture, manifest, report)
    reference_time = datetime.fromisoformat(manifest.captured_at.replace("Z", "+00:00"))
    recent_after = int(reference_time.timestamp()) - RECENT_UPDATE_SECONDS
    ages, recent_ages, funding_keys, scids, outpoints = [], [], set(), set(), set()
    # Channels and funding keys with, then without, a recent update.
    groups = [[0, set()], [0, set()]]
    bands = Counter()
    without_update = 0
    exclusions = Counter()
    total = 0
    with closing(verified_records(records_path, workers)) as records:
        for record in records:
            if record.height > manifest.reference.height:
                raise ValueError(
                    f"funding height exceeds snapshot reference at line {record.line_number}"
                )
            if record.scid in scids or record.outpoint in outpoints:
                raise ValueError(
                    f"duplicate/conflicting SCID or funding outpoint at line {record.line_number}"
                )
            scids.add(record.scid)
            outpoints.add(record.outpoint)
            total += 1
            if record.exclusion:
                exclusions[record.exclusion] += 1
            else:
                ages.append(manifest.reference.height - record.height)
                funding_keys.update(record.funding_keys)
                recent = record.last_update is not None and record.last_update >= recent_after
                group = groups[not recent]
                if recent:
                    recent_ages.append(ages[-1])
                bands[ages[-1] // YEAR_BLOCKS, recent] += 1
                group[0] += 1
                group[1].update(record.funding_keys)
                without_update += record.last_update is None

    if total != report.exported_records or len(ages) != report.included_records:
        raise ValueError("exported/included counts do not reconcile")
    if dict(exclusions) != report.exclusions:
        raise ValueError("exclusion counts do not reconcile")
    if not ages:
        raise ValueError("empty eligible cohort; no empirical result to plot")
    ages.sort()
    recent_ages.sort()

    def binned(values):
        return Counter((age + bin_width - 1) // bin_width * bin_width for age in values)

    bins, recent_bins = binned(ages), binned(recent_ages)
    cumulative, recent_cumulative, cdf = 0, 0, []
    for upper_bound, count in sorted(bins.items()):
        cumulative += count
        recent_cumulative += recent_bins[upper_bound]
        cdf.append(
            {
                "age_upper_blocks": upper_bound,
                "count": count,
                "cumulative_count": cumulative,
                "cumulative_fraction": cumulative / len(ages),
                # The same bins for the announcements with a recent update observed.
                "recent_count": recent_bins[upper_bound],
                "recent_cumulative_count": recent_cumulative,
                "recent_cumulative_fraction": recent_cumulative / max(len(recent_ages), 1),
            }
        )
    summary = {
        "schema_version": SUMMARY_VERSION,
        "dataset_kind": manifest.dataset_kind,
        "captured_at": manifest.captured_at,
        "reference": manifest.reference.model_dump(),
        "backend": manifest.backend,
        "lnd_commit": report.lnd_commit,
        "cohort": "local graph: authenticated v1 P2WSH, no nonzero channel feature bits",
        "exported_records": total,
        "included_records": len(ages),
        "excluded_records": sum(exclusions.values()),
        "exclusions": dict(sorted(exclusions.items())),
        "distinct_funding_keys": len(funding_keys),
        "counts": {
            "authenticated_announcements": len(ages),
            "distinct_funding_keys": len(funding_keys),
            "update_recency": {
                "window_seconds": RECENT_UPDATE_SECONDS,
                "reference_time": manifest.captured_at,
                "recent_update_observed": {
                    "channels": groups[0][0],
                    "distinct_funding_keys": len(groups[0][1]),
                },
                "no_recent_update_observed": {
                    "channels": groups[1][0],
                    "distinct_funding_keys": len(groups[1][1]),
                    "channels_without_any_update": without_update,
                },
                "note": UPDATE_NOTE,
            },
        },
        "closed_index": closed,
        "funding_output_age_blocks": quantiles(ages),
        # The published age figure and statistics: channels that their nodes still advertise.
        "funding_output_age_blocks_recent_update_observed": (
            quantiles(recent_ages) if recent_ages else None
        ),
        "funding_output_age_bands": {
            "band_blocks": YEAR_BLOCKS,
            "note": "Band k holds ages from k to below k+1 nominal years of 144 blocks a day.",
            "bands": [
                {
                    "band": band,
                    "recent_update_observed": bands[band, True],
                    "no_recent_update_observed": bands[band, False],
                }
                for band in range(ages[-1] // YEAR_BLOCKS + 1)
            ],
        },
        "quantile_method": "nearest rank: sorted_values[ceil(n*p)-1]",
        "cdf_bin_width_blocks": bin_width,
        "interpretation": (
            "Funding-output age at observation, not exposure duration or channel age."
        ),
        "inputs": {
            "manifest_sha256": sha256_file(capture / "manifest.json"),
            "export_report_sha256": manifest.export_report_sha256,
            "records_sha256": manifest.records_sha256,
        },
    }
    return summary, cdf


def cdf_csv(rows: list[dict]) -> str:
    """The exact age distribution as CSV text, one row per bin."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=[
            "age_upper_blocks",
            "count",
            "cumulative_count",
            "cumulative_fraction",
            "recent_count",
            "recent_cumulative_count",
            "recent_cumulative_fraction",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def analyze(capture: Path, output: Path, bin_width: int = BIN_BLOCKS, *, workers=0) -> dict:
    """Write the exact aggregates of a capture to a new folder; returns the summary.

    The folder holds the summary, the age distribution, a record of the software that
    computed them and the checksums of the three. It stays private: see `publish`.
    """
    workers = worker_count(workers)
    summary, rows = calculate(capture, bin_width, workers=workers)
    if output.exists():
        raise ValueError("output directory exists; choose a new destination")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".gossip-analysis-", dir=output.parent))
    try:
        write_json(staging / "summary.json", summary)
        (staging / "age-cdf.csv").write_text(cdf_csv(rows))
        write_json(
            staging / "analysis-environment.json",
            {
                "package_version": version("gossip-snapshot"),
                "python_version": platform.python_version(),
                "verification_worker_limit": workers,
                "platform": {"system": platform.system(), "machine": platform.machine()},
                "uv_lock_sha256": (
                    sha256_file(PROJECT / "uv.lock") if (PROJECT / "uv.lock").is_file() else None
                ),
                "dependencies": {
                    name: version(name) for name in ["coincurve", "pydantic", "matplotlib"]
                },
                "source_hashes": {
                    path.name: sha256_file(path)
                    for path in sorted(Path(__file__).parent.glob("*.py"))
                },
            },
        )
        write_json(
            staging / "checksums.json",
            {path.name: sha256_file(path) for path in sorted(staging.iterdir()) if path.is_file()},
        )
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return summary


def verify_artifact(output: Path, capture: Path, *, workers=0) -> None:
    """Check what `analyze` wrote against its checksums and a new analysis of the capture."""
    checksums = load_json(output / "checksums.json")
    expected = {"summary.json", "age-cdf.csv", "analysis-environment.json"}
    if not isinstance(checksums, dict) or set(checksums) != expected:
        raise ValueError("artifact checksum file has an unexpected file set")
    if {p.name for p in output.iterdir()} != expected | {"checksums.json"}:
        raise ValueError("unexpected files in the results folder")
    for name, digest in checksums.items():
        if (output / name).is_symlink() or sha256_file(output / name) != digest:
            raise ValueError(f"artifact hash mismatch: {name}")
    stored = current(load_json(output / "summary.json"))
    summary, rows = calculate(capture, stored["cdf_bin_width_blocks"], workers=workers)
    if summary != stored or cdf_csv(rows) != (output / "age-cdf.csv").read_text():
        raise ValueError("artifact statistics differ from reanalysis")
