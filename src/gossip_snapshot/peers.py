"""Bounded recovery of public gossip connections, without changing LND validation.

Candidates come from the local graph, never a bundled list of preferred nodes.
All network actions use injected, narrowly scoped RPC callbacks (offline in tests).
"""

import ipaddress
import json
import random
import re
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from .common import MIB
from .readiness import MINIMUM_PEERS

# Peers that a session keeps connected, and the minutes without graph progress after
# which it stops. Both can be set per session within these bounds.
DEFAULT_TARGET_PEERS = 6
MIN_TARGET_PEERS, MAX_TARGET_PEERS = 3, 12
DEFAULT_STALL_MINUTES = 45
MIN_STALL_MINUTES, MAX_STALL_MINUTES = 30, 180
CHECK_SECONDS = 60  # Interval between two recovery checks.
REFRESH_SECONDS = 600  # Interval between two readings of dial candidates from the graph.
RETRY_SECONDS = 600  # Wait before a peer is dialed again. It doubles with each failed dial.
MAX_RETRY_SECONDS = 3600  # Ceiling of that wait, which also applies to a replaced sync peer.
# At most one peer is replaced per interval. A stalled graph replaces only a peer that
# has been connected this long.
ROTATE_SECONDS = 300
# Without graph progress for this long, the least active peer is replaced.
ROTATE_AFTER_SECONDS = 1200
# Without progress for this long, the peer that serves the historical sync is replaced.
SYNC_STALL_SECONDS = 600
CONNECT_TIMEOUT = 8  # Seconds that lncli gives one dial.
PARALLEL_DIALS = 2  # Dials per check, made at the same time.
# Rejected range replies after which a peer is replaced and not dialed again.
BAD_REPLY_LIMIT = 3
GRAPH_SECONDS = 30  # Time limit for reading the graph that the candidates come from.
ERROR_TAIL = 500  # Characters of an error message that the status and the events keep.
# What one check reads of LND's log at most, and in which pieces. The rest waits for the
# next check, and an unfinished line is carried over up to PARTIAL_LINE characters.
LOG_READ_BUDGET = 16 * MIB
LOG_CHUNK = MIB
PARTIAL_LINE = 8192
QUERY_ERRORS = re.compile(
    r"Process query msg from peer ([0-9a-f]{66}) got unexpected msg "
    r"\*lnwire\.ReplyChannelRange received in state chansSynced"
)
PONG_FAILURES = re.compile(r"Peer\(([0-9a-f]{66})\): pong response failure")
# Pinned LND scores a peer for each announcement of a channel in the observer's
# own closed-channel index ("outdated") or failing validation ("invalid"), and
# disconnects it at the ban threshold. The Core configuration disables the ban.
REJECTED = re.compile(
    r"Increasing ban score for peer=\S+ due to (outdated|invalid) channel announcement"
)
BAN_DISCONNECT = "reason: peer has bypassed ban threshold"
# Pinned LND states graph progress exactly in these INFO lines. The channel count
# of getnetworkinfo is not used: at the pinned commit its loop leaves a node at the
# first channel already counted, so it undercounts and varies on a fixed graph.
PROCESSED = re.compile(r"GRPH: Processed channels=(\d+) ")
SYNC = re.compile(
    r"GossipSyncer\(([0-9a-f]{66})\): (?:"
    r"requesting new chans from height=(\d+) |"
    r"filtering through (\d+) chans|"
    r"starting query for (\d+) new chans|"
    r"(remote peer has no new chans)|"
    r"querying for (\d+) new channels)"
)
RPC_ERRORS = (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)


def public_address(address):
    """Accept literal public TCP addresses only; no DNS, Tor or local-network dials."""
    try:
        host, port = address.rsplit(":", 1)
        ip = ipaddress.ip_address(host.strip("[]"))
        if not ip.is_global or ip.is_multicast or not port.isdecimal() or not 0 < int(port) < 65536:
            return None
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        return f"{host}:{int(port)}"
    except (ValueError, AttributeError):
        return None


def candidates(graph, own_key):
    """One public address per advertised identity, preferring IPv4 when available."""
    result = {}
    for node in graph.get("nodes", []):
        key = node.get("pub_key", "")
        if key == own_key or not re.fullmatch(r"0[23][0-9a-f]{64}", key):
            continue
        addresses = {
            value
            for item in node.get("addresses", [])
            if item.get("network") in ("tcp", "tcp4", "tcp6")
            if (value := public_address(item.get("addr", "")))
        }
        if addresses:
            result[key] = min(addresses, key=lambda address: (address.startswith("["), address))
    return result


def check_policy(target, stall_minutes):
    """Refuse a peer target or a stall timeout outside the supported range."""
    if type(target) is not int or not MIN_TARGET_PEERS <= target <= MAX_TARGET_PEERS:
        raise ValueError(f"peer target must be between {MIN_TARGET_PEERS} and {MAX_TARGET_PEERS}")
    if (
        type(stall_minutes) is not int
        or not MIN_STALL_MINUTES <= stall_minutes <= MAX_STALL_MINUTES
    ):
        raise ValueError(
            f"graph stall timeout must be between {MIN_STALL_MINUTES} and "
            f"{MAX_STALL_MINUTES} minutes"
        )


class PeerRecovery:
    """Keeps an observer connected to public peers while its graph synchronizes.

    Each `tick` reads LND's log for progress, dials candidates from the local graph up to
    the target, and replaces at most one peer that holds up a stalled synchronization.
    Without progress for the stall timeout, it asks the session to stop.
    """

    def __init__(
        self,
        session,
        rpc,
        mutate,
        *,
        target=DEFAULT_TARGET_PEERS,
        stall_minutes=DEFAULT_STALL_MINUTES,
        clock=time.monotonic,
    ):
        check_policy(target, stall_minutes)
        self.session, self.rpc, self.mutate, self.clock = session, rpc, mutate, clock
        self.target, self.stall_seconds = target, stall_minutes * 60
        self.rng = random.Random(session.name)
        self.next_check = self.next_refresh = self.next_rotation = 0
        self.pool, self.retry_at, self.connected_since = {}, {}, {}
        self.bad_replies = Counter()
        self.dial_failures = Counter()
        self.pong_failures = Counter()
        self.pong_disconnects = 0
        self.rejected = Counter()
        self.ban_disconnects = 0
        self.log_offset, self.log_partial = 0, ""
        # Historical syncs seen in the log, as counts only, and their serving peer.
        self.processed, self.syncs, self.sync_of, self.sync_peer = 0, [], {}, None
        self.changed_at = self.seen_at = clock()
        self.status = {"target": target, "attempts": 0, "connections": 0, "rotations": 0}

    def policy(self):
        """The parameters of this recovery, as a session records them at its start."""
        return {
            "target_peers": self.target,
            "candidate_source": "local graph, public literal TCP addresses",
            "candidate_shuffle_seed": self.session.name,
            "check_seconds": CHECK_SECONDS,
            "retry_seconds": RETRY_SECONDS,
            "max_retry_seconds": MAX_RETRY_SECONDS,
            "retry_policy": "exponential backoff for failed dials; prefer unfailed candidates",
            "parallel_dials": PARALLEL_DIALS,
            "connect_timeout_seconds": CONNECT_TIMEOUT,
            "rotate_after_seconds": ROTATE_AFTER_SECONDS,
            "sync_stall_seconds": SYNC_STALL_SECONDS,
            "progress_source": "LND log: channels processed and channel queries sent",
            "stall_timeout_seconds": self.stall_seconds,
            "invalid_range_reply_threshold": BAD_REPLY_LIMIT,
        }

    def event(self, action, **fields):
        """Append one timestamped action to the session's private peer-events.jsonl."""
        with (self.session / "peer-events.jsonl").open("a") as stream:
            stream.write(
                json.dumps({"at": datetime.now(UTC).isoformat(), "action": action, **fields}) + "\n"
            )

    def sync_line(self, match):
        """Follow one historical sync: what the peer has, what is new, what was asked."""
        peer, height, advertised, new, none, queried = match.groups()
        if height is not None:
            self.sync_of.pop(peer, None)
            if height == "0":
                self.sync_peer = peer
                # Counts only: what the peer lists after LND's staleness filter, how
                # much of it this observer lacked, and how much it then asked for.
                self.sync_of[peer] = {
                    "seen_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "advertised": None,
                    "new": None,
                    "queried": 0,
                }
                self.syncs.append(self.sync_of[peer])
            return False
        record = self.sync_of.get(peer)
        if record is None:
            return False
        if advertised:
            record["advertised"] = int(advertised)
        elif new or none:
            record["new"] = int(new or 0)
        else:
            record["queried"] += int(queried)
        return bool(queried)

    def read_log(self):
        """Count log markers; return whether the graph or a channel query advanced."""
        path = self.session / "lnd.log"
        progress = False
        if not path.exists():
            return progress
        with path.open("rb") as stream:
            if path.stat().st_size < self.log_offset:
                self.log_offset, self.log_partial = 0, ""
            stream.seek(self.log_offset)
            remaining = LOG_READ_BUDGET
            while remaining:
                chunk = stream.read(min(LOG_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                text = self.log_partial + chunk.decode("utf-8", errors="replace")
                lines = text.split("\n")
                self.log_partial = lines.pop()[-PARTIAL_LINE:]
                for line in lines:
                    match = QUERY_ERRORS.search(line)
                    if match:
                        self.bad_replies[match[1]] += 1
                    if match := REJECTED.search(line):
                        self.rejected[match[1]] += 1
                    if BAN_DISCONNECT in line:
                        self.ban_disconnects += 1
                    if match := PROCESSED.search(line):
                        self.processed += int(match[1])
                        progress = progress or match[1] != "0"
                    if match := SYNC.search(line):
                        progress = self.sync_line(match) or progress
                    if match := PONG_FAILURES.search(line):
                        self.pong_failures[match[1]] += 1
                        disconnected = "-- disconnecting" in line
                        self.pong_disconnects += int(disconnected)
                        self.event("pong_failure", pub_key=match[1], disconnected=disconnected)
            self.log_offset = stream.tell()
            self.status["log_backlog_bytes"] = max(0, path.stat().st_size - self.log_offset)
        return progress

    def dial(self, key, address):
        """Ask LND to connect to one peer; returns the peer and the error, if there was one."""
        try:
            self.mutate("connect", "--timeout", f"{CONNECT_TIMEOUT}s", f"{key}@{address}")
            return key, address, None
        except RPC_ERRORS as exc:
            return key, address, str(exc)[-ERROR_TAIL:]

    def tick(self, info):
        """One recovery step for the node's latest getinfo reply; returns the status to show.

        A step does its work at most once per `CHECK_SECONDS` and only while the chain is
        synchronized.
        """
        now = self.clock()
        waited, self.seen_at = now - self.seen_at, now
        if not info.get("synced_to_chain"):
            # Each new block unsets the flag briefly: pause the stall clock, never reset it.
            self.changed_at += waited
            return dict(self.status)
        if now < self.next_check:
            return dict(self.status)
        self.next_check = now + CHECK_SECONDS
        try:
            # This collector is for an empty observer, never a channel-operating node.
            if any(
                info.get(name) != 0
                for name in ("num_active_channels", "num_inactive_channels", "num_pending_channels")
            ):
                raise ValueError(
                    "automatic peer recovery requires an empty observer with no channels"
                )
            peers = self.rpc("listpeers")["peers"]
            if self.read_log() or info.get("synced_to_graph"):
                self.changed_at = now
            stalled = now - self.changed_at
            sync = self.syncs[-1] if self.syncs else {}
            self.status.update(
                graph_checked_at=datetime.now(UTC).isoformat(),
                processed_channels=self.processed,
                historical_syncs=len(self.syncs),
                sync_advertised=sync.get("advertised"),
                sync_new=sync.get("new"),
                sync_queried=sync.get("queried"),
                stalled_seconds=round(stalled),
                connected=len(peers),
                active_syncers=sum(
                    p.get("sync_type") in ("ACTIVE_SYNC", "PINNED_SYNC") for p in peers
                ),
                warning="",
                stop_reason=None,
            )
            self.status.pop("last_error", None)
            keys = {p["pub_key"] for p in peers}
            self.connected_since = {key: self.connected_since.get(key, now) for key in keys}
            self.status.update(
                pong_failures=sum(self.pong_failures.values()),
                pong_disconnects=self.pong_disconnects,
                peers_with_pong_failures=len(self.pong_failures),
                rejected_outdated=self.rejected["outdated"],
                rejected_invalid=self.rejected["invalid"],
                ban_disconnects=self.ban_disconnects,
            )
            blocked = {key for key, count in self.bad_replies.items() if count >= BAD_REPLY_LIMIT}
            if not info.get("synced_to_graph") and stalled >= self.stall_seconds:
                self.status["stop_reason"] = "graph stalled despite peer recovery"
                self.status["warning"] = "No graph progress; preserving diagnostics"
                return dict(self.status)

            if now >= self.next_refresh and (
                len(peers) < self.target or stalled >= ROTATE_AFTER_SECONDS
            ):
                self.pool = candidates(
                    self.rpc("describegraph", timeout=GRAPH_SECONDS), info["identity_pubkey"]
                )
                self.next_refresh = now + REFRESH_SECONDS
                self.event("candidates", count=len(self.pool), addresses=self.pool)
            self.status["candidates"] = len(self.pool)
            choices = [
                (key, address)
                for key, address in sorted(self.pool.items())
                if key not in keys and key not in blocked and now >= self.retry_at.get(key, 0)
            ]
            self.rng.shuffle(choices)
            # Try other advertised peers before repeatedly dialing an endpoint
            # that has failed. Random order remains within each failure tier.
            choices.sort(key=lambda pair: self.dial_failures[pair[0]])
            choices = choices[: min(PARALLEL_DIALS, max(0, self.target - len(peers)))]
            # The read-only RPC account remains the default. Only these two
            # peer operations use the observer's admin macaroon, through mutate.
            if choices:
                with ThreadPoolExecutor(max_workers=PARALLEL_DIALS) as pool:
                    results = list(pool.map(lambda pair: self.dial(*pair), choices))
                for key, address, error in results:
                    self.status["attempts"] += 1
                    if error is None:
                        self.status["connections"] += 1
                        self.dial_failures[key] = 0
                    else:
                        self.dial_failures[key] += 1
                    delay = min(
                        MAX_RETRY_SECONDS,
                        RETRY_SECONDS * 2 ** min(max(self.dial_failures[key] - 1, 0), 3),
                    )
                    self.retry_at[key] = now + delay
                    self.event(
                        "connect",
                        pub_key=key,
                        address=address,
                        error=error,
                        retry_after_seconds=delay,
                    )
                peers = self.rpc("listpeers")["peers"]
                self.status["connected"] = len(peers)

            # Rotate at most one established peer per five minutes, only while
            # graph sync is incomplete. Preserve at least one connection.
            if not info.get("synced_to_graph") and now >= self.next_rotation and len(peers) > 1:
                problem = [p for p in peers if p["pub_key"] in blocked]
                reason = "rejected range replies"
                if not problem and stalled >= SYNC_STALL_SECONDS:
                    # LND waits without limit for the end of a channel query and
                    # replaces the sync peer at once when that peer disconnects.
                    problem = [p for p in peers if p["pub_key"] == self.sync_peer]
                    reason = "historical sync query unanswered"
                if not problem and stalled >= ROTATE_AFTER_SECONDS:
                    reason = "graph progress stalled"
                    problem = [
                        p
                        for p in peers
                        if now - self.connected_since.get(p["pub_key"], now) >= ROTATE_SECONDS
                    ]
                    problem.sort(
                        key=lambda p: (
                            p.get("sync_type") != "PASSIVE_SYNC",
                            int(p.get("bytes_recv", 0)),
                        )
                    )
                if problem:
                    key = problem[0]["pub_key"]
                    self.next_rotation = now + ROTATE_SECONDS
                    self.retry_at[key] = now + (
                        MAX_RETRY_SECONDS if key == self.sync_peer else RETRY_SECONDS
                    )
                    self.mutate("disconnect", key)
                    self.status["rotations"] += 1
                    self.event("disconnect", pub_key=key, reason=reason)
                    peers = self.rpc("listpeers")["peers"]
            self.status["connected"] = len(peers)
            self.status["active_syncers"] = sum(
                p.get("sync_type") in ("ACTIVE_SYNC", "PINNED_SYNC") for p in peers
            )
            if stalled >= ROTATE_AFTER_SECONDS:
                self.status["warning"] = "Graph stalled; recovering peer connections"
            elif self.status["connected"] < MINIMUM_PEERS:
                self.status["warning"] = "Restoring Lightning peers"
        except RPC_ERRORS as exc:
            self.status["warning"] = "Peer recovery check failed; retrying"
            self.status["last_error"] = str(exc)[-ERROR_TAIL:]
            self.event("check_error", error=self.status["last_error"])
        return dict(self.status)
