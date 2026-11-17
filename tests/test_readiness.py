from copy import deepcopy

from gossip_snapshot.readiness import (
    MAXIMUM_CHURN,
    MINIMUM_INTERVAL_SECONDS,
    MINIMUM_PEERS,
    check_readiness,
)


def samples():
    return [
        {
            "sampled_at": f"2026-11-10T0{i}:00:00Z",
            "getinfo": {
                "block_height": 900000,
                "block_hash": "11" * 32,
                "synced_to_chain": True,
                "synced_to_graph": True,
                "num_peers": 3,
            },
            "scids": [str(800000 << 40 | n << 16) for n in range(200)],
        }
        for i in range(3)
    ]


def test_ready_requires_nonempty_stable_spaced_synced_samples():
    assert check_readiness(samples())["ready"]
    assert not check_readiness(samples()[:2])["ready"]
    data = samples()
    data[1]["sampled_at"] = data[0]["sampled_at"]
    assert not check_readiness(data)["ready"]
    data = samples()
    data[-1]["getinfo"]["synced_to_graph"] = False
    assert not check_readiness(data)["ready"]


def test_empty_or_churning_graph_is_not_ready():
    for mutation in [[], [str(700000 << 40)]]:
        data = samples()
        data[-1]["scids"] = mutation
        assert not check_readiness(data)["ready"]


def test_same_height_different_hash_detects_reorg():
    data = deepcopy(samples())
    data[-1]["getinfo"]["block_hash"] = "22" * 32
    assert not check_readiness(data)["ready"]


def test_each_threshold_holds_at_its_boundary():
    # Samples are an hour apart, hold 200 channels and report three peers.
    assert MINIMUM_INTERVAL_SECONDS == 1800 and MINIMUM_PEERS == 3 and MAXIMUM_CHURN == 0.01
    # Exactly half an hour apart is enough, one second less is not.
    data = samples()
    data[1]["sampled_at"], data[2]["sampled_at"] = "2026-11-10T00:30:00Z", "2026-11-10T01:00:00Z"
    assert check_readiness(data)["ready"]
    data[2]["sampled_at"] = "2026-11-10T00:59:59Z"
    assert check_readiness(data)["reasons"] == ["samples less than 30 minutes apart"]
    # Three peers are enough, two are not.
    data = samples()
    data[0]["getinfo"]["num_peers"] = 2
    assert check_readiness(data)["reasons"] == ["fewer than three connected peers"]
    # One new channel beside 199 kept and one lost: 2 of 201 changed, just below 1%.
    data = samples()
    data[2]["scids"] = [*data[2]["scids"][1:], str(800000 << 40 | 200 << 16)]
    decision = check_readiness(data)
    assert decision["ready"] and decision["churn"] == [0.0, 2 / 201]
    # Two channels gone of 200 is exactly 1%, which is not less than 1%.
    data = samples()
    data[2]["scids"] = data[2]["scids"][2:]
    decision = check_readiness(data)
    assert decision["churn"] == [0.0, 0.01]
    assert decision["reasons"] == ["SCID-set churn is at least 1%"]
