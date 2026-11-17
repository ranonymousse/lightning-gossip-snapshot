"""TeX macros for the paper's snapshot numbers, generated from a results folder.

The manuscript inputs the file once and names each reported value by its macro, so a new
capture replaces every number in one step. Only published aggregates are read: the
snapshot counts, the audited funding value, the session's readiness and synchronization
coverage, and the comparison with a second observer. Counts and value cover every
announcement, ages the announcements with a recent update observed. Method constants (the
14-day update window, the 52,560-block nominal year) stay in the prose, and so does
anything that is known from private logs alone.
"""

import math
import re
from datetime import datetime
from pathlib import Path

from .funding_value import RECENCY, SATOSHIS_PER_BTC
from .publish import (
    MINIMUM_ROW,
    OLD_YEARS,
    RESULTS_VERSION,
    YOUNG_YEARS,
    load_results,
    percent,
    share_younger,
)

INPUTS = ("snapshot.json", "funding-value.json", "observers.json", "collection.json")
WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
)


def number(value):
    """A number as a \\num macro, which groups its digits as the paper does."""
    return f"\\num{{{value}}}"


def word(value):
    """Small counts are written out in the prose."""
    return WORDS[value] if value < len(WORDS) else number(value)


def ordinal_date(timestamp):
    """UTC date in the paper's form: day with an upright ordinal, month and year."""
    day = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    suffix = (
        "th" if 10 <= day.day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day.day % 10, "th")
    )
    return f"{day.day}\\textsuperscript{{\\upshape {suffix}}}~{day:%B} {day.year}"


def years(tenths):
    """A published age in tenths of a nominal year, written with one decimal."""
    return f"{tenths // 10}.{tenths % 10}"


def significant(value, digits=2):
    """A value rounded to `digits` significant digits, but never left of the decimal point."""
    if value <= 0:
        return "0"
    return f"{value:.{max(digits - 1 - math.floor(math.log10(value)), 0)}f}"


def first_complete(syncs):
    """Index of the first historical sync that requested every channel the node lacked.

    A synchronization in which the peer listed nothing new counts as well: the node then
    lacked nothing that this peer knew.
    """
    for i, sync in enumerate(syncs):
        if sync["new"] is not None and sync["queried"] >= sync["new"]:
            return i
    raise ValueError("no historical synchronization requested every channel it lacked")


def numbers_text(results: dict, *, synthetic: bool = False) -> tuple[str, dict]:
    """The macro file for loaded results, and a record of its inputs."""
    snapshot, funding = results["snapshot"], results["value"]
    session, cross = results["collection"], results["observers"]
    if funding is None or session is None or cross is None:
        raise ValueError(
            "the numbers need the funding value, the observer comparison and the "
            "collection record in the results folder"
        )
    if snapshot["dataset_kind"] != "mainnet" and not synthetic:
        raise ValueError("paper numbers need a mainnet result")
    counts, recency = snapshot["counts"], snapshot["counts"]["update_recency"]
    split = funding.get("by_update_recency")
    if (
        funding.get("schema_version") != RESULTS_VERSION
        or any(funding["inputs"].get(name) != digest for name, digest in snapshot["inputs"].items())
        or (funding["reference"], funding["captured_at"])
        != (snapshot["reference"], snapshot["captured_at"])
        or funding["included_outputs"] != counts["authenticated_announcements"]
        or any(split[name]["outputs"] != recency[name]["channels"] for name in RECENCY)
    ):
        raise ValueError("funding-value summary describes a different capture or format")
    if (
        session["captured_at"],
        session["reference"],
        session["included_records"],
        session["inputs"]["manifest_sha256"],
    ) != (
        snapshot["captured_at"],
        snapshot["reference"],
        snapshot["included_records"],
        snapshot["inputs"]["manifest_sha256"],
    ):
        raise ValueError("collection record is not the session of these results")
    seen, back = cross["observer_in_second"], cross["second_in_observer"]
    if (
        cross["dataset_kind"] != snapshot["dataset_kind"]
        or cross["observer"]["inputs"] != snapshot["inputs"]
        or any(seen[name]["channels"] != recency[name]["channels"] for name in RECENCY)
    ):
        raise ValueError("observer comparison does not start from this capture")
    audit = funding["core_audit"]
    if audit is None:
        raise ValueError("the paper's value needs the Core audit; run funding-value --core-dir")
    ages, closed = snapshot["funding_output_age"], snapshot["closed_index"]
    if ages is None:
        raise ValueError("too few announcements with a recent update observed to report ages")
    old = ages[f"outputs_at_least_{OLD_YEARS}_years"]
    young, aged = (share_younger(results["rows"], years) for years in (YOUNG_YEARS, OLD_YEARS))
    if old is None or young is None or aged is None:
        raise ValueError(
            f"the outputs younger than {YOUNG_YEARS} or of {OLD_YEARS} nominal years or more "
            "do not follow from the published age rows"
        )
    if (1 - aged) * recency[RECENCY[0]]["channels"] != old:
        raise ValueError("the age rows and the count of old outputs disagree")

    syncs = session["sync_coverage"]
    complete = first_complete(syncs)
    later = [sync["new"] for sync in syncs[complete + 1 :] if sync["new"] is not None]
    churn = 100 * max(session["readiness"]["churn"])
    confirmed, states = audit["confirmed_unspent_at_reference"], audit["outputs_by_state"]
    # Only value that Core showed unspent at the reference block is reported.
    total = confirmed["funding_value_satoshis"]
    everything = funding["funding_value_satoshis"]
    recent_value = split[RECENCY[0]]["funding_value_satoshis"]
    if total is None or everything is None or recent_value is None:
        raise ValueError(
            "a funding value is withheld, because it would describe or give away the value "
            f"of fewer than {MINIMUM_ROW} outputs or because the audit did not show every "
            "output unspent"
        )
    audit_note = (
        f"{confirmed['outputs']} of {audit['outputs_checked']} outputs shown unspent at the "
        f"reference block ({confirmed['funding_value_btc']} BTC), "
        f"{states['spent_after_reference']} of them through a later spend, "
        f"{states['not_shown']} not shown"
    )
    without = recency[RECENCY[1]]
    second = cross["second_observer"]

    macros = [
        (
            "SnapshotDate",
            ordinal_date(snapshot["captured_at"]),
            "UTC date of the final observation",
        ),
        ("ReferenceBlock", number(snapshot["reference"]["height"]), "snapshot block height"),
        (
            "Announcements",
            number(counts["authenticated_announcements"]),
            "authenticated announcements, one funding output each",
        ),
        ("FundingKeys", number(counts["distinct_funding_keys"]), "distinct funding keys"),
        (
            "ValueBTC",
            number((total + SATOSHIS_PER_BTC // 2) // SATOSHIS_PER_BTC),
            f"funding value in whole BTC, exactly {total} satoshis",
        ),
        (
            "RecentAnnouncements",
            number(recency[RECENCY[0]]["channels"]),
            "announcements with a recent update observed",
        ),
        (
            "RecentValueShare",
            f"{100 * recent_value // everything}\\%",
            "their share of the funding value, rounded down",
        ),
        (
            "NoRecentAnnouncements",
            number(without["channels"]),
            "announcements with no recent update observed",
        ),
        (
            "RecentAgeMedian",
            years(ages["median_tenths"]),
            "median output age with a recent update observed, in nominal years",
        ),
        ("RecentAgeNinetieth", years(ages["p90_tenths"]), "their 90th percentile of output age"),
        (
            "RecentYoungShare",
            f"{percent(young)}\\%",
            f"their outputs younger than {YOUNG_YEARS} nominal year, in whole percent",
        ),
        (
            "OldRecentOutputs",
            number(old),
            f"their outputs of {OLD_YEARS} nominal years or more",
        ),
        ("OldRecentShare", f"{percent(1 - aged)}\\%", "the same as a share, in whole percent"),
        (
            "AuditSpentLater",
            word(states["spent_after_reference"]),
            "outputs the Core audit found spent in a block after the reference",
        ),
        ("Churn", f"{significant(churn)}\\%", "larger channel-set change between observations"),
        (
            "LaterSyncNew",
            word(sum(later)),
            "new channels in the synchronizations after the first complete one",
        ),
        ("ClosedEntries", word(closed["entries"]), "entries in the closed-channel index"),
        (
            "ClosedAuditBlock",
            number(closed["audit"]["tip"]["height"]),
            "chain tip of the closed-index audit",
        ),
        (
            "CrossRecentHeld",
            number(seen[RECENCY[0]]["held"]),
            "announcements with a recent update that the second observer also held",
        ),
        (
            "CrossNoRecentHeld",
            number(seen[RECENCY[1]]["held"]),
            "announcements without a recent update that the second observer also held",
        ),
    ]
    if not all(re.fullmatch(r"[A-Za-z]+", name) for name, _, _ in macros):
        raise ValueError("a macro name may hold letters only")
    record = {
        "macros": len(macros),
        "dataset_kind": snapshot["dataset_kind"],
        "captured_at": snapshot["captured_at"],
        "reference": snapshot["reference"],
        "inputs_sha256": {name: results["checksums"][name] for name in INPUTS},
    }
    listed = ", ".join("no list" if sync["new"] is None else str(sync["new"]) for sync in syncs)
    lines = [
        "% Generated by `gossip paper-numbers` from a published results folder. Do not edit",
        "% by hand: generate it again and replace the file. Use each macro with a pair of",
        "% braces, as in \\gossipAnnouncements{} channel announcements.",
        f"% Snapshot {snapshot['captured_at']}, block {snapshot['reference']['height']}, "
        f"data set: {snapshot['dataset_kind']}.",
        *(f"% {name} {digest}" for name, digest in record["inputs_sha256"].items()),
        "%",
        "% Not generated, because the results folder does not hold them: a currency",
        "% equivalent of the value, and anything about single peers.",
        "%",
        "% Further aggregates of the same folder, for sentences that use no macro:",
        f"%   announcements without a recent update that have no stored update at all: "
        f"{without['channels_without_any_update']} of {without['channels']}",
        f"%   new channels per synchronization: {listed}",
        f"%   first sync that requested every channel it lacked: number {complete + 1}, "
        f"followed by {len(later)} with a channel list",
        f"%   closed-index entries unspent or unresolved at the audit: "
        f"{closed['audit']['unspent_at_audit']} and {closed['audit']['unresolved']}",
        f"%   Core audit of the funding outputs: {audit_note}",
        f"%   second observer, captured {second['captured_at']} at block "
        f"{second['reference']['height']}, for each group here: held, not held",
        *(f"%     {name}: {seen[name]['held']}, {seen[name]['not_held']}" for name in RECENCY),
        "%   and for each group of the second observer, here: held, not held",
        *(f"%     {name}: {back[name]['held']}, {back[name]['not_held']}" for name in RECENCY),
    ]
    if synthetic or snapshot["dataset_kind"] != "mainnet":
        lines.insert(0, "% SYNTHETIC DATA. Never use this file in the paper.")
    lines += [f"\\newcommand{{\\gossip{name}}}{{{body}}}% {note}" for name, body, note in macros]
    return "\n".join(lines) + "\n", record


def write_numbers(results: Path, output: Path) -> dict:
    """Write the macro file for a results folder; returns the record of its inputs."""
    if output.exists() or output.is_symlink():
        raise ValueError("output file exists; choose a new destination")
    text, record = numbers_text(load_results(Path(results)))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)
    return record
