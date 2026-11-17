"""Helpers that several modules share: the checkout's root, sizes, time, hashes and JSON files."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

# The checkout, which holds versions.json, uv.lock, config/ and the built binaries.
PROJECT = Path(__file__).resolve().parents[2]
GIB = 1024**3
MIB = 1024**2
COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # A git commit, in SHA-1 or SHA-256 form.


def utc_now() -> str:
    """The current UTC time to the second, as saved records carry it (ending in Z)."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    """SHA-256 of a file's content, in hexadecimal."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    """Write a value as indented JSON with sorted keys, so that equal values give equal files."""
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def save(path: Path, value) -> None:
    """Replace a small private status or metadata file atomically, in the same JSON form.

    Only its owner can read the file, and no reader ever sees it half written.
    """
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    temporary.chmod(0o600)
    temporary.replace(path)
