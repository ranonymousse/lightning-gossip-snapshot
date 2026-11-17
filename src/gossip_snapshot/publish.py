"""Public results: the aggregates of a verified capture that may be published.

A capture holds funding keys, channel identifiers and outpoints, and a session holds peer
addresses and logs. None of that leaves the machine that collected it. `publish` analyzes
the capture again, checks the value and observer artifacts against it and writes a folder
that holds aggregates only. Four rules keep single channels out of that folder:

- The age distribution covers the announcements with a recent update observed, in
  twelfths of a nominal year. Rows are merged towards older ages until each describes at
  least `MINIMUM_ROW` announcements, and the oldest row ends at a whole nominal year.
- Ages are published for a group of at least `MINIMUM_AGES` announcements. Apart from
  the distribution they appear as two quantiles in tenths of a nominal year, each at
  least `MINIMUM_ROW` announcements away from either end, and as the number of old
  outputs where that number follows from the rows. No extreme value is published.
- A funding value is published for a set of at least `MINIMUM_ROW` outputs, and no two
  published values differ by the value of fewer.
- Every file is built field by field from the lists in this module. As a second line of
  defence, the folder is then scanned for anything shaped like a key, a script, a channel
  identifier, an outpoint, an address or a path. A hit stops the publication.

Plain counts, such as the entries of the closed-channel index or the channels that one
synchronization added, carry no age or value of a channel and are published as they are.
`figures` and `paper-numbers` read such a folder and nothing else.
"""

import csv
import io
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

from .analysis import calculate
from .common import sha256_file, write_json
from .funding_value import RECENCY, STATES, btc, verify_value
from .observers import verify_observers
from .readiness import (
    MAXIMUM_CHURN,
    MINIMUM_INTERVAL_SECONDS,
    MINIMUM_PEERS,
    OBSERVATIONS,
    check_readiness,
)
from .schema import YEAR_BLOCKS, BlockRef, load_json

RESULTS_VERSION = 1
# A twelfth of a nominal year, so that whole years fall on row boundaries.
CDF_STEP_BLOCKS = YEAR_BLOCKS // 12
MINIMUM_ROW = 10  # No published row or value describes fewer announcements than this.
# Ages are published for a group of at least this size. Its 90th percentile then lies at
# least MINIMUM_ROW announcements below the oldest output, and its median further inside.
MINIMUM_AGES = 100
READY = "readiness reached"  # The collector's stop reason for a session that ended ready.
OLD_YEARS = 5  # Outputs of at least this many nominal years are counted on their own.
YOUNG_YEARS = 1  # The figure and the reported numbers also give the share younger than this.
DATA_FILES = (
    "snapshot.json",
    "age-cdf.csv",
    "funding-value.json",
    "observers.json",
    "collection.json",
)
# Everything a results folder may hold.
FOLDER_FILES = {
    *DATA_FILES,
    "checksums.json",
    "README.md",
    "age-cdf.pdf",
    "age-cdf.png",
    "gossip_numbers.tex",
}
CDF_COLUMNS = (
    "age_from_blocks",
    "age_below_blocks",
    "count",
    "cumulative_count",
    "cumulative_fraction",
)
EXCLUSIONS = ("missing_proof", "unsupported_features")
LIMITS = ("hours", "max_download_gb", "max_data_gb", "min_free_gb")
AGE_UNIT = "tenths of a nominal year of 52,560 blocks, rounded to nearest, ties to even"
VALUE_NOTE = (
    "Funding value of the snapshot's announcements, each funding output counted once. It "
    "is what the announced keys guard if both private keys of an output are recovered "
    "while the output is still spendable under them. It is not a network census, a "
    "demonstrated attack or a forecast of losses. The audit shows output by output that "
    "the output was unspent at the reference block: it was either still unspent when "
    "checked, or the input that spent it lies in a later block."
)
OBSERVERS_NOTE = (
    "For each group of one observer, how many of its announcements the other observer "
    "also held, with or without a recent update of its own. An announcement that the "
    "other did not hold may have been funded after its reference block, closed in "
    "between, or never have reached it. Agreement shows that a group does not depend on "
    "one collection. It is not a network census, because both observers run the same LND "
    "selection rules on the same gossip network."
)
SCANNED = {".json", ".csv", ".md", ".tex"}
HASH = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
# Keys, scripts, signatures and signed messages are long runs of hexadecimal digits.
HEX_RUN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40,}(?![0-9A-Fa-f])")
FORBIDDEN = (
    ("a public key", re.compile(r"(?<![0-9A-Fa-f])0[23][0-9A-Fa-f]{64}(?![0-9A-Fa-f])")),
    ("an outpoint", re.compile(r"[0-9A-Fa-f]{64}:[0-9]+")),
    (
        "a channel identifier",
        re.compile(
            # In decimal, unless the digits are the fraction of a number.
            r"(?<![0-9A-Fa-f])(?<![0-9]\.)[0-9]{15,20}(?![0-9A-Fa-f])(?!\.[0-9])"
            r"|(?<![0-9,])[0-9]{1,3}(?:,[0-9]{3}){4,6}(?![0-9,])"
            r"|\b0x[0-9A-Fa-f]{12,16}\b"
            r"|\b[0-9]+x[0-9]+x[0-9]+\b"
            r"|(?<![0-9:.])[0-9]{6,7}:[0-9]{1,5}:[0-9]{1,5}(?![0-9:])"
        ),
    ),
    ("a session identifier", re.compile(r"\b[0-9]{8}T[0-9]{6}(?:\.[0-9]+)?Z\b")),
    (
        "a Bitcoin address",
        re.compile(
            r"\b(?i:bc|tb|bcrt)1(?i:[02-9ac-hj-np-z]){20,87}\b"
            r"|\b[13][1-9A-HJ-NP-Za-km-z]{25,34}\b"
        ),
    ),
    (
        "a network address",
        re.compile(
            r"(?<![0-9])(?<![0-9]\.)[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?![0-9])(?!\.[0-9])"
            r"|\[[0-9A-Fa-f:]{3,}\]"
            r"|(?<![0-9A-Fa-f:.])(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}(?![0-9A-Fa-f:])"
            r"|(?<![0-9A-Fa-f:.])(?:[0-9A-Fa-f]{1,4}:){2,7}:"
            r"|::[0-9A-Fa-f]{1,4}"
            r"|\.onion\b"
        ),
    ),
    ("an e-mail address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    (
        "a local path",
        re.compile(
            r"(?<![\w.:/~])(?:~|/(?:home|Users|root|var|tmp|private|opt|mnt|srv|etc|data"
            r"|Volumes|media|run|usr|workspace))/"
            r"|\b[A-Za-z]:\\"
        ),
    ),
)


def tenths(blocks: int) -> int:
    """An age in tenths of a nominal year: nearest, ties to even, in whole numbers only."""
    whole, rest = divmod(10 * blocks, YEAR_BLOCKS)
    if 2 * rest > YEAR_BLOCKS or (2 * rest == YEAR_BLOCKS and whole % 2):
        whole += 1
    return whole


def count(value) -> int:
    """A published count: a whole number that is not negative."""
    if type(value) is not int or value < 0:
        raise ValueError("a published count must be a whole number")
    return value


def instant(value: str) -> str:
    """A published time: UTC, to the second."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.utcoffset() != timedelta(0):
        raise ValueError("a published time must be in UTC")
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def fraction(value) -> float:
    """A published share: a number from 0 to 1."""
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError("a published share must lie between 0 and 1")
    return float(value)


def block(value: dict) -> dict:
    """A published block: its height and hash, and nothing else."""
    return BlockRef.model_validate(value).model_dump()


def digests(value: dict, names) -> dict:
    """The named SHA-256 values of a record, each checked for its form."""
    for name in names:
        if type(value.get(name)) is not str or not HASH.fullmatch(value[name]):
            raise ValueError(f"{name} is not a SHA-256 value")
    return {name: value[name] for name in names}


def recent_ages(exact_rows: list[dict]) -> Counter:
    """Announcements with a recent update observed per exact age, from one-block bins."""
    return Counter(
        {row["age_upper_blocks"]: row["recent_count"] for row in exact_rows if row["recent_count"]}
    )


def coarse_cdf(ages: Counter) -> list[dict]:
    """The publishable age distribution, or no rows when the group is too small for ages.

    A row counts the announcements whose funding output is at least `age_from_blocks` and
    less than `age_below_blocks` old.
    """
    total = sum(ages.values())
    if total < MINIMUM_AGES:
        return []
    steps = Counter()
    for age, announcements in ages.items():
        steps[age // CDF_STEP_BLOCKS] += announcements
    merged, start, held = [], 0, 0
    for step in sorted(steps):
        held += steps[step]
        if held >= MINIMUM_ROW:
            merged.append([start, (step + 1) * CDF_STEP_BLOCKS, held])
            start, held = (step + 1) * CDF_STEP_BLOCKS, 0
    # What is left above the last full row joins it, and that row ends at a whole year.
    merged[-1][2] += held
    oldest = (max(steps) + 1) * CDF_STEP_BLOCKS
    merged[-1][1] = -(-oldest // YEAR_BLOCKS) * YEAR_BLOCKS
    rows, cumulative = [], 0
    for start, below, announcements in merged:
        cumulative += announcements
        rows.append(
            {
                "age_from_blocks": start,
                "age_below_blocks": below,
                "count": announcements,
                "cumulative_count": cumulative,
                "cumulative_fraction": cumulative / total,
            }
        )
    return rows


def share_younger(rows: list[dict], years: int) -> Fraction | None:
    """The share of the announcements whose output is younger than `years` nominal years.

    The share is exact only where a published row ends at that age. Rows are merged until
    each holds enough announcements, so no row may end there, and the share is then
    unknown: nothing is read off the line between two rows.
    """
    for row in rows:
        if row["age_below_blocks"] == years * YEAR_BLOCKS:
            return Fraction(row["cumulative_count"], rows[-1]["cumulative_count"])
    return None


def percent(share: Fraction) -> int:
    """A share in whole percent, rounded to nearest, ties to even."""
    return round(100 * share)


def cdf_csv(rows: list[dict]) -> str:
    """The published age distribution as CSV text."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CDF_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def snapshot_document(summary: dict, ages: Counter, rows: list[dict]) -> dict:
    """snapshot.json: the counts of the analysis, with ages in publishable form."""
    exact = summary["funding_output_age_blocks_recent_update_observed"]
    age = None
    if rows:
        boundary = OLD_YEARS * YEAR_BLOCKS
        old = sum(n for blocks, n in ages.items() if blocks >= boundary)
        bands = summary["funding_output_age_bands"]["bands"]
        if old != sum(band[RECENCY[0]] for band in bands if band["band"] >= OLD_YEARS):
            raise ValueError("age distribution differs from the analysis summary")
        # The count is published where it follows from the rows: a row starts at the
        # boundary, or no row reaches it. Otherwise it would split a row.
        if not (
            rows[-1]["age_below_blocks"] <= boundary
            or any(row["age_from_blocks"] == boundary for row in rows)
        ):
            old = None
        age = {
            "group": RECENCY[0],
            "unit": AGE_UNIT,
            "median_tenths": tenths(exact["median"]),
            "p90_tenths": tenths(exact["p90"]),
            "quantile_method": summary["quantile_method"],
            f"outputs_at_least_{OLD_YEARS}_years": old,
            "interpretation": summary["interpretation"],
        }
    counts, recency = summary["counts"], summary["counts"]["update_recency"]
    recent, silent = recency[RECENCY[0]], recency[RECENCY[1]]
    closed, audit = summary["closed_index"], summary["closed_index"]["audit"]
    return {
        "schema_version": RESULTS_VERSION,
        "dataset_kind": summary["dataset_kind"],
        "captured_at": instant(summary["captured_at"]),
        "reference": block(summary["reference"]),
        "backend": summary["backend"],
        "lnd_commit": summary["lnd_commit"],
        "cohort": summary["cohort"],
        "exported_records": count(summary["exported_records"]),
        "included_records": count(summary["included_records"]),
        "excluded_records": count(summary["excluded_records"]),
        "exclusions": {
            name: count(summary["exclusions"][name])
            for name in EXCLUSIONS
            if name in summary["exclusions"]
        },
        "counts": {
            "authenticated_announcements": count(counts["authenticated_announcements"]),
            "distinct_funding_keys": count(counts["distinct_funding_keys"]),
            "update_recency": {
                "window_seconds": count(recency["window_seconds"]),
                "reference_time": instant(recency["reference_time"]),
                RECENCY[0]: {
                    "channels": count(recent["channels"]),
                    "distinct_funding_keys": count(recent["distinct_funding_keys"]),
                },
                RECENCY[1]: {
                    "channels": count(silent["channels"]),
                    "distinct_funding_keys": count(silent["distinct_funding_keys"]),
                    "channels_without_any_update": count(silent["channels_without_any_update"]),
                },
                "note": recency["note"],
            },
        },
        "closed_index": {
            "entries": count(closed["entries"]),
            "audit": audit
            and {
                "audited_at": instant(audit["audited_at"]),
                "tip": block(audit["tip"]),
                "not_in_utxo_set": count(audit["not_in_utxo_set"]),
                "unspent_at_audit": count(audit["unspent_at_audit"]),
                "unresolved": count(audit["unresolved"]),
            },
            "note": closed["note"],
        },
        "funding_output_age": age,
        "age_cdf": {
            "group": RECENCY[0],
            "step_blocks": CDF_STEP_BLOCKS,
            "minimum_row_count": MINIMUM_ROW,
            "rows": len(rows),
        },
        "inputs": digests(
            summary["inputs"], ("manifest_sha256", "export_report_sha256", "records_sha256")
        ),
    }


def value_document(stored: dict) -> dict:
    """funding-value.json: the funding value and its audit, field by field.

    A value is published for a set of at least `MINIMUM_ROW` outputs, and no two published
    values may differ by the value of fewer. The audited total is therefore published
    without the unaudited one when a few outputs were not shown unspent, and the split by
    update recency only when every output was shown or none was audited. A withheld value
    is null. The audit's states are published as counts.

    No hash of the private value summary is published: without an audit, that summary
    holds nothing that could not be guessed, so its hash would give a withheld value away.
    """

    def few(outputs: int) -> bool:
        return 0 < outputs < MINIMUM_ROW

    def amount(cell: dict, shown: bool) -> dict:
        value = count(cell["funding_value_satoshis"]) if shown else None
        return {
            "outputs": count(cell["outputs"]),
            "funding_value_satoshis": value,
            "funding_value_btc": None if value is None else btc(value),
        }

    split, audit = stored["by_update_recency"], stored["core_audit"]
    whole = {
        "outputs": stored["included_outputs"],
        "funding_value_satoshis": stored["funding_value_satoshis"],
    }
    states = audit and {
        name: count(audit["snapshot_unspentness"][name]["outputs"]) for name in STATES
    }
    # The unaudited total exceeds the audited one by the value of the outputs not shown,
    # and a group's value could differ from the audited total by the value of a few.
    hidden = states["not_shown"] if states else 0
    total = not few(whole["outputs"]) and not few(hidden)
    parts = total and not hidden and not any(few(split[name]["outputs"]) for name in RECENCY)
    checked = None
    if audit is not None:
        confirmed = audit["confirmed_unspent_at_reference"]
        checked = {
            "scope": audit["scope"],
            "started_at": instant(audit["started_at"]),
            "finished_at": instant(audit["finished_at"]),
            "tip_start": block(audit["tip_start"]),
            "tip_end": block(audit["tip_end"]),
            "outputs_checked": count(audit["outputs_checked"]),
            "blocks_scanned_after_reference": count(audit["blocks_scanned_after_reference"]),
            "outputs_by_state": states,
            "confirmed_unspent_at_reference": amount(confirmed, not few(confirmed["outputs"])),
        }
    summed = amount(whole, total)
    return {
        "schema_version": RESULTS_VERSION,
        "dataset_kind": stored["dataset_kind"],
        "captured_at": instant(stored["captured_at"]),
        "reference": block(stored["reference"]),
        "included_outputs": summed["outputs"],
        "funding_value_satoshis": summed["funding_value_satoshis"],
        "funding_value_btc": summed["funding_value_btc"],
        "by_update_recency": {
            "window_seconds": count(split["window_seconds"]),
            **{name: amount(split[name], parts) for name in RECENCY},
        },
        "value_source": stored["value_source"],
        "core_audit": checked,
        "inputs": digests(
            stored["inputs"],
            ("manifest_sha256", "export_report_sha256", "records_sha256", "final_sample_sha256"),
        ),
        "interpretation": VALUE_NOTE,
    }


def observers_document(stored: dict) -> dict:
    """observers.json: per group and direction, what the other observer held."""

    def held(group: dict) -> dict:
        kept = group["held_with_recent_update"] + group["held_without_recent_update"]
        return {
            "channels": count(group["channels"]),
            "held": count(kept),
            "not_held": count(group["funded_after_its_reference"] + group["absent"]),
        }

    def described(observer: dict) -> dict:
        return {
            "captured_at": instant(observer["captured_at"]),
            "reference": block(observer["reference"]),
            "inputs": digests(
                observer["inputs"], ("manifest_sha256", "export_report_sha256", "records_sha256")
            ),
        }

    return {
        "schema_version": RESULTS_VERSION,
        "dataset_kind": stored["dataset_kind"],
        "observer": described(stored["observer"]),
        "second_observer": described(stored["second_observer"]),
        "observer_in_second": {name: held(stored["observer_in_second"][name]) for name in RECENCY},
        "second_in_observer": {name: held(stored["second_in_observer"][name]) for name in RECENCY},
        "note": OBSERVERS_NOTE,
    }


def collection_document(summary: dict, capture: Path, session_result: Path) -> dict:
    """collection.json: how the session behind a capture reached readiness.

    Only counts and the session's own settings are taken from its result. Paths, the
    collector revision, the host's resources, peers and the times of network events stay
    private.
    """
    session = load_json(session_result)
    closed = session.get("closed_audit") or {}
    if (
        session.get("included_records"),
        session.get("final_sampled_at"),
        closed.get("entries"),
    ) != (
        summary["included_records"],
        summary["captured_at"],
        summary["closed_index"]["entries"],
    ):
        raise ValueError("session result does not belong to this capture")
    churn = session["readiness"]["churn"]
    if summary["dataset_kind"] == "mainnet":
        decision = check_readiness(load_json(capture / "readiness.json"))
        if not decision["ready"] or decision["churn"] != churn:
            raise ValueError("session readiness differs from the capture's saved observations")
    limits = session.get("limits") or {}
    return {
        "schema_version": RESULTS_VERSION,
        "dataset_kind": summary["dataset_kind"],
        "captured_at": instant(summary["captured_at"]),
        "reference": block(summary["reference"]),
        "included_records": count(summary["included_records"]),
        "session": {
            # Whether the session ended because the rule held, not on a budget or a request.
            "stopped_at_readiness": session.get("stop_reason") == READY,
            "limits": {
                name: limits[name] for name in LIMITS if type(limits.get(name)) in (int, float)
            },
            "debug_log": (session.get("logging") or {}).get("debug_sync") is True,
        },
        "readiness": {
            "ready": True,
            # The share of the channel set that changed between consecutive observations.
            "churn": [fraction(value) for value in churn],
            "rule": {
                "observations": OBSERVATIONS,
                "minimum_interval_seconds": MINIMUM_INTERVAL_SECONDS,
                "minimum_peers": MINIMUM_PEERS,
                "maximum_churn": MAXIMUM_CHURN,
            },
        },
        # One entry per historical synchronization, in order: what the peer listed, what
        # this observer lacked, and what it then requested.
        "sync_coverage": [
            {
                key: None if sync[key] is None else count(sync[key])
                for key in ("advertised", "new", "queried")
            }
            for sync in session["sync_coverage"]
        ],
        "inputs": {
            **digests(summary["inputs"], ("manifest_sha256",)),
            "session_result_sha256": sha256_file(session_result),
        },
    }


def hashes(value) -> set[str]:
    """Every value that a JSON document names as a hash: file hashes, blocks, the commit."""
    found = set()
    if isinstance(value, dict):
        if set(value) == {"height", "hash"}:
            found.add(value["hash"])
        for key, item in value.items():
            if key.endswith("_sha256") or key == "lnd_commit":
                found.add(item)
            else:
                found |= hashes(item)
    elif isinstance(value, list):
        for item in value:
            found |= hashes(item)
    return found


def strings(value):
    """Every string value of a JSON document."""
    if isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def scan(folder: Path) -> None:
    """Refuse a results folder that holds anything shaped like private data.

    The scan knows the forms that a capture and a session hold: keys, scripts, signed
    messages, outpoints, channel identifiers, addresses and paths. It reads the files as
    text and every JSON string once more as decoded. It cannot recognise a host or user
    name, a node's alias, a value in another encoding or one spread over several fields,
    which is why the files are built from field lists first.
    """
    for path in sorted(folder.iterdir()):
        if path.name not in FOLDER_FILES:
            raise ValueError(f"{path.name} does not belong in a results folder")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{path.name} is not a regular file")
    documents = {path.name: load_json(path) for path in sorted(folder.glob("*.json"))}
    allowed = set(documents["checksums.json"].values())
    for document in documents.values():
        allowed |= hashes(document)
    if not all(
        type(text) is str and (HASH.fullmatch(text) or COMMIT.fullmatch(text)) for text in allowed
    ):
        raise ValueError("a field named as a hash holds something else; nothing published")

    def check(name: str, text: str) -> None:
        for what, pattern in FORBIDDEN:
            if pattern.search(text):
                raise ValueError(f"{name} holds what looks like {what}; nothing published")
        if any(match not in allowed for match in HEX_RUN.findall(text)):
            raise ValueError(f"{name} holds an unexpected hash or key; nothing published")

    for path in sorted(folder.iterdir()):
        if path.suffix in SCANNED:
            check(path.name, path.read_text())
    for name, document in documents.items():
        for text in strings(document):
            # No published value is a path, so none needs a separator.
            if "/" in text or "\\" in text or text.startswith("~"):
                raise ValueError(f"{name} holds what looks like a local path; nothing published")
            check(name, text)
    with (folder / "age-cdf.csv").open(newline="") as stream:
        if any(int(row["count"]) < MINIMUM_ROW for row in csv.DictReader(stream)):
            raise ValueError(f"a distribution row describes fewer than {MINIMUM_ROW} announcements")


def overview(snapshot: dict, value: dict | None, missing: str | None = None) -> str:
    """README.md of a results folder: what each file is and the headline numbers.

    `missing` is the reason why a folder with all data files has no macro file.
    """
    counts, recency = snapshot["counts"], snapshot["counts"]["update_recency"]
    rows = [
        ("Snapshot block", f"{snapshot['reference']['height']:,}"),
        ("Observed at (UTC)", snapshot["captured_at"]),
        ("Authenticated channel announcements", f"{counts['authenticated_announcements']:,}"),
        ("Distinct funding keys", f"{counts['distinct_funding_keys']:,}"),
        ("Announcements with a recent update observed", f"{recency[RECENCY[0]]['channels']:,}"),
        ("Announcements with no recent update observed", f"{recency[RECENCY[1]]['channels']:,}"),
    ]
    if value is not None:
        audit = value["core_audit"]
        total = audit["confirmed_unspent_at_reference"] if audit else value
        rows.append(
            (
                "Funding value shown unspent at the snapshot block (BTC)"
                if audit
                else "Funding value (BTC)",
                total["funding_value_btc"] or "not published",
            )
        )
    age = snapshot["funding_output_age"]
    if age is not None:
        old = age[f"outputs_at_least_{OLD_YEARS}_years"]
        rows += [
            ("Median funding-output age (nominal years)", f"{age['median_tenths'] / 10:.1f}"),
            ("90th percentile of that age (nominal years)", f"{age['p90_tenths'] / 10:.1f}"),
            (
                f"Outputs of {OLD_YEARS} nominal years or more",
                "not published" if old is None else f"{old:,}",
            ),
        ]
    label = (
        ""
        if snapshot["dataset_kind"] == "mainnet"
        else "**Synthetic data, not a measurement.**\n\n"
    )
    table = "\n".join(f"| {name} | {figure} |" for name, figure in rows)
    return (
        "# Published results\n\n"
        f"{label}"
        "Written by `gossip publish`. Do not edit: publish again and replace the folder.\n\n"
        "| Quantity | Value |\n|---|---|\n"
        f"{table}\n\n"
        "Ages cover the announcements with a recent update observed and are rounded to a "
        "tenth of a nominal year (52,560 blocks).\n\n"
        "| File | Content |\n|---|---|\n"
        "| `snapshot.json` | Counts, funding keys, update recency, closed-index audit and age "
        "quantiles |\n"
        "| `age-cdf.csv` | Age distribution in twelfths of a nominal year, at least "
        f"{MINIMUM_ROW} announcements per row |\n"
        "| `funding-value.json` | Funding value and its audit against Bitcoin Core, when "
        "measured |\n"
        "| `observers.json` | Agreement with a second observer, when compared |\n"
        "| `collection.json` | Settings, readiness and synchronization coverage of the "
        "session, when published |\n"
        "| `checksums.json` | SHA-256 of the files above |\n"
        "| `age-cdf.pdf`, `age-cdf.png` | The figure, drawn from `snapshot.json` and "
        "`age-cdf.csv` |\n"
        "| `gossip_numbers.tex` | The reported numbers as TeX macros, for mainnet results "
        "with all four JSON files |\n"
        + (f"\nNo macro file was written: {missing}.\n" if missing else "")
    )


def write_results(
    output: Path,
    snapshot: dict,
    rows: list[dict],
    *,
    value: dict | None = None,
    observers: dict | None = None,
    collection: dict | None = None,
) -> dict:
    """Write, scan and complete a results folder. Nothing is left behind on failure."""
    # Imported here: both modules read results folders through this one.
    from .figures import draw
    from .paper_numbers import numbers_text

    if output.exists() or output.is_symlink():
        raise ValueError("output directory exists; choose a new destination")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".gossip-results-", dir=output.parent))
    try:
        write_json(staging / "snapshot.json", snapshot)
        (staging / "age-cdf.csv").write_text(cdf_csv(rows))
        for name, document in (
            ("funding-value.json", value),
            ("observers.json", observers),
            ("collection.json", collection),
        ):
            if document is not None:
                write_json(staging / name, document)
        write_json(
            staging / "checksums.json",
            {name: sha256_file(staging / name) for name in DATA_FILES if (staging / name).exists()},
        )
        results = load_results(staging)
        missing = None
        if snapshot["dataset_kind"] == "mainnet" and all(
            results[name] is not None for name in ("value", "observers", "collection")
        ):
            try:
                (staging / "gossip_numbers.tex").write_text(numbers_text(results)[0])
            except ValueError as reason:
                # The data files stand without the macros. The folder's README says why
                # the numbers that the paper reports do not follow from them.
                missing = str(reason)
        (staging / "README.md").write_text(overview(snapshot, results["value"], missing))
        if rows:
            draw(results, staging)
        scan(staging)
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return snapshot


def publish(
    capture: Path,
    output: Path,
    *,
    value: Path | None = None,
    sample: Path | None = None,
    observers: Path | None = None,
    other: Path | None = None,
    session_result: Path | None = None,
    workers: int = 0,
) -> dict:
    """Analyze a capture again and write its public results folder.

    The value artifact is replayed against the capture and its final sample, and the
    observer comparison is recomputed from both captures, before either is published.
    """
    if (value is None) != (sample is None):
        raise ValueError("a value artifact is published together with its final sample")
    if (observers is None) != (other is None):
        raise ValueError("an observer comparison is published together with the other capture")
    # One-block bins give the exact ages, from which the published rows are counted.
    summary, exact = calculate(capture, 1, workers=workers)
    audit = summary["closed_index"]["audit"]
    if audit is not None and audit["unresolved"]:
        raise ValueError(
            f"the closed-index audit left {audit['unresolved']} entries unchecked; "
            "such a capture is not published"
        )
    ages = recent_ages(exact)
    rows = coarse_cdf(ages)
    return write_results(
        output,
        snapshot_document(summary, ages, rows),
        rows,
        value=(
            value_document(verify_value(capture, sample, value, workers=workers))
            if value is not None
            else None
        ),
        observers=(
            observers_document(verify_observers(capture, other, observers, workers=workers))
            if observers is not None
            else None
        ),
        collection=(
            collection_document(summary, capture, session_result)
            if session_result is not None
            else None
        ),
    )


def load_results(folder: Path) -> dict:
    """The data files of a results folder, each checked against its recorded checksum."""
    checksums = load_json(folder / "checksums.json")
    if not isinstance(checksums, dict) or not {"snapshot.json", "age-cdf.csv"} <= set(
        checksums
    ) <= set(DATA_FILES):
        raise ValueError("results folder lists an unexpected set of files")
    for name, digest in checksums.items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ValueError(f"{name} does not match its recorded checksum")
    snapshot = load_json(folder / "snapshot.json")
    if snapshot.get("schema_version") != RESULTS_VERSION:
        raise ValueError("results folder is in another format; publish the capture again")
    with (folder / "age-cdf.csv").open(newline="") as stream:
        rows = [
            {
                "age_from_blocks": int(row["age_from_blocks"]),
                "age_below_blocks": int(row["age_below_blocks"]),
                "count": int(row["count"]),
                "cumulative_count": int(row["cumulative_count"]),
                "cumulative_fraction": float(row["cumulative_fraction"]),
            }
            for row in csv.DictReader(stream)
        ]

    def optional(name):
        return load_json(folder / name) if name in checksums else None

    return {
        "snapshot": snapshot,
        "rows": rows,
        "value": optional("funding-value.json"),
        "observers": optional("observers.json"),
        "collection": optional("collection.json"),
        "checksums": checksums,
    }
