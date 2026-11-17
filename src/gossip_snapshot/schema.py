"""Versioned interchange contract shared with the offline Go exporter."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAINNET = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
LND_COMMIT = "390b2d2b7bdcc335c183962e483f5daeceb8f0e7"
Hex32 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Pubkey = Annotated[str, Field(pattern=r"^(02|03)[0-9a-f]{64}$")]
Height = Annotated[int, Field(ge=0, lt=1 << 24)]
Timestamp = Annotated[int, Field(ge=0, lt=1 << 32)]
# BOLT7 lets a node prune a channel whose latest update is older than two weeks,
# and pinned LND does (DefaultChannelPruneExpiry). Used as the recency window.
RECENT_UPDATE_SECONDS = 14 * 24 * 3600
# One nominal year of blocks: Bitcoin's ten-minute target gives 144 blocks a day.
# Ages are exact in blocks, and the nominal year is the unit they are reported in.
YEAR_BLOCKS = 52560


class Model(BaseModel):
    """Base of every record: an unknown field is refused and no value is converted."""

    model_config = ConfigDict(extra="forbid", strict=True)


class BlockRef(Model):
    """A block, by height and hash."""

    height: Height
    hash: Hex32


class Record(Model):
    """One line of records.jsonl: a channel announcement as the exporter reconstructed it."""

    schema_version: Literal[2]
    provenance: Literal["reconstructed_from_lnd_graph"]
    gossip_version: Literal[1]
    chain_hash: Hex32
    scid: str
    funding_outpoint: str
    node_key_1: Pubkey
    node_key_2: Pubkey
    bitcoin_key_1: Pubkey | None
    bitcoin_key_2: Pubkey | None
    announcement_hex: str | None
    signature_verified: bool
    # These labels are cross-checked against signed bytes by the Python reader.
    funding_construction: Literal["p2wsh", "unsupported", "unknown"]
    exclusion_reason: Literal["missing_proof", "unsupported_features"] | None
    # Timestamps of the stored channel_update of node 1 and node 2, as recorded
    # by the observer's LND. No offline signature check covers them.
    last_update_1: Timestamp | None
    last_update_2: Timestamp | None

    @field_validator("scid")
    @classmethod
    def valid_scid(cls, value: str) -> str:
        """A channel identifier is a nonzero 64-bit number in canonical decimal form."""
        if not re.fullmatch(r"[1-9][0-9]*", value) or int(value) >= 1 << 64:
            raise ValueError("SCID must be a canonical nonzero uint64 decimal string")
        return value

    @field_validator("funding_outpoint")
    @classmethod
    def valid_outpoint(cls, value: str) -> str:
        """An outpoint is a transaction id with an output index that fits a channel identifier."""
        if not re.fullmatch(r"[0-9a-f]{64}:(0|[1-9][0-9]*)", value):
            raise ValueError("invalid funding outpoint")
        if int(value.split(":")[1]) > 65535:
            raise ValueError("outpoint index exceeds SCID range")
        return value

    @field_validator("announcement_hex")
    @classmethod
    def valid_wire(cls, value: str | None) -> str | None:
        """The signed message is lowercase hexadecimal and at most 65,535 bytes long."""
        if value is not None and (
            not re.fullmatch(r"(?:[0-9a-f]{2})+", value) or len(value) > 131070
        ):
            raise ValueError("invalid announcement hex")
        return value


class ExportReport(Model):
    """export-report.json: what the exporter read, from which database, and how much it wrote."""

    schema_version: Literal[2]
    lnd_commit: Literal[LND_COMMIT]
    database_sha256: Hex32
    records_sha256: Hex32
    graph_prune_tip: BlockRef
    exported_records: Annotated[int, Field(gt=0)]
    included_records: Annotated[int, Field(ge=0)]
    exclusions: dict[str, int]
    # Channels the observer marked closed and therefore never exported.
    closed_scids: Annotated[int, Field(ge=0)]
    closed_scids_sha256: Hex32


class ClosedAudit(Model):
    """Closed-index entries checked against Bitcoin Core's UTXO set."""

    schema_version: Literal[1]
    audited_at: str
    closed_scids_sha256: Hex32
    tip: BlockRef
    entries: Annotated[int, Field(ge=0)]
    # Not in the UTXO set: spent, or the output never existed.
    not_in_utxo_set: Annotated[int, Field(ge=0)]
    # Still unspent, so the observer excluded a live funding output.
    unspent_at_audit: Annotated[int, Field(ge=0)]
    unresolved: Annotated[int, Field(ge=0)]
    unspent_scids: list[str]
    unresolved_reasons: dict[str, int]

    @model_validator(mode="after")
    def coherent_counts(self):
        """The three outcomes add up to the entries, and the details match their counts."""
        if self.entries != self.not_in_utxo_set + self.unspent_at_audit + self.unresolved:
            raise ValueError("closed-index audit counts do not add up")
        if len(self.unspent_scids) != self.unspent_at_audit:
            raise ValueError("closed-index audit lists a different number of unspent entries")
        if sum(self.unresolved_reasons.values()) != self.unresolved:
            raise ValueError("closed-index audit reasons do not add up")
        return self


class Manifest(Model):
    """manifest.json: what a capture is, when and how it was taken, and the hashes of its files."""

    schema_version: Literal[1]
    dataset_kind: Literal["synthetic", "mainnet"]
    captured_at: str
    reference: BlockRef
    backend: Literal["fixture", "bitcoind"]
    validation_enabled_from_start: bool
    clean_shutdown: bool
    records_sha256: Hex32
    export_report_sha256: Hex32
    readiness_sha256: Hex32 | None = None
    closed_audit_sha256: Hex32 | None = None
    # Until the public release the collector called every mainnet capture a pilot and
    # wrote `pilot: true` here. The field is accepted so that such a capture keeps its
    # manifest and the hashes recorded over it. The collector no longer writes it and
    # nothing reads it.
    pilot: bool = False

    @field_validator("captured_at")
    @classmethod
    def utc_timestamp(cls, value: str) -> str:
        """The capture time is an ISO timestamp in UTC."""
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.utcoffset() != timedelta(0):
            raise ValueError("capture time must include UTC timezone")
        return value

    @model_validator(mode="after")
    def coherent_kind(self):
        """Only synthetic data uses the fixture backend.

        A mainnet capture attests validation from the first start, a clean stop, readiness
        and the closed-index audit.
        """
        if (self.dataset_kind == "synthetic") != (self.backend == "fixture"):
            raise ValueError("synthetic captures require fixture backend, mainnet cannot use it")
        if self.dataset_kind == "mainnet" and not (
            self.validation_enabled_from_start
            and self.clean_shutdown
            and self.readiness_sha256
            and self.closed_audit_sha256
        ):
            raise ValueError(
                "mainnet requires validated collection, clean stop, readiness record "
                "and closed-index audit"
            )
        return self


def decode_scid(scid: str) -> tuple[int, int, int]:
    """Decode without passing through float, including IDs above 2**53."""
    Record.valid_scid(scid)
    value = int(scid)
    return value >> 40, (value >> 16) & 0xFFFFFF, value & 0xFFFF


def load_json(path: Path):
    """Reject duplicate fields and nonstandard NaN/Infinity rather than guessing."""

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(
        path.read_text(), object_pairs_hook=unique_pairs, parse_constant=invalid_constant
    )
