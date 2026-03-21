"""
HotEdgeTracker — Temporal State Machine for group chat (PDF §8.1).

Prevents "Cryptographic Thrashing" during rapid group conversations by
transitioning active (group_id, member_id) edges to HOT state. HOT edges
bypass the B-Tree asymmetric leaf KEM and use an ephemeral AES-256 session
ratchet (D4) — no coin consumed (D8).

Edge scope is per (group_id, member_id) pair (D3). Alice↔Bob being HOT
in G1 does NOT affect G2 or Alice↔Charlie.

Chain keys are stored encrypted at rest (D4) — never plaintext in SQLite.
"""

import logging
import time
from typing import Optional

from AQM_Database.aqm_shared.crypto_engine import CryptoEngine
from AQM_Database.aqm_session.ratchet import SessionRatchet
from AQM_Database.aqm_group.group_db import GroupDatabase
from AQM_Database.aqm_group.group_types import HotEdgeState, HOT_EDGE_TTL_SECONDS

logger = logging.getLogger("aqm.group.hot_edge")


class HotEdgeTracker:
    """
    Manages HOT/COLD state transitions for group chat edges.

    Why: during rapid group chat, re-doing the full B-Tree asymmetric leaf
    for every message is wasteful ("Cryptographic Thrashing"). HOT edges
    use a symmetric ratchet instead, saving one KEM+coin per message.
    Implements D3 (per-group scope), D4 (wraps SessionRatchet).
    """

    def __init__(self, group_db: GroupDatabase, crypto: CryptoEngine,
                 vault_key: bytes, ttl_seconds: int = HOT_EDGE_TTL_SECONDS):
        """
        Args:
            group_db: SQLite database for persisting edge state
            crypto: CryptoEngine for AEAD encrypt/decrypt of chain keys (D4)
            vault_key: 32-byte AES key for encrypting chain keys at rest
            ttl_seconds: HOT→COLD timeout (default 600s = 10 minutes)
        """
        self.group_db = group_db
        self.crypto = crypto
        self.vault_key = vault_key
        self.ttl_seconds = ttl_seconds

    def activate(self, group_id: str, member_id: str,
                 shared_secret: bytes) -> None:
        """
        Transition edge from COLD→HOT after a B-Tree leaf KEM exchange.

        Why: D4 — the ephemeral master secret from the most recent KEM
        exchange seeds a SessionRatchet. Subsequent messages to this member
        use derive_send_key() instead of consuming another coin.

        Args:
            group_id: group this edge belongs to (D3)
            member_id: the member on the other end
            shared_secret: KEM shared secret from the leaf exchange
        """
        # Create a SessionRatchet seeded from the shared secret
        ratchet = SessionRatchet(
            contact_id=f"group:{group_id}:{member_id}",
            coin_tier="BRONZE",  # tier doesn't matter — we only use the HKDF chain
            master_secret=shared_secret,
            is_initiator=True,
        )

        # Encrypt the chain key before storing (D4 — never plaintext)
        chain_key = ratchet.send_chain_key
        aad = f"hot-edge:{group_id}:{member_id}".encode()
        blob = self.crypto.encrypt_aead(chain_key, self.vault_key, aad)
        iv = blob[:12]
        tag = blob[-16:]
        enc_key = blob[12:-16]

        edge = HotEdgeState(
            group_id=group_id,
            member_id=member_id,
            state="HOT",
            last_activity_at=time.time(),
            ephemeral_chain_key=enc_key,
            chain_key_iv=iv,
            chain_key_tag=tag,
            msg_counter=0,
        )
        self.group_db.set_hot_edge(edge)
        logger.info("COLD→HOT: group=%s member=%s", group_id, member_id)

    def derive_key(self, group_id: str, member_id: str) -> Optional[bytes]:
        """
        Derive the next AES-256 key from a HOT edge's ratchet.

        Why: D4, D8 — HOT edges bypass the B-Tree asymmetric leaf.
        The returned key is used to encrypt the branch_key share for
        this member, saving one coin per message.

        Returns None if edge is COLD or expired.
        """
        edge = self.group_db.get_hot_edge(group_id, member_id)
        if edge is None or edge.state != "HOT":
            return None

        # Check TTL — expire if silent too long
        if self._is_expired(edge):
            self.deactivate(group_id, member_id)
            return None

        # Decrypt chain key from storage (D4)
        chain_key = self._decrypt_chain_key(edge)

        # Build a temporary ratchet to derive the next key
        ratchet = SessionRatchet(
            contact_id=f"group:{group_id}:{member_id}",
            coin_tier="BRONZE",
        )
        ratchet.send_chain_key = chain_key
        ratchet.send_counter = edge.msg_counter
        ratchet.has_sent_first = True
        # Override max_messages to avoid exhaustion — HOT edges reset on COLD
        ratchet.max_messages = 999999

        msg_key = ratchet.derive_send_key()

        # Re-encrypt advanced chain key and update state
        new_chain_key = ratchet.send_chain_key
        aad = f"hot-edge:{group_id}:{member_id}".encode()
        blob = self.crypto.encrypt_aead(new_chain_key, self.vault_key, aad)

        edge.ephemeral_chain_key = blob[12:-16]
        edge.chain_key_iv = blob[:12]
        edge.chain_key_tag = blob[-16:]
        edge.msg_counter = ratchet.send_counter
        edge.last_activity_at = time.time()
        self.group_db.set_hot_edge(edge)

        return msg_key

    def derive_recv_key(self, group_id: str, member_id: str,
                        shared_secret: bytes, msg_counter: int) -> bytes:
        """
        Derive the decryption key for a HOT leaf from a received parcel.

        Why: the receiver needs the same key the sender derived. Since
        we know the shared_secret (from the original COLD KEM exchange)
        and the counter, we can reconstruct the exact key.

        Args:
            shared_secret: original KEM shared secret that seeded the HOT edge
            msg_counter: the sender's counter when they derived this key
        """
        ratchet = SessionRatchet(
            contact_id=f"group:{group_id}:{member_id}",
            coin_tier="BRONZE",
            master_secret=shared_secret,
            is_initiator=True,
        )
        ratchet.max_messages = 999999

        # Advance the ratchet to match the sender's counter
        for _ in range(msg_counter + 1):
            key = ratchet.derive_send_key()

        return key

    def touch(self, group_id: str, member_id: str) -> None:
        """
        Refresh the TTL on a HOT edge (activity detected).

        Why: receiving a message from this member resets the 10-minute timer.
        """
        edge = self.group_db.get_hot_edge(group_id, member_id)
        if edge is not None and edge.state == "HOT":
            edge.last_activity_at = time.time()
            self.group_db.set_hot_edge(edge)

    def deactivate(self, group_id: str, member_id: str) -> None:
        """
        Transition edge from HOT→COLD. Burns the ephemeral key.

        Why: after 10 minutes of silence, the edge returns to COLD.
        The ephemeral chain key is deleted (D4 — key burned).
        """
        self.group_db.delete_hot_edge(group_id, member_id)
        logger.info("HOT→COLD: group=%s member=%s (key burned)", group_id, member_id)

    def is_hot(self, group_id: str, member_id: str) -> bool:
        """Check if an edge is currently HOT (and not expired)."""
        edge = self.group_db.get_hot_edge(group_id, member_id)
        if edge is None or edge.state != "HOT":
            return False
        if self._is_expired(edge):
            self.deactivate(group_id, member_id)
            return False
        return True

    def get_hot_key_for_member(self, group_id: str):
        """
        Returns a callable suitable for GroupKeyTree.build(get_hot_key_for_member=...).

        Why: provides the bridge between HotEdgeTracker and GroupKeyTree.
        Returns a function that takes member_id and returns the derived key
        if HOT, or None if COLD.
        """
        def _get(member_id):
            return self.derive_key(group_id, member_id)
        return _get

    def expire_stale(self, group_id: str) -> list[str]:
        """
        Check all HOT edges in a group and expire any that have timed out.
        Returns list of member_ids that transitioned to COLD.
        """
        expired = []
        for edge in self.group_db.get_all_hot_edges(group_id):
            if self._is_expired(edge):
                self.deactivate(edge.group_id, edge.member_id)
                expired.append(edge.member_id)
        return expired

    def _is_expired(self, edge: HotEdgeState) -> bool:
        """Check if a HOT edge has exceeded its TTL."""
        if edge.last_activity_at is None:
            return True
        return (time.time() - edge.last_activity_at) > self.ttl_seconds

    def _decrypt_chain_key(self, edge: HotEdgeState) -> bytes:
        """Decrypt the encrypted chain key from storage (D4)."""
        blob = edge.chain_key_iv + edge.ephemeral_chain_key + edge.chain_key_tag
        aad = f"hot-edge:{edge.group_id}:{edge.member_id}".encode()
        return self.crypto.decrypt_aead(blob, self.vault_key, aad)
