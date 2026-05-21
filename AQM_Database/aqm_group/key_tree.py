"""
GroupKeyTree — Categorical B-Tree key hierarchy (PDF §8.1).

Implements the 3-level tree:
  Level 0 (ROOT):     AES-256 Group Key — encrypts message payload
  Level 1 (BRANCHES): AES-256 Branch Key per tier (BESTIE/MATE/STRANGER)
                      Root key encrypted under each branch key
  Level 2 (LEAVES):   Per-member encryption of branch key using their coin (COLD)
                      or ephemeral ratchet key (HOT, D4)

Key property: adding a STRANGER member only recomputes the STRANGER branch.
BESTIE and MATE branches are untouched — zero battery drain for premium contacts.

Heterogeneous cipher suites (PDF §8.1):
  BESTIE  → GOLD coins (ML-KEM-768 + ML-DSA-65)
  MATE    → SILVER coins (ML-KEM-768 + Ed25519)
  STRANGER → BRONZE coins (X25519 + Ed25519)
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Optional, Union, Tuple

from AQM_Database.aqm_shared.crypto_engine import CryptoEngine
from AQM_Database.aqm_group.group_types import BRANCH_COIN_TIER

logger = logging.getLogger("aqm.group.key_tree")


@dataclass
class LeafResult:
    """Result of encrypting a branch key for a single leaf member."""
    member_id: str
    encrypted_branch_key: bytes   # KEM ciphertext + AES-GCM(branch_key) for COLD
    kem_ciphertext: Optional[bytes] = None  # KEM ct (COLD only, None for HOT)
    coin_key_id: Optional[str] = None       # consumed coin key_id (COLD only)
    hot_path: bool = False                  # True if HOT ratchet was used


@dataclass
class BranchResult:
    """Result of building one tier branch of the B-Tree."""
    tier: str
    branch_key: bytes             # AES-256 branch key (plaintext, ephemeral)
    encrypted_root_key: bytes     # AES-GCM(root_key, branch_key)
    leaves: list[LeafResult]


@dataclass
class TreeBuildResult:
    """Complete B-Tree build output."""
    root_key: bytes               # AES-256 root key (plaintext, ephemeral)
    encrypted_payload: bytes      # AES-GCM(plaintext_message, root_key)
    branches: dict[str, BranchResult]  # tier → BranchResult
    # Flattened maps for parcel construction
    root_key_enc: dict[str, bytes]     # tier → encrypted root key
    leaf_enc: dict[str, bytes]         # member_id → encrypted branch key
    hot_leaf_ids: list[str]            # member_ids that used HOT path
    hot_leaf_counters: dict[str, int] = field(default_factory=dict)  # HOT: counter before derive
    cold_kem_secrets: dict[str, bytes] = field(default_factory=dict)  # COLD: shared_secret per member
    leaf_coin_ids: dict[str, str] = field(default_factory=dict)  # member_id → coin key_id (COLD only)


class GroupKeyTree:
    """
    Builds and parses the Categorical B-Tree for group message encryption.

    Why: implements the core PDF §8.1 B-Tree topology where each send
    constructs a fresh root key, encrypts it per-branch, and encrypts
    each branch key per-leaf member. Decisions D1, D2, D8.
    """

    def __init__(self, crypto: CryptoEngine):
        self.crypto = crypto

    def build(
        self,
        plaintext: bytes,
        members_by_tier: dict[str, list[str]],
        get_coin_for_member: callable,
        get_hot_key_for_member: callable = None,
        aad_prefix: bytes = b"",
    ) -> TreeBuildResult:
        """
        Build the complete B-Tree for a group message send.

        Why: each send constructs the full hierarchy (D8). COLD leaves
        consume one coin per member. HOT leaves use symmetric ratchet (D4).

        Args:
            plaintext: raw message bytes to encrypt
            members_by_tier: {"BESTIE": [id1], "MATE": [id2], "STRANGER": [id3]}
                Tier assignment is sender-local (D1), evaluated from contacts_db (G1).
            get_coin_for_member: callable(member_id, coin_tier) → (public_key, key_id)
                Called for COLD leaves only. Returns coin's public key + key_id.
                Raises if no coin available (G2).
            get_hot_key_for_member: callable(member_id) → Optional[Union[bytes, Tuple[bytes, int]]]
                Returns (aes_key, counter_before_derive) or bare bytes (counter defaults to 0),
                or None if COLD. When HOT, no coin is consumed (D4, D8).
            aad_prefix: additional authenticated data prefix for all AES-GCM ops
        """
        # Level 0: generate ephemeral root key
        root_key = os.urandom(32)

        # Encrypt the plaintext under the root key
        root_aad = aad_prefix + b":root"
        encrypted_payload = self.crypto.encrypt_aead(plaintext, root_key, root_aad)

        branches: dict[str, BranchResult] = {}
        root_key_enc: dict[str, bytes] = {}
        leaf_enc: dict[str, bytes] = {}
        hot_leaf_ids: list[str] = []
        hot_leaf_counters: dict[str, int] = {}
        cold_kem_secrets: dict[str, bytes] = {}
        leaf_coin_ids: dict[str, str] = {}

        for tier, member_ids in members_by_tier.items():
            if not member_ids:
                # Empty branch omitted — no branch key generated
                continue

            # Level 1: generate ephemeral branch key for this tier
            branch_key = os.urandom(32)

            # Encrypt root key under branch key
            branch_aad = aad_prefix + f":branch:{tier}".encode()
            enc_root = self.crypto.encrypt_aead(root_key, branch_key, branch_aad)
            root_key_enc[tier] = enc_root

            leaves: list[LeafResult] = []

            for member_id in member_ids:
                # Check if this member has a HOT edge (D4)
                hot_key: Optional[bytes] = None
                hot_ctr_before: int = 0
                if get_hot_key_for_member is not None:
                    hk = get_hot_key_for_member(member_id)
                    if hk is not None:
                        if isinstance(hk, tuple):
                            hot_key, hot_ctr_before = hk[0], hk[1]
                        else:
                            hot_key = hk

                if hot_key is not None:
                    # HOT path: encrypt branch key with symmetric ratchet key
                    # No coin consumed (D8) — this is the HOT optimization
                    leaf_aad = aad_prefix + f":leaf:{member_id}:hot".encode()
                    enc_branch = self.crypto.encrypt_aead(branch_key, hot_key, leaf_aad)
                    leaf = LeafResult(
                        member_id=member_id,
                        encrypted_branch_key=enc_branch,
                        hot_path=True,
                    )
                    hot_leaf_ids.append(member_id)
                    hot_leaf_counters[member_id] = hot_ctr_before
                    logger.debug("HOT leaf for %s — no coin consumed", member_id)
                else:
                    # COLD path: KEM encapsulate with member's coin (D8)
                    coin_tier = BRANCH_COIN_TIER[tier]
                    public_key, key_id = get_coin_for_member(member_id, coin_tier)

                    # KEM encapsulate to get shared secret
                    kem_ct, shared_secret = self.crypto.kem_encapsulate(
                        public_key, coin_tier
                    )

                    # Encrypt branch key under shared secret
                    leaf_aad = aad_prefix + f":leaf:{member_id}:cold".encode()
                    enc_branch = self.crypto.encrypt_aead(branch_key, shared_secret, leaf_aad)

                    # Concatenate: KEM ciphertext || encrypted branch key
                    # Reader splits at known KEM ct size boundary
                    combined = kem_ct + enc_branch
                    leaf = LeafResult(
                        member_id=member_id,
                        encrypted_branch_key=combined,
                        kem_ciphertext=kem_ct,
                        coin_key_id=key_id,
                    )
                    leaf_coin_ids[member_id] = key_id
                    cold_kem_secrets[member_id] = shared_secret
                    logger.debug("COLD leaf for %s — consumed coin %s (%s)",
                                member_id, key_id, coin_tier)

                leaves.append(leaf)
                leaf_enc[member_id] = leaf.encrypted_branch_key

            branches[tier] = BranchResult(
                tier=tier, branch_key=branch_key,
                encrypted_root_key=enc_root, leaves=leaves,
            )

        return TreeBuildResult(
            root_key=root_key,
            encrypted_payload=encrypted_payload,
            branches=branches,
            root_key_enc=root_key_enc,
            leaf_enc=leaf_enc,
            hot_leaf_ids=hot_leaf_ids,
            hot_leaf_counters=hot_leaf_counters,
            cold_kem_secrets=cold_kem_secrets,
            leaf_coin_ids=leaf_coin_ids,
        )

    def decrypt_as_recipient(
        self,
        my_id: str,
        my_tier: str,
        leaf_enc: dict[str, bytes],
        root_key_enc: dict[str, bytes],
        encrypted_payload: bytes,
        hot_leaf_ids: list[str],
        get_secret_key: callable,
        get_hot_key: callable = None,
        aad_prefix: bytes = b"",
    ) -> bytes:
        """
        Decrypt a group parcel as a recipient.

        Why: recipient finds their leaf, decrypts branch key → root key → payload.

        Args:
            my_id: this user's member_id
            my_tier: the tier branch this user is on (from parcel metadata)
            leaf_enc: {member_id: encrypted_branch_key_share}
            root_key_enc: {tier: encrypted_root_key}
            encrypted_payload: AES-GCM(plaintext, root_key)
            hot_leaf_ids: member_ids that used HOT path
            get_secret_key: callable(kem_ciphertext, coin_tier) → shared_secret
                For COLD path: KEM decapsulate using vault private key.
            get_hot_key: callable(member_id) → bytes
                For HOT path: returns the ratchet-derived key.
            aad_prefix: same prefix used during build
        """
        if my_id not in leaf_enc:
            raise ValueError(f"No leaf entry for {my_id} in parcel")

        if my_tier not in root_key_enc:
            raise ValueError(f"No branch {my_tier} in parcel")

        my_leaf_blob = leaf_enc[my_id]
        is_hot = my_id in hot_leaf_ids

        if is_hot:
            # HOT path: decrypt branch key with ratchet key
            if get_hot_key is None:
                raise ValueError(f"HOT leaf for {my_id} but no hot key provider")
            hot_key = get_hot_key(my_id)
            leaf_aad = aad_prefix + f":leaf:{my_id}:hot".encode()
            branch_key = self.crypto.decrypt_aead(my_leaf_blob, hot_key, leaf_aad)
        else:
            # COLD path: split KEM ciphertext from encrypted branch key
            # KEM ct sizes: GOLD/SILVER (ML-KEM-768) = 1088 bytes, BRONZE (X25519) = 32 bytes
            coin_tier = BRANCH_COIN_TIER[my_tier]
            if coin_tier == "BRONZE":
                kem_ct_size = 32   # X25519 ephemeral public key
            else:
                kem_ct_size = 1088  # ML-KEM-768 ciphertext

            kem_ct = my_leaf_blob[:kem_ct_size]
            enc_branch_key = my_leaf_blob[kem_ct_size:]

            # KEM decapsulate to recover shared secret
            shared_secret = get_secret_key(kem_ct, coin_tier)

            # Decrypt branch key
            leaf_aad = aad_prefix + f":leaf:{my_id}:cold".encode()
            branch_key = self.crypto.decrypt_aead(enc_branch_key, shared_secret, leaf_aad)

        # Decrypt root key from branch key
        branch_aad = aad_prefix + f":branch:{my_tier}".encode()
        root_key = self.crypto.decrypt_aead(root_key_enc[my_tier], branch_key, branch_aad)

        # Decrypt payload with root key
        root_aad = aad_prefix + b":root"
        plaintext = self.crypto.decrypt_aead(encrypted_payload, root_key, root_aad)

        return plaintext
