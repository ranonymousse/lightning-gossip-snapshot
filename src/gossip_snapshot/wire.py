"""Independently check BOLT7 v1 signed bytes exported by LND.

No blockchain validation is claimed here. See the capture's backend provenance.
"""

from hashlib import sha256

from coincurve import PublicKey
from coincurve.ecdsa import cdata_to_der, deserialize_compact, signature_normalize

from .schema import MAINNET, Record, decode_scid


def verify_record(record: Record) -> str | None:
    """Return an exclusion reason, or None for the strict P2WSH cohort.

    Broken signatures and contradictory metadata raise, rather than becoming
    silent exclusions. Nonempty feature vectors are conservatively excluded.
    """
    if record.chain_hash != MAINNET:
        raise ValueError("record is not on Bitcoin mainnet")
    if int(record.funding_outpoint.split(":")[1]) != decode_scid(record.scid)[2]:
        raise ValueError("SCID/output-index mismatch")
    if record.announcement_hex is None:
        if (
            record.signature_verified
            or record.exclusion_reason != "missing_proof"
            or record.funding_construction != "unknown"
        ):
            raise ValueError("missing announcement has contradictory validation labels")
        return "missing_proof"

    raw = bytes.fromhex(record.announcement_hex)
    # type + 4 compact ECDSA signatures + feature length + chain + scid + 4 keys
    if len(raw) < 432 or raw[:2] != b"\x01\x00":
        raise ValueError("not a complete channel_announcement (message type 256)")
    feature_length = int.from_bytes(raw[258:260], "big")
    offset = 260 + feature_length
    if len(raw) < offset + 172:
        raise ValueError("truncated channel announcement features/body")
    features = raw[260:offset]
    chain_hash = raw[offset : offset + 32][::-1].hex()
    scid = int.from_bytes(raw[offset + 32 : offset + 40], "big")
    keys = [raw[offset + 40 + 33 * i : offset + 73 + 33 * i] for i in range(4)]
    expected_keys = [
        record.node_key_1,
        record.node_key_2,
        record.bitcoin_key_1,
        record.bitcoin_key_2,
    ]
    if chain_hash != record.chain_hash or str(scid) != record.scid:
        raise ValueError("signed chain/SCID differs from exported metadata")
    if [key.hex() for key in keys] != expected_keys:
        raise ValueError("signed keys differ from exported metadata")
    if keys[0] >= keys[1]:
        raise ValueError("node keys are not in BOLT7 order")
    digest = sha256(sha256(raw[258:]).digest()).digest()
    for i, key in enumerate(keys):
        sig = deserialize_compact(raw[2 + 64 * i : 66 + 64 * i])
        # Bitcoin ECDSA verification permits high-S encodings; libsecp requires
        # normalization at verification. This changes no signed content.
        _, sig = signature_normalize(sig)
        if not PublicKey(key).verify(cdata_to_der(sig), digest, hasher=None):
            raise ValueError(f"invalid announcement signature {i + 1}")
    if not record.signature_verified:
        raise ValueError("exporter did not attest successful signature verification")
    reason = "unsupported_features" if any(features) else None
    construction = "unsupported" if reason else "p2wsh"
    if record.exclusion_reason != reason or record.funding_construction != construction:
        raise ValueError("signed features contradict exported cohort classification")
    return reason
