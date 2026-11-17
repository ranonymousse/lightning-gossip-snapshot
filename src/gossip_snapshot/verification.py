"""Bounded, ordered verification batches; aggregation stays in the parent."""

import json
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from itertools import islice
from multiprocessing import get_context
from typing import NamedTuple

from .performance import worker_count
from .schema import Record, decode_scid
from .wire import verify_record

BATCH_SIZE = 256


class VerifiedRecord(NamedTuple):
    line_number: int
    scid: str
    outpoint: str
    height: int
    funding_keys: tuple[str, str]
    exclusion: str | None
    # Latest stored channel_update timestamp of either node; None if neither exists.
    last_update: int | None


def verify_batch(lines):
    results = []
    for number, line in lines:
        if not line.strip():
            raise ValueError(f"blank record at line {number}")
        try:
            record = Record.model_validate_json(line, strict=True)
            pairs = json.loads(line, object_pairs_hook=list)
            if len({key for key, _ in pairs}) != len(pairs):
                raise ValueError("duplicate JSON field")
            reason = verify_record(record)
            results.append(
                VerifiedRecord(
                    number,
                    record.scid,
                    record.funding_outpoint,
                    decode_scid(record.scid)[0],
                    (record.bitcoin_key_1, record.bitcoin_key_2),
                    reason,
                    max(
                        (t for t in (record.last_update_1, record.last_update_2) if t is not None),
                        default=None,
                    ),
                )
            )
        except ValueError as exc:
            raise ValueError(f"invalid record at line {number}: {exc}") from exc
    return results


def verified_records(path, workers=0):
    """At most 2×workers batches in flight; stable input order and error locations.

    Small fixtures use one batch directly to avoid process startup overhead.
    Spawn avoids inheriting the supervisor's signal handlers or Rich's threads.
    """
    count = worker_count(workers)
    with path.open() as stream:
        lines = enumerate(stream, 1)
        batches = iter(lambda: list(islice(lines, BATCH_SIZE)), [])
        first = next(batches, [])
        second = next(batches, []) if count > 1 else []
        if count == 1 or not second:
            yield from verify_batch(first)
            for batch in batches:
                yield from verify_batch(batch)
            return
        with ProcessPoolExecutor(max_workers=count, mp_context=get_context("spawn")) as pool:
            pending = deque(pool.submit(verify_batch, batch) for batch in (first, second))
            try:
                for batch in islice(batches, count * 2 - 2):
                    pending.append(pool.submit(verify_batch, batch))
                while pending:
                    yield from pending.popleft().result()
                    if batch := next(batches, []):
                        pending.append(pool.submit(verify_batch, batch))
            except BrokenProcessPool as exc:
                raise ValueError(
                    "verification worker exited unexpectedly; no verified result"
                ) from exc
            finally:
                for future in pending:
                    future.cancel()
