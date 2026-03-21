"""
Group chat dataclasses for the AQM B-Tree architecture (PDF §8.1).

Defines the wire-format structures for group parcels, hot edge state,
and group membership. Kept separate from aqm_shared/types.py to avoid
modifying the existing shared module.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GroupParcelHeader:
    """
    Unencrypted routing envelope. Implements Blind Star Graph routing (PDF §8.1).
    Server reads this and nothing else. Decision D7.
    """
    version: int                  # = 2 (distinguishes from 1:1 parcel)
    group_id: str                 # UUID string
    sender_id: str                # sender's user id
    recipient_ids: list[str]      # all members except sender (D2)
    timestamp: float
    group_name: str = ""          # human-readable name for auto-create on receiver


@dataclass
class GroupParcelInner:
    """
    Hierarchical encrypted payload encoding the B-Tree (PDF §8.1).
    Server has zero access to any field here.
    """
    # Level 1→0: root_key encrypted under each branch key that has members
    root_key_enc: dict[str, bytes]    # {"BESTIE": bytes, "MATE": bytes, "STRANGER": bytes}

    # Level 2→1: branch_key encrypted per leaf member (D8)
    # COLD leaf: KEM ciphertext + AES-GCM(branch_key, kem_shared_secret)
    # HOT leaf: AES-GCM(branch_key, ratchet_derived_key) (D4)
    leaf_enc: dict[str, bytes]        # {member_id: encrypted_branch_key_share}

    # Which leaf entries used the HOT ratchet path (no coin consumed)
    hot_leaf_ids: list[str]

    # Member → tier mapping so receivers know which branch they're on (D1)
    member_tiers: dict[str, str] = field(default_factory=dict)

    # Member → coin_key_id for COLD leaves (receiver needs this to find vault key)
    leaf_coin_ids: dict[str, str] = field(default_factory=dict)

    # AES-GCM(plaintext, root_key)
    encrypted_payload: bytes = b""

    # Signs concat(serialized_header, serialized_inner_minus_sig)
    sender_signature: bytes = b""


@dataclass
class HotEdgeState:
    """
    Tracks the temporal state of a single (group_id, member_id) edge (D3).
    HOT edges bypass the B-Tree asymmetric leaf and use a symmetric ratchet (D4).
    """
    group_id: str
    member_id: str
    state: str = "COLD"                     # "HOT" or "COLD"
    last_activity_at: Optional[float] = None
    ephemeral_chain_key: Optional[bytes] = None  # encrypted at rest (D4)
    chain_key_iv: Optional[bytes] = None
    chain_key_tag: Optional[bytes] = None
    msg_counter: int = 0


@dataclass
class GroupMemberInfo:
    """
    A member within a group, with sender-local tier assignment (D1).
    priority is re-evaluated from contacts_db at send time (G1).
    """
    group_id: str
    member_id: str
    display_name: str
    priority: str = "STRANGER"  # display-only; re-evaluated at send time


@dataclass
class GroupInfo:
    """Metadata for a group this device belongs to."""
    group_id: str
    name: str
    my_role: str = "MEMBER"     # ADMIN or MEMBER
    created_at: Optional[str] = None


# Tier → branch mapping for the B-Tree (used by key_tree.py)
TIER_TO_BRANCH = {
    "BESTIE": "BESTIE",
    "MATE": "MATE",
    "STRANGER": "STRANGER",
}

# Branch → required coin tier (heterogeneous cipher suites, PDF §8.1)
BRANCH_COIN_TIER = {
    "BESTIE": "GOLD",
    "MATE": "SILVER",
    "STRANGER": "BRONZE",
}

# Hot edge TTL in seconds (PDF §8.1: 10 minutes)
HOT_EDGE_TTL_SECONDS = 600
