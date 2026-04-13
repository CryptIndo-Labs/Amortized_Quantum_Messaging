"""
GroupOrchestrator — orchestrates group chat send/receive lifecycle (PDF §8.1).

Wires together GroupKeyTree, HotEdgeTracker, GroupDatabase, and the network
layer to implement the full Amortized Group Chat Architecture.

Decisions implemented: D1 (sender-local tier), D2 (sender not a leaf),
D4 (HOT wraps SessionRatchet), D5 (creator-only groups), D7 (fan-out once),
D8 (one coin per COLD leaf), D9 (on-demand STRANGER fetch), D10 (client-only history).
"""

import logging
import os
import uuid
from typing import Optional, Callable, Awaitable

from AQM_Database.aqm_shared.crypto_engine import CryptoEngine
from AQM_Database.aqm_contacts.contacts_db import ContactsDatabase
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_group.group_db import GroupDatabase
from AQM_Database.aqm_group.group_types import (
    GroupInfo, GroupMemberInfo, BRANCH_COIN_TIER, TIER_TO_BRANCH,
)
from AQM_Database.aqm_group.key_tree import GroupKeyTree
from AQM_Database.aqm_group.hot_edge import HotEdgeTracker
from AQM_Database.aqm_group.group_parcel import (
    build_parcel, parse_parcel, make_header, make_inner,
)

logger = logging.getLogger("aqm.group.orchestrator")


class InsufficientCoinsError(Exception):
    """
    Raised when a member has no coins and on-demand fetch fails (G2).
    No silent tier downgrade — the send fails explicitly.
    """
    def __init__(self, member_id: str, coin_tier: str):
        self.member_id = member_id
        self.coin_tier = coin_tier
        super().__init__(
            f"No {coin_tier} coins available for member {member_id} "
            f"and on-demand fetch failed"
        )


class GroupOrchestrator:
    """
    Main group chat orchestrator.

    Why: coordinates the B-Tree key hierarchy, hot edge state machine,
    coin inventory, and network layer into a single send/receive flow.
    """

    def __init__(
        self,
        user_id: str,
        group_db: GroupDatabase,
        contacts_db: ContactsDatabase,
        inventory: SmartInventory,
        vault: SecureVault,
        crypto: CryptoEngine,
        hot_edge_tracker: HotEdgeTracker,
        send_parcel_fn: Callable[[str], None] = None,
        fetch_coins_fn: Callable[[str, str], Awaitable[None]] = None,
    ):
        """
        Args:
            user_id: this user's ID
            group_db: local SQLite group state
            contacts_db: local SQLite contacts (for tier lookup, D1/G1)
            inventory: SmartInventory for coin selection (D8)
            vault: SecureVault for private key lookup on receive
            crypto: CryptoEngine for all crypto ops
            hot_edge_tracker: manages HOT/COLD edge state (D3/D4)
            send_parcel_fn: callable(raw_json) to send to relay (G5)
            fetch_coins_fn: async callable(member_id, coin_tier) for on-demand fetch (D9)
        """
        self.user_id = user_id
        self.group_db = group_db
        self.contacts_db = contacts_db
        self.inventory = inventory
        self.vault = vault
        self.crypto = crypto
        self.hot_edge = hot_edge_tracker
        self.key_tree = GroupKeyTree(crypto)
        self._send_parcel = send_parcel_fn or (lambda raw: None)
        self._fetch_coins = fetch_coins_fn

    # ── Group Management ───────────────────────────────────────────────

    def create_group(self, name: str, member_ids: list[str]) -> GroupInfo:
        """
        Create a new group with the given members.

        Why: D5 — creator is ADMIN, Phase I creator-only. Members are
        added at creation time. No invite/join protocol.
        """
        group_id = str(uuid.uuid4())
        group = self.group_db.create_group(group_id, name, role="ADMIN")

        for mid in member_ids:
            # Look up display name and priority from contacts_db (D1)
            contact = self.contacts_db.get_contact(mid)
            display_name = contact.display_name if contact else mid
            priority = contact.priority if contact else "STRANGER"
            self.group_db.add_member(group_id, mid, display_name, priority)

        logger.info("Created group %s (%s) with %d members", group_id, name, len(member_ids))
        return group

    def add_member(self, group_id: str, member_id: str) -> GroupMemberInfo:
        """
        Add a member to an existing group.

        Why: D6 — Phase I supports create + add-member only, no removal.
        Adding a STRANGER member only recomputes the STRANGER branch.
        """
        contact = self.contacts_db.get_contact(member_id)
        display_name = contact.display_name if contact else member_id
        priority = contact.priority if contact else "STRANGER"

        member = self.group_db.add_member(group_id, member_id, display_name, priority)
        logger.info("Added member %s to group %s (tier=%s)", member_id, group_id, priority)
        return member

    # ── Send ───────────────────────────────────────────────────────────

    def send_group_message(self, group_id: str, plaintext: str) -> str:
        """
        Send a message to all members of a group.

        Why: implements the full B-Tree send flow. Sender constructs the
        complete hierarchical parcel and uploads it exactly once (G5).

        Returns the serialized parcel (for testing/inspection).
        """
        group = self.group_db.get_group(group_id)
        if group is None:
            raise ValueError(f"Group {group_id} not found")

        members = self.group_db.get_members(group_id)
        if not members:
            raise ValueError(f"Group {group_id} has no members")

        # D1/G1: re-evaluate priority from contacts_db at send time
        members_by_tier = {"BESTIE": [], "MATE": [], "STRANGER": []}
        member_tiers = {}  # member_id → tier for parcel metadata
        for m in members:
            contact = self.contacts_db.get_contact(m.member_id)
            tier = contact.priority if contact else "STRANGER"
            members_by_tier[tier].append(m.member_id)
            member_tiers[m.member_id] = tier

        # D9/G2: ensure STRANGER members have BRONZE coins
        for mid in members_by_tier["STRANGER"]:
            self._ensure_coins(mid, "BRONZE")

        # Expire stale HOT edges before building the tree
        self.hot_edge.expire_stale(group_id)

        def get_coin(member_id, coin_tier):
            """
            D8: one coin consumed per COLD leaf member.
            Returns (public_key, key_id) from inventory.
            """
            coin = self.inventory.select_coin(member_id, coin_tier)
            if coin is None:
                raise InsufficientCoinsError(member_id, coin_tier)
            return coin.public_key, coin.key_id

        # Build the B-Tree
        aad_prefix = f"group:{group_id}:{self.user_id}".encode()
        result = self.key_tree.build(
            plaintext=plaintext.encode(),
            members_by_tier=members_by_tier,
            get_coin_for_member=get_coin,
            get_hot_key_for_member=self.hot_edge.get_hot_key_for_member(group_id),
            aad_prefix=aad_prefix,
        )

        # COLD→HOT (D4): persist KEM secrets and activate outbound HOT toward each peer.
        for peer_id, secret in result.cold_kem_secrets.items():
            self.hot_edge.remember_peer_kem_secret(group_id, peer_id, secret)
            self.hot_edge.activate(group_id, peer_id, secret)

        # D2: sender is not in recipient list
        recipient_ids = [m.member_id for m in members]

        # Build and serialize the parcel
        header = make_header(group_id, self.user_id, recipient_ids, group_name=group.name)
        inner = make_inner(
            root_key_enc=result.root_key_enc,
            leaf_enc=result.leaf_enc,
            hot_leaf_ids=result.hot_leaf_ids,
            encrypted_payload=result.encrypted_payload,
            member_tiers=member_tiers,
            leaf_coin_ids=result.leaf_coin_ids,
            hot_leaf_counters=result.hot_leaf_counters,
        )
        raw = build_parcel(header, inner)

        # G5: send exactly once — relay fans out
        self._send_parcel(raw)

        # D10: record message locally
        self.group_db.record_message(
            group_id=group_id,
            sender_id=self.user_id,
            plaintext=plaintext,
            tier_used="MIXED",
            hot_path=len(result.hot_leaf_ids) > 0,
        )

        logger.info(
            "Sent group message: group=%s members=%d cold=%d hot=%d",
            group_id, len(members),
            len(members) - len(result.hot_leaf_ids),
            len(result.hot_leaf_ids),
        )

        return raw

    def _ensure_coins(self, member_id: str, coin_tier: str) -> None:
        """
        D9/G2: ensure a member has coins of the required tier.
        For STRANGER members, fetch BRONZE on-demand if inventory is empty.
        Raises InsufficientCoinsError if fetch fails.
        """
        try:
            summary = self.inventory.get_inventory(member_id)
            count = getattr(summary, f"{coin_tier.lower()}_count", 0)
            if count > 0:
                return
        except Exception:
            pass

        # On-demand fetch (D9)
        if self._fetch_coins is not None:
            try:
                import asyncio
                import inspect
                result = self._fetch_coins(member_id, coin_tier)
                # Support both sync and async callbacks
                if inspect.isawaitable(result):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop and loop.is_running():
                        pass  # Can't block in async context
                    else:
                        asyncio.run(result)
                # else: sync callback already executed
                return
            except Exception as e:
                logger.warning("On-demand fetch failed for %s/%s: %s",
                             member_id, coin_tier, e)

        # If we get here without coins, raise (G2 — no silent downgrade)
        try:
            summary = self.inventory.get_inventory(member_id)
            count = getattr(summary, f"{coin_tier.lower()}_count", 0)
            if count > 0:
                return
        except Exception:
            pass

        raise InsufficientCoinsError(member_id, coin_tier)

    # ── Receive ────────────────────────────────────────────────────────

    def receive_group_message(self, raw_parcel: str) -> Optional[str]:
        """
        Receive and decrypt a group parcel.

        Why: recipient finds their leaf in the B-Tree, decrypts branch key →
        root key → payload. Records message locally (D10).

        Returns plaintext string or None on failure.
        """
        try:
            header, inner = parse_parcel(raw_parcel)

            if self.user_id not in inner.leaf_enc:
                logger.warning("No leaf for %s in group parcel %s",
                              self.user_id, header.group_id)
                return None

            # Determine which tier branch I'm on
            my_tier = self._find_my_tier(inner)

            # Build decryption callbacks
            aad_prefix = f"group:{header.group_id}:{header.sender_id}".encode()

            # Get coin_id hint for this recipient (if COLD leaf)
            my_coin_id = inner.leaf_coin_ids.get(self.user_id)

            cold_secret_holder: list[Optional[bytes]] = [None]

            def get_secret_key(kem_ct, coin_tier):
                """COLD path: KEM decapsulate using vault private key."""
                s = self._kem_decapsulate_from_vault(kem_ct, coin_tier, my_coin_id)
                cold_secret_holder[0] = s
                return s

            def get_hot_key_decrypt(member_id: str):
                """HOT path: same AES key sender derived (needs stored KEM secret + counter)."""
                if member_id != self.user_id:
                    raise ValueError("HOT leaf member_id mismatch")
                sec = self.hot_edge.get_peer_kem_secret(header.group_id, header.sender_id)
                if sec is None:
                    return None
                ctr = inner.hot_leaf_counters.get(self.user_id, 0)
                return self.hot_edge.derive_recv_key(
                    header.group_id, self.user_id, sec, ctr
                )

            plaintext_bytes = self.key_tree.decrypt_as_recipient(
                my_id=self.user_id,
                my_tier=my_tier,
                leaf_enc=inner.leaf_enc,
                root_key_enc=inner.root_key_enc,
                encrypted_payload=inner.encrypted_payload,
                hot_leaf_ids=inner.hot_leaf_ids,
                get_secret_key=get_secret_key,
                get_hot_key=get_hot_key_decrypt if self.user_id in inner.hot_leaf_ids else None,
                aad_prefix=aad_prefix,
            )

            plaintext = plaintext_bytes.decode()

            # Receiver: COLD→HOT toward sender (peer = sender) for future sends / HOT decrypt symmetry
            if self.user_id not in inner.hot_leaf_ids and cold_secret_holder[0]:
                s = cold_secret_holder[0]
                self.hot_edge.remember_peer_kem_secret(header.group_id, header.sender_id, s)
                self.hot_edge.activate(header.group_id, header.sender_id, s)

            # Auto-create group locally if this is the first message we receive
            # (receiver doesn't have the group in their DB yet — only creator does)
            existing = self.group_db.get_group(header.group_id)
            if existing is None:
                grp_name = header.group_name or header.group_id[:8]
                self.group_db.create_group(
                    group_id=header.group_id,
                    name=grp_name,
                    role="MEMBER",
                )
                # Add ALL members from the parcel header (sender + recipients)
                all_member_ids = [header.sender_id] + header.recipient_ids
                for mid in all_member_ids:
                    tier = inner.member_tiers.get(mid, "STRANGER")
                    contact = self.contacts_db.get_contact(mid)
                    display = contact.display_name if contact else mid
                    self.group_db.add_member(
                        header.group_id, mid, display, priority=tier,
                    )

            # D10: record locally
            self.group_db.record_message(
                group_id=header.group_id,
                sender_id=header.sender_id,
                plaintext=plaintext,
                tier_used=my_tier,
                hot_path=self.user_id in inner.hot_leaf_ids,
            )

            # Touch HOT edge for sender (they're active in this group)
            self.hot_edge.touch(header.group_id, header.sender_id)

            logger.info("Received group message: group=%s sender=%s tier=%s hot=%s",
                       header.group_id, header.sender_id, my_tier,
                       self.user_id in inner.hot_leaf_ids)

            return plaintext

        except Exception as e:
            logger.error("Group receive failed: %s", e, exc_info=True)
            return None

    def _find_my_tier(self, inner) -> str:
        """
        Determine which tier branch the sender placed me on.
        Uses the member_tiers map included in the parcel (D1).
        """
        if self.user_id in inner.member_tiers:
            return inner.member_tiers[self.user_id]

        # Fallback: try each existing branch
        for tier in ["BESTIE", "MATE", "STRANGER"]:
            if tier in inner.root_key_enc:
                return tier
        raise ValueError("No tier branch found in parcel")

    def _kem_decapsulate_from_vault(self, kem_ct: bytes, coin_tier: str,
                                     coin_id: str = None) -> bytes:
        """
        KEM decapsulate using the specified vault key.

        Why: the sender encrypted our branch key share with one of our
        public coins. The parcel includes a coin_id hint so we can look
        up the exact private key in our vault.

        Args:
            coin_id: the key_id of the coin used by the sender (from leaf_coin_ids)
        """
        if coin_id:
            entry = self.vault.fetch_key(coin_id)
            if entry is not None:
                shared_secret = self.crypto.kem_decapsulate(
                    kem_ct, entry.encrypted_blob, coin_tier
                )
                # Burn the used one-time key
                self.vault.burn_key(coin_id)
                return shared_secret

        # Fallback: try all active keys (for parcels without coin_id hint)
        active_ids = self.vault.get_all_active_ids(coin_tier)
        for key_id in active_ids:
            entry = self.vault.fetch_key(key_id)
            if entry is None:
                continue
            try:
                shared_secret = self.crypto.kem_decapsulate(
                    kem_ct, entry.encrypted_blob, coin_tier
                )
                self.vault.burn_key(key_id)
                return shared_secret
            except Exception:
                continue

        raise ValueError(f"No vault key could decapsulate (tier={coin_tier})")
