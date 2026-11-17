"""A declared sampling heuristic, never a claim of network completeness."""

from datetime import datetime

from .schema import BlockRef, decode_scid

# The rule, fixed before any mainnet data was collected: the last three observations,
# at least half an hour apart, each with a synchronized chain and graph and at least
# three peers, and less than 1% change in the channel set between consecutive ones.
OBSERVATIONS = 3
MINIMUM_INTERVAL_SECONDS = 1800
MINIMUM_PEERS = 3
MAXIMUM_CHURN = 0.01


def check_readiness(samples: list[dict]) -> dict:
    """Apply the readiness rule to the latest observations of a session.

    Returns whether the rule holds, the reasons why it does not, and the change of the
    channel set between consecutive observations.
    """
    if not isinstance(samples, list) or len(samples) < OBSERVATIONS:
        return {"ready": False, "reasons": ["need at least three samples"], "churn": []}
    reasons, sets, times, refs = [], [], [], []
    for sample in samples[-OBSERVATIONS:]:
        time = datetime.fromisoformat(sample["sampled_at"].replace("Z", "+00:00"))
        if time.utcoffset() is None or time.utcoffset().total_seconds() != 0:
            raise ValueError("sample timestamp must be UTC")
        times.append(time)
        info = sample["getinfo"]
        ref = BlockRef.model_validate({"height": info["block_height"], "hash": info["block_hash"]})
        refs.append(ref)
        if info["synced_to_chain"] is not True:
            reasons.append("Bitcoin chain is not synced")
        if info["synced_to_graph"] is not True:
            reasons.append("Lightning graph is not synced")
        if type(info["num_peers"]) is not int or info["num_peers"] < MINIMUM_PEERS:
            reasons.append("fewer than three connected peers")
        ids = sample["scids"]
        if not isinstance(ids, list) or len(set(ids)) != len(ids):
            raise ValueError("invalid or duplicate readiness SCIDs")
        for scid in ids:
            if decode_scid(scid)[0] > ref.height:
                raise ValueError("readiness graph has future-height SCID")
        if not ids:
            reasons.append("empty graph")
        sets.append(set(ids))
    churn = []
    for i in range(1, OBSERVATIONS):
        if (times[i] - times[i - 1]).total_seconds() < MINIMUM_INTERVAL_SECONDS:
            reasons.append("samples less than 30 minutes apart")
        if refs[i].height < refs[i - 1].height or (
            refs[i].height == refs[i - 1].height and refs[i].hash != refs[i - 1].hash
        ):
            reasons.append("inconsistent chain references")
        union = sets[i] | sets[i - 1]
        value = len(sets[i] ^ sets[i - 1]) / len(union) if union else 1.0
        churn.append(value)
        if value >= MAXIMUM_CHURN:
            reasons.append("SCID-set churn is at least 1%")
    return {"ready": not reasons, "reasons": sorted(set(reasons)), "churn": churn}
