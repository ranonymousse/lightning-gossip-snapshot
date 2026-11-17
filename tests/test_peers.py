"""Peer recovery and stall detection, exercised with fake RPCs only."""

from threading import Lock
from time import sleep

import pytest
from coincurve import PrivateKey

from gossip_snapshot.peers import PeerRecovery, candidates, public_address


def key(n):
    return PrivateKey(n.to_bytes(32, "big")).public_key.format().hex()


def node(n, address=None):
    return {
        "pub_key": key(n),
        "addresses": [{"network": "tcp", "addr": address or f"8.8.8.{n}:9735"}],
    }


def peer(n):
    return {"pub_key": key(n), "bytes_recv": "100", "sync_type": "PASSIVE_SYNC"}


@pytest.mark.parametrize(
    "address",
    [
        "localhost:9735",
        "example.org:9735",
        "abc.onion:9735",
        "127.0.0.1:9735",
        "10.0.0.1:9735",
        "192.168.1.2:9735",
        "169.254.169.254:80",
        "[::1]:9735",
        "[fe80::1%eth0]:9735",
        "224.0.0.1:9735",
        "8.8.8.8:0",
        "8.8.8.8:65536",
        "8.8.8.8:--flag",
        "8.8.8.8",
        None,
    ],
)
def test_peer_candidates_cannot_redirect_connections_to_local_or_named_hosts(address):
    assert public_address(address) is None


def test_candidates_are_unique_public_identities_and_skip_our_own_key():
    graph = {"nodes": [node(1), node(2), node(2), node(3, "192.168.1.1:9735")]}
    graph["nodes"][1]["addresses"].append({"network": "tcp", "addr": "[2606:4700:4700::1111]:9735"})
    graph["nodes"].append(
        {"pub_key": "--bad", "addresses": [{"network": "tcp", "addr": "8.8.8.8:9735"}]}
    )
    assert candidates(graph, key(1)) == {key(2): "8.8.8.2:9735"}
    assert public_address("[2606:4700:4700::1111]:9735") == "[2606:4700:4700::1111]:9735"


class Observer:
    def __init__(self, tmp_path):
        self.now = 0
        self.peers = [peer(1)]
        self.graph = {"nodes": [node(n) for n in range(1, 10)]}
        self.calls = []
        self.fail = False
        self.info = {
            "identity_pubkey": key(99),
            "synced_to_chain": True,
            "synced_to_graph": False,
            "num_active_channels": 0,
            "num_inactive_channels": 0,
            "num_pending_channels": 0,
        }
        self.recovery = PeerRecovery(tmp_path, self.rpc, self.mutate, clock=lambda: self.now)

    def rpc(self, command, **_):
        return {
            "listpeers": {"peers": list(self.peers)},
            "describegraph": self.graph,
        }[command]

    def mutate(self, command, *args):
        self.calls.append((command, args))
        if self.fail:
            raise ValueError("simulated connection timeout")
        if command == "connect":
            assert args[:2] == ("--timeout", "8s")
            self.peers.append(
                {"pub_key": args[-1].split("@")[0], "bytes_recv": "0", "sync_type": "PASSIVE_SYNC"}
            )
        else:
            assert command == "disconnect"
            self.peers = [p for p in self.peers if p["pub_key"] != args[0]]

    def tick(self, now):
        self.now = now
        return self.recovery.tick(self.info)

    def log(self, *lines):
        with (self.recovery.session / "lnd.log").open("a") as stream:
            stream.write("".join(f"2026-11-10 00:00:00.000 [INF] {line}\n" for line in lines))


def test_replenishes_connections_without_claiming_graph_readiness(tmp_path):
    observer = Observer(tmp_path)
    assert observer.tick(0)["connected"] == 3
    assert observer.tick(1)["attempts"] == 2  # rate limit, not two more dials
    assert observer.tick(60)["connected"] == 5
    result = observer.tick(120)
    assert result["connected"] == 6
    assert result["attempts"] == 5
    assert not observer.info["synced_to_graph"]
    assert not (tmp_path / "manifest.json").exists()
    assert "connect" in (tmp_path / "peer-events.jsonl").read_text()


def test_all_failed_dials_retry_after_cooldown_instead_of_backing_off_forever(tmp_path):
    observer = Observer(tmp_path)
    observer.graph = {"nodes": [node(2), node(3)]}
    observer.fail = True
    assert observer.tick(0)["attempts"] == 2
    assert observer.tick(60)["attempts"] == 2
    assert observer.tick(599)["attempts"] == 2
    assert observer.tick(660)["attempts"] == 4
    assert observer.recovery.status["connections"] == 0


def test_connections_have_bounded_parallelism(tmp_path):
    observer = Observer(tmp_path)
    lock = Lock()
    active = peak = 0
    original = observer.mutate

    def bounded(command, *args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        sleep(0.03)
        result = original(command, *args)
        with lock:
            active -= 1
        return result

    observer.recovery.mutate = bounded
    observer.tick(0)
    assert peak == 2


def test_invalid_sync_replies_rotate_only_the_offending_peer_and_are_not_retried(tmp_path):
    observer = Observer(tmp_path)
    observer.peers = [peer(n) for n in range(1, 7)]
    line = (
        f"Process query msg from peer {key(1)} got unexpected msg "
        "*lnwire.ReplyChannelRange received in state chansSynced\n"
    )
    (tmp_path / "lnd.log").write_text(line * 3)
    status = observer.tick(0)
    assert status["rotations"] == 1 and status["connected"] == 5
    assert key(1) not in {p["pub_key"] for p in observer.peers}
    observer.tick(60)
    observer.tick(660)
    assert not any(command == "connect" and key(1) in args[-1] for command, args in observer.calls)
    assert observer.recovery.bad_replies[key(1)] == 3  # tail cursor prevents rereading


def test_partial_log_line_does_not_trigger_a_false_rotation(tmp_path):
    observer = Observer(tmp_path)
    line = (
        f"Process query msg from peer {key(1)} got unexpected msg "
        "*lnwire.ReplyChannelRange received in state chansSynced\n"
    )
    log = tmp_path / "lnd.log"
    log.write_text(line[:80])
    observer.recovery.read_log()
    assert not observer.recovery.bad_replies
    with log.open("a") as stream:
        stream.write(line[80:])
    observer.recovery.read_log()
    assert observer.recovery.bad_replies[key(1)] == 1


def test_unsynced_flat_graph_rotates_then_stops_with_diagnostics(tmp_path):
    observer = Observer(tmp_path)
    observer.peers = [peer(n) for n in range(1, 7)]
    observer.tick(0)
    assert observer.tick(1200)["rotations"] == 1
    assert observer.tick(2700)["stop_reason"] == "graph stalled despite peer recovery"


def test_real_graph_progress_resets_watchdog_and_synced_stability_never_triggers_it(tmp_path):
    observer = Observer(tmp_path)
    observer.tick(0)
    observer.log("GRPH: Processed channels=500 updates=900 nodes=300 in last 1m0s")
    result = observer.tick(2600)
    assert result["stalled_seconds"] == 0 and result["processed_channels"] == 500
    assert observer.tick(2700)["stop_reason"] is None
    observer.info["synced_to_graph"] = True
    result = observer.tick(10000)
    assert result["stalled_seconds"] == 0 and result["rotations"] == 0
    assert result["stop_reason"] is None


def test_unanswered_channel_query_disconnects_the_sync_peer_not_an_idle_one(tmp_path):
    """A peer can answer pings for an hour and leave a channel query unanswered."""
    observer = Observer(tmp_path)
    observer.peers = [peer(n) for n in range(1, 7)]
    observer.peers[2]["bytes_recv"] = "430000"  # The range replies it did send.
    observer.log(
        f"DISC: GossipSyncer({key(3)}): requesting new chans from height=0 and 900000 blocks after",
        f"DISC: GossipSyncer({key(3)}): filtering through 50000 chans",
        f"DISC: GossipSyncer({key(3)}): starting query for 30000 new chans",
        f"DISC: GossipSyncer({key(3)}): querying for 500 new channels",
    )
    status = observer.tick(0)
    assert status["stalled_seconds"] == 0 and status["historical_syncs"] == 1
    assert (status["sync_advertised"], status["sync_new"], status["sync_queried"]) == (
        50000,
        30000,
        500,
    )
    # Updates alone, pings and a recent-range query by another peer are no progress.
    observer.log(
        "GRPH: Processed channels=0 updates=1 nodes=0 in last 9m0s",
        f"DISC: GossipSyncer({key(4)}): requesting new chans from height=899856 and 144 blocks",
        f"DISC: GossipSyncer({key(4)}): querying for 12 new channels",
    )
    assert observer.tick(540)["rotations"] == 0
    # A new block briefly unsets synced_to_chain. That pauses the clock, never resets it.
    observer.info["synced_to_chain"] = False
    observer.tick(570)
    observer.info["synced_to_chain"] = True
    assert observer.tick(600)["rotations"] == 0
    status = observer.tick(660)
    assert status["rotations"] == 1 and status["historical_syncs"] == 1
    assert status["stalled_seconds"] == 630
    assert observer.calls[-1] == ("disconnect", (key(3),))
    assert "historical sync query unanswered" in (tmp_path / "peer-events.jsonl").read_text()
    assert observer.recovery.retry_at[key(3)] == 660 + 3600
    # LND replaces the sync peer at once; its first query restarts the clock.
    observer.log(
        f"DISC: GossipSyncer({key(5)}): requesting new chans from height=0 and 900000 blocks after",
        f"DISC: GossipSyncer({key(5)}): remote peer has no new chans",
        f"DISC: GossipSyncer({key(6)}): requesting new chans from height=0 and 900000 blocks after",
        f"DISC: GossipSyncer({key(6)}): filtering through 26000 chans",
        f"DISC: GossipSyncer({key(6)}): starting query for 5000 new chans",
        f"DISC: GossipSyncer({key(6)}): querying for 500 new channels",
        f"DISC: GossipSyncer({key(6)}): querying for 500 new channels",
    )
    status = observer.tick(960)
    assert status["stalled_seconds"] == 0 and status["rotations"] == 1
    assert status["historical_syncs"] == 3 and status["sync_queried"] == 1000
    syncs = observer.recovery.syncs
    assert [sync["new"] for sync in syncs] == [30000, 0, 5000]
    # The result keeps counts and a time for every sync, never the peer.
    assert all(set(sync) == {"seen_at", "advertised", "new", "queried"} for sync in syncs)
    assert [(sync["advertised"], sync["queried"]) for sync in syncs] == [
        (50000, 500),
        (None, 0),
        (26000, 1000),
    ]


def test_wallet_with_channels_is_never_automatically_reconnected(tmp_path):
    observer = Observer(tmp_path)
    observer.info["num_active_channels"] = 1
    assert "empty observer" in observer.tick(0)["last_error"]
    assert not observer.calls


def test_no_peer_operations_before_chain_sync(tmp_path):
    observer = Observer(tmp_path)
    observer.info["synced_to_chain"] = False
    observer.tick(9000)
    assert not observer.calls
    observer.info["synced_to_chain"] = True
    assert observer.tick(9060)["stalled_seconds"] == 60  # Only time with a synced chain counts.


def test_peer_rpc_is_scoped_to_observer_and_cannot_run_other_admin_commands(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gossip_snapshot import collector

    calls = []
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda args, **kw: calls.append((args, kw)) or SimpleNamespace(returncode=0, stdout="{}"),
    )
    collector.peer_rpc(tmp_path, "connect", "--timeout", "8s", f"{key(1)}@8.8.8.8:9735")
    command, options = calls[0]
    assert command[0] == str(tmp_path / "bin/lncli")
    assert "--rpcserver=127.0.0.1:11009" in command
    assert f"--macaroonpath={tmp_path}/lnd/data/chain/bitcoin/mainnet/admin.macaroon" in command
    assert options["timeout"] == 12
    with pytest.raises(ValueError, match="only connect or disconnect"):
        collector.peer_rpc(tmp_path, "sendcoins")
    assert len(calls) == 1


def test_failed_dials_back_off_exponentially_without_changing_readiness(tmp_path):
    observer = Observer(tmp_path)
    observer.graph = {"nodes": [node(1), node(2), node(3)]}
    observer.fail = True
    observer.info["synced_to_graph"] = True
    for now, expected_delay in ((0, 600), (600, 1200), (1800, 2400), (4200, 3600)):
        before = len(observer.calls)
        observer.tick(now)
        assert len(observer.calls) == before + 2
        assert observer.recovery.retry_at[key(2)] == now + expected_delay
        assert observer.recovery.retry_at[key(3)] == now + expected_delay
        observer.tick(now + 60)
        assert len(observer.calls) == before + 2
    assert observer.recovery.status["connections"] == 0


def test_rejected_announcements_and_ban_disconnects_are_counted(tmp_path):
    observer = Observer(tmp_path)
    score = f"[WRN] DISC: Increasing ban score for peer={key(1)}@8.8.8.1:9735 due to "
    (tmp_path / "lnd.log").write_text(
        score
        + "outdated channel announcement for channel 800000:1:0\n"
        + score
        + "outdated channel announcement for channel 850000:2:1\n"
        + score
        + "invalid channel announcement for channel 899000:3:0\n"
        + f"[INF] PEER: Peer({key(1)}): disconnecting {key(1)}@8.8.8.1:9735, "
        "reason: peer has bypassed ban threshold - banning\n"
    )
    status = observer.tick(0)
    assert status["rejected_outdated"] == 2 and status["rejected_invalid"] == 1
    assert status["ban_disconnects"] == 1
    assert observer.tick(60)["ban_disconnects"] == 1  # no tail double-counting


def test_pong_failures_are_diagnosed_without_blacklisting_a_potentially_slow_peer(tmp_path):
    observer = Observer(tmp_path)
    line = f"[WRN] PEER: Peer({key(1)}): pong response failure for peer: timeout"
    log = tmp_path / "lnd.log"
    log.write_text(line + " -- disconnecting\n" + line + " -- not disconnecting due to config\n")
    status = observer.tick(0)
    assert status["pong_failures"] == 2
    assert status["pong_disconnects"] == 1
    assert status["peers_with_pong_failures"] == 1
    assert status["rotations"] == 0
    assert not observer.recovery.bad_replies
    assert observer.tick(60)["pong_failures"] == 2  # no tail double-counting


def test_debug_log_backlog_is_bounded_visible_and_eventually_consumed(tmp_path, monkeypatch):
    import gossip_snapshot.peers as peers_module

    observer = Observer(tmp_path)
    monkeypatch.setattr(peers_module, "LOG_READ_BUDGET", 1024)
    message = f"[WRN] PEER: Peer({key(1)}): pong response failure for peer: timeout\n"
    path = tmp_path / "lnd.log"
    path.write_text("[DBG] DISC: progress\n" * 200 + message)
    observer.recovery.read_log()
    assert observer.recovery.status["log_backlog_bytes"] == path.stat().st_size - 1024
    assert not observer.recovery.pong_failures
    while observer.recovery.status["log_backlog_bytes"]:
        observer.recovery.read_log()
    assert observer.recovery.pong_failures[key(1)] == 1
    observer.recovery.read_log()
    assert observer.recovery.pong_failures[key(1)] == 1
