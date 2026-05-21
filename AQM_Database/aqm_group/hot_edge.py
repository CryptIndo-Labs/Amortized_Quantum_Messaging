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
from typing import Optional, Tuple

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

    def remember_peer_kem_secret(self, group_id: str, peer_id: str, shared_secret: bytes) -> None:
        """
        Store the KEM shared secret for this peer (AEAD with vault_key).
        Needed to derive HOT receive keys; updated on each COLD exchange.
        """
        aad = f"group-kem:{group_id}:{peer_id}".encode()
        blob = self.crypto.encrypt_aead(shared_secret, self.vault_key, aad)
        self.group_db.store_peer_kem_ciphertext(group_id, peer_id, blob)

    def get_peer_kem_secret(self, group_id: str, peer_id: str) -> Optional[bytes]:
        blob = self.group_db.get_peer_kem_ciphertext(group_id, peer_id)
        if not blob:
            return None
        aad = f"group-kem:{group_id}:{peer_id}".encode()
        try:
            return self.crypto.decrypt_aead(blob, self.vault_key, aad)
        except Exception:
            return None

    def derive_key_with_counter(
        self, group_id: str, peer_id: str
    ) -> Optional[Tuple[bytes, int]]:
        """
        Derive next HOT send key and return (key, msg_counter_before_derive).

        peer_id is the remote member this leaf is addressed to (same as activate()).
        """
        edge = self.group_db.get_hot_edge(group_id, peer_id)
        if edge is None or edge.state != "HOT":
            return None

        if self._is_expired(edge):
            self.deactivate(group_id, peer_id)
            return None

        counter_before = edge.msg_counter
        chain_key = self._decrypt_chain_key(edge)

        ratchet = SessionRatchet(
            contact_id=f"group:{group_id}:{peer_id}",
            coin_tier="BRONZE",
        )
        ratchet.send_chain_key = chain_key
        ratchet.send_counter = edge.msg_counter
        ratchet.has_sent_first = True
        ratchet.max_messages = 999999

        msg_key = ratchet.derive_send_key()

        new_chain_key = ratchet.send_chain_key
        aad = f"hot-edge:{group_id}:{peer_id}".encode()
        blob = self.crypto.encrypt_aead(new_chain_key, self.vault_key, aad)

        edge.ephemeral_chain_key = blob[12:-16]
        edge.chain_key_iv = blob[:12]
        edge.chain_key_tag = blob[-16:]
        edge.msg_counter = ratchet.send_counter
        edge.last_activity_at = time.time()
        self.group_db.set_hot_edge(edge)

        return (msg_key, counter_before)

    def derive_key(self, group_id: str, member_id: str) -> Optional[bytes]:
        """
        Derive the next AES-256 key from a HOT edge's ratchet.

        Why: D4, D8 — HOT edges bypass the B-Tree asymmetric leaf.
        The returned key is used to encrypt the branch_key share for
        this member, saving one coin per message.

        Returns None if edge is COLD or expired.
        """
        t = self.derive_key_with_counter(group_id, member_id)
        return t[0] if t else None

    def derive_recv_key(
        self,
        group_id: str,
        leaf_recipient_id: str,
        shared_secret: bytes,
        msg_counter: int,
    ) -> bytes:
        """
        Derive the AES key for a HOT leaf as the recipient.

        leaf_recipient_id must match the leaf member_id and the sender's
        activate(..., peer=leaf_recipient_id) so SessionRatchet contact_id matches.

        msg_counter is the sender's stored counter *before* derive_send_key for this leaf.
        """
        ratchet = SessionRatchet(
            contact_id=f"group:{group_id}:{leaf_recipient_id}",
            coin_tier="BRONZE",
            master_secret=shared_secret,
            is_initiator=True,
        )
        ratchet.max_messages = 999999

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

        Returns (aes_key, counter_before) for HOT, or None if COLD.
        """
        def _get(member_id):
            return self.derive_key_with_counter(group_id, member_id)
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
