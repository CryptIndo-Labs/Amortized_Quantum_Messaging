"""
Group parcel builder and parser (PDF §8.1).

Constructs the hierarchical parcel envelope: outer routing header (plaintext,
for Blind Star Graph fan-out) + inner B-Tree encrypted payload.
The server reads only the header. The inner payload is end-to-end encrypted.

Serialization uses JSON for the outer header and base64-encoded binary blobs
for the inner cryptographic material.
"""

import base64
import json
import time
import logging
from typing import Optional

from AQM_Database.aqm_group.group_types import GroupParcelHeader, GroupParcelInner

logger = logging.getLogger("aqm.group.parcel")


def build_parcel(
    header: GroupParcelHeader,
    inner: GroupParcelInner,
) -> str:
    """
    Serialize a group parcel into a JSON wire format.

    Why: the sender constructs the complete hierarchical parcel and uploads
    it exactly once (Blind Star Graph, D7). The relay reads only the header
    for fan-out routing.

    The wire format is a single JSON object with two top-level keys:
    - "header": plaintext routing envelope (server reads this)
    - "inner": base64-encoded encrypted B-Tree payload (server cannot read)
    """
    wire = {
        "msg_type": "GROUP_PARCEL",
        "header": {
            "version": header.version,
            "group_id": header.group_id,
            "sender_id": header.sender_id,
            "recipient_ids": header.recipient_ids,
            "timestamp": header.timestamp,
            "group_name": header.group_name,
        },
        "inner": {
            # Level 1→0: root key encrypted under each branch key
            "root_key_enc": {
                tier: base64.b64encode(blob).decode()
                for tier, blob in inner.root_key_enc.items()
            },
            # Level 2→1: branch key encrypted per leaf member
            "leaf_enc": {
                mid: base64.b64encode(blob).decode()
                for mid, blob in inner.leaf_enc.items()
            },
            # HOT leaf member IDs (no coin consumed for these)
            "hot_leaf_ids": inner.hot_leaf_ids,
            # HOT: sender ratchet counter before derive for each hot leaf member
            "hot_leaf_counters": inner.hot_leaf_counters,
            # Member → tier mapping so receivers know their branch (D1)
            "member_tiers": inner.member_tiers,
            # Member → coin_key_id for COLD leaves (receiver vault lookup)
            "leaf_coin_ids": inner.leaf_coin_ids,
            # AES-GCM(plaintext, root_key)
            "encrypted_payload": base64.b64encode(inner.encrypted_payload).decode(),
            # Sender signature
            "sender_signature": base64.b64encode(inner.sender_signature).decode(),
        },
    }
    return json.dumps(wire)


def parse_parcel(raw: str) -> tuple[GroupParcelHeader, GroupParcelInner]:
    """
    Deserialize a group parcel from JSON wire format.

    Why: the receiver needs to extract their leaf, decrypt the branch key,
    then the root key, then the payload. This function reconstructs the
    typed header and inner structures from the wire format.
    """
    data = json.loads(raw)

    if data.get("msg_type") != "GROUP_PARCEL":
        raise ValueError(f"Expected GROUP_PARCEL, got {data.get('msg_type')}")

    h = data["header"]
    header = GroupParcelHeader(
        version=h["version"],
        group_id=h["group_id"],
        sender_id=h["sender_id"],
        recipient_ids=h["recipient_ids"],
        timestamp=h["timestamp"],
        group_name=h.get("group_name", ""),
    )

    i = data["inner"]
    raw_counters = i.get("hot_leaf_counters") or {}
    hot_leaf_counters = {k: int(v) for k, v in raw_counters.items()}
    inner = GroupParcelInner(
        root_key_enc={
            tier: base64.b64decode(blob)
            for tier, blob in i["root_key_enc"].items()
        },
        leaf_enc={
            mid: base64.b64decode(blob)
            for mid, blob in i["leaf_enc"].items()
        },
        hot_leaf_ids=i["hot_leaf_ids"],
        hot_leaf_counters=hot_leaf_counters,
        member_tiers=i.get("member_tiers", {}),
        leaf_coin_ids=i.get("leaf_coin_ids", {}),
        encrypted_payload=base64.b64decode(i["encrypted_payload"]),
        sender_signature=base64.b64decode(i["sender_signature"]),
    )

    return header, inner


def make_header(
    group_id: str,
    sender_id: str,
    recipient_ids: list[str],
    group_name: str = "",
) -> GroupParcelHeader:
    """
    Create a routing header for a group parcel.

    Why: D2 — sender is never a leaf in their own parcel. recipient_ids
    contains all members except the sender.
    """
    return GroupParcelHeader(
        version=2,
        group_id=group_id,
        sender_id=sender_id,
        recipient_ids=recipient_ids,
        timestamp=time.time(),
        group_name=group_name,
    )


def make_inner(
    root_key_enc: dict[str, bytes],
    leaf_enc: dict[str, bytes],
    hot_leaf_ids: list[str],
    encrypted_payload: bytes,
    member_tiers: dict[str, str] = None,
    leaf_coin_ids: dict[str, str] = None,
    hot_leaf_counters: dict[str, int] = None,
    sender_signature: bytes = b"",
) -> GroupParcelInner:
    """Create the inner B-Tree payload structure."""
    return GroupParcelInner(
        root_key_enc=root_key_enc,
        leaf_enc=leaf_enc,
        hot_leaf_ids=hot_leaf_ids,
        hot_leaf_counters=hot_leaf_counters or {},
        member_tiers=member_tiers or {},
        leaf_coin_ids=leaf_coin_ids or {},
        encrypted_payload=encrypted_payload,
        sender_signature=sender_signature,
    )
