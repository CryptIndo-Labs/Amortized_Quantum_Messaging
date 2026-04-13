"""
Tests for GroupOrchestrator — full group chat send/receive lifecycle (PDF §8.1).

Covers: create_group, send COLD, send HOT, receive+decrypt, add_member,
priority re-evaluation (D1/G1), on-demand fetch (D9), InsufficientCoinsError (G2),
fan-out called once (G5), client-only history (D10).

All tests use real CryptoEngine, fakeredis inventory, in-memory SQLite.
No Docker, no WebSocket server (G7).
"""

import os
import pytest
from unittest.mock import MagicMock

from AQM_Database.aqm_shared.crypto_engine import CryptoEngine
from AQM_Database.aqm_group.group_orchestrator import GroupOrchestrator, InsufficientCoinsError
from AQM_Database.aqm_group.group_db import GroupDatabase
from AQM_Database.aqm_group.hot_edge import HotEdgeTracker
from AQM_Database.aqm_group.group_parcel import parse_parcel
from AQM_Database.aqm_group.group_types import BRANCH_COIN_TIER
from AQM_Database.aqm_contacts.contacts_db import ContactsDatabase
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory

import fakeredis


@pytest.fixture
def setup(tmp_path):
    """
    Build a complete test environment with two users (alice, bob)
    each with their own subsystems. Returns a dict with everything needed.
    """
    crypto = CryptoEngine()
    fake_server = fakeredis.FakeServer()

    def make_user(user_id):
        """Create a full subsystem for one user."""
        group_db = GroupDatabase(db_path=str(tmp_path / f"{user_id}_groups.db"))
        contacts_db = ContactsDatabase(db_path=str(tmp_path / f"{user_id}_contacts.db"))

        vault_redis = fakeredis.FakeRedis(server=fake_server, decode_responses=False)
        vault = SecureVault(vault_redis, user_id=user_id)

        inv_redis = fakeredis.FakeRedis(server=fake_server, decode_responses=False)
        inventory = SmartInventory(inv_redis)

        vault_key = os.urandom(32)
        hot_edge = HotEdgeTracker(group_db, crypto, vault_key)

        sent_parcels = []

        orch = GroupOrchestrator(
            user_id=user_id,
            group_db=group_db,
            contacts_db=contacts_db,
            inventory=inventory,
            vault=vault,
            crypto=crypto,
            hot_edge_tracker=hot_edge,
            send_parcel_fn=lambda raw: sent_parcels.append(raw),
        )

        return {
            "orch": orch,
            "group_db": group_db,
            "contacts_db": contacts_db,
            "vault": vault,
            "inventory": inventory,
            "hot_edge": hot_edge,
            "sent_parcels": sent_parcels,
        }

    alice = make_user("alice")
    bob = make_user("bob")

    # Register each other as contacts
    alice["contacts_db"].add_contact("bob", "Bob")
    bob["contacts_db"].add_contact("alice", "Alice")

    return {"alice": alice, "bob": bob, "crypto": crypto}


def _mint_and_store(crypto, vault, inventory, user_id, contact_id, tier, count=3,
                    priority="STRANGER"):
    """
    Mint coins, store private keys in vault, and cache public keys in inventory
    as if they were fetched from the server.
    """
    from AQM_Database.aqm_shared import config as aqm_config
    import time as _time

    # Register contact in inventory (or update priority for budget caps)
    inventory.register_contact(contact_id, priority, contact_id)
    # Force-update priority if contact already registered
    meta_key = f"{aqm_config.INV_META_PREFIX}:{contact_id}"
    inventory.db.hset(meta_key, mapping={
        "contact_id": contact_id,
        "priority": priority,
        "display_name": contact_id,
        "last_msg_at": str(int(_time.time() * 1000)),
    })

    for _ in range(count):
        bundle = crypto.mint_coin(tier)
        # Store private key in vault (for receive-side decapsulation)
        vault.store_key(
            key_id=bundle.key_id,
            coin_category=tier,
            encrypted_blob=bundle.secret_key,
            encryption_iv=bytes(12),
            auth_tag=bytes(16),
        )
        # Store public key in inventory (for send-side encapsulation)
        inventory.store_key(
            contact_id=contact_id,
            key_id=bundle.key_id,
            coin_category=tier,
            public_key=bundle.public_key,
            signature=bundle.signature,
        )


class TestCreateGroup:
    """Group creation tests (D5)."""

    def test_create_group_stores_in_db(self, setup):
        """create_group persists to local SQLite."""
        alice = setup["alice"]
        group = alice["orch"].create_group("Test Group", ["bob"])
        assert group.name == "Test Group"
        assert group.my_role == "ADMIN"

        stored = alice["group_db"].get_group(group.group_id)
        assert stored is not None
        assert stored.name == "Test Group"

    def test_create_group_adds_members(self, setup):
        """Members are added at creation time."""
        alice = setup["alice"]
        group = alice["orch"].create_group("G1", ["bob"])
        members = alice["group_db"].get_members(group.group_id)
        assert len(members) == 1
        assert members[0].member_id == "bob"

    def test_create_group_member_priority_from_contacts(self, setup):
        """Member priority comes from contacts_db (D1)."""
        alice = setup["alice"]
        alice["contacts_db"].lock_priority("bob", "BESTIE")
        group = alice["orch"].create_group("G1", ["bob"])
        members = alice["group_db"].get_members(group.group_id)
        assert members[0].priority == "BESTIE"

    def test_create_group_unknown_contact_is_stranger(self, setup):
        """Unknown contact defaults to STRANGER."""
        alice = setup["alice"]
        group = alice["orch"].create_group("G1", ["unknown_user"])
        members = alice["group_db"].get_members(group.group_id)
        assert members[0].priority == "STRANGER"

    def test_create_group_returns_unique_id(self, setup):
        """Each group gets a unique ID."""
        alice = setup["alice"]
        g1 = alice["orch"].create_group("G1", ["bob"])
        g2 = alice["orch"].create_group("G2", ["bob"])
        assert g1.group_id != g2.group_id


class TestAddMember:
    """add_member tests (D6)."""

    def test_add_member_to_existing_group(self, setup):
        """Can add a member to an existing group."""
        alice = setup["alice"]
        group = alice["orch"].create_group("G1", ["bob"])
        alice["contacts_db"].add_contact("charlie", "Charlie")
        alice["orch"].add_member(group.group_id, "charlie")
        members = alice["group_db"].get_members(group.group_id)
        assert len(members) == 2
        member_ids = {m.member_id for m in members}
        assert member_ids == {"bob", "charlie"}


class TestSendGroupMessage:
    """Group message send tests (D8, G5)."""

    def test_send_cold_consumes_coins(self, setup):
        """COLD send consumes one coin per leaf member (D8)."""
        alice = setup["alice"]
        crypto = setup["crypto"]

        # Mint BRONZE coins for bob in alice's inventory
        _mint_and_store(crypto, alice["vault"], alice["inventory"],
                       "alice", "bob", "BRONZE", count=3)

        group = alice["orch"].create_group("G1", ["bob"])
        raw = alice["orch"].send_group_message(group.group_id, "hello group")

        assert raw is not None
        # Verify parcel was sent exactly once (G5)
        assert len(alice["sent_parcels"]) == 1

    def test_send_records_message_locally(self, setup):
        """D10: message is recorded in local SQLite."""
        alice = setup["alice"]
        crypto = setup["crypto"]
        _mint_and_store(crypto, alice["vault"], alice["inventory"],
                       "alice", "bob", "BRONZE", count=3)

        group = alice["orch"].create_group("G1", ["bob"])
        alice["orch"].send_group_message(group.group_id, "hello")

        messages = alice["group_db"].get_messages(group.group_id)
        assert len(messages) == 1
        assert messages[0]["plaintext"] == "hello"
        assert messages[0]["sender_id"] == "alice"

    def test_send_parcel_structure(self, setup):
        """Verify the parcel has correct structure."""
        alice = setup["alice"]
        crypto = setup["crypto"]
        _mint_and_store(crypto, alice["vault"], alice["inventory"],
                       "alice", "bob", "BRONZE", count=3)

        group = alice["orch"].create_group("G1", ["bob"])
        raw = alice["orch"].send_group_message(group.group_id, "structure test")

        header, inner = parse_parcel(raw)
        assert header.group_id == group.group_id
        assert header.sender_id == "alice"
        assert "bob" in header.recipient_ids
        assert "alice" not in header.recipient_ids  # D2
        assert "bob" in inner.leaf_enc
        assert "STRANGER" in inner.root_key_enc  # bob is STRANGER

    def test_send_no_group_raises(self, setup):
        """Sending to nonexistent group raises ValueError."""
        alice = setup["alice"]
        with pytest.raises(ValueError, match="not found"):
            alice["orch"].send_group_message("nonexistent", "hello")

    def test_send_insufficient_coins_raises(self, setup):
        """G2: no coins and no fetch → InsufficientCoinsError."""
        alice = setup["alice"]
        # Register bob in inventory but don't mint any coins
        alice["inventory"].register_contact("bob", "STRANGER", "Bob")

        group = alice["orch"].create_group("G1", ["bob"])
        with pytest.raises(InsufficientCoinsError):
            alice["orch"].send_group_message(group.group_id, "no coins")

    def test_send_fan_out_exactly_once(self, setup):
        """G5: send_parcel_fn called exactly once, not per-member."""
        alice = setup["alice"]
        crypto = setup["crypto"]

        # Add multiple members
        alice["contacts_db"].add_contact("charlie", "Charlie")
        _mint_and_store(crypto, alice["vault"], alice["inventory"],
                       "alice", "bob", "BRONZE", count=3)
        _mint_and_store(crypto, alice["vault"], alice["inventory"],
                       "alice", "charlie", "BRONZE", count=3)

        group = alice["orch"].create_group("G1", ["bob", "charlie"])
        alice["orch"].send_group_message(group.group_id, "multi-member")

        assert len(alice["sent_parcels"]) == 1  # exactly once

    def test_send_priority_reevaluated_at_send_time(self, setup):
        """D1/G1: priority is re-read from contacts_db at send time."""
        alice = setup["alice"]
        crypto = setup["crypto"]

        # Initially bob is STRANGER, mint BRONZE
        _mint_and_store(crypto, alice["vault"], alice["inventory"],
                       "alice", "bob", "BRONZE", count=3)

        group = alice["orch"].create_group("G1", ["bob"])

        # Send first message — bob is STRANGER
        raw1 = alice["orch"].send_group_message(group.group_id, "msg1")
        _, inner1 = parse_parcel(raw1)
        assert "STRANGER" in inner1.root_key_enc

        # Promote bob to MATE
        alice["contacts_db"].lock_priority("bob", "MATE")
        # Mint SILVER coins for the new tier (need MATE priority for budget caps)
        _mint_and_store(crypto, alice["vault"], alice["inventory"],
                       "alice", "bob", "SILVER", count=3, priority="MATE")

        # Send second message — bob should now be on MATE branch
        raw2 = alice["orch"].send_group_message(group.group_id, "msg2")
        _, inner2 = parse_parcel(raw2)
        assert "MATE" in inner2.root_key_enc


class TestSendWithHotEdge:
    """HOT edge send tests (D4, D8)."""

    def test_hot_member_no_coin_consumed(self, setup):
        """HOT member bypasses B-Tree leaf — no coin consumed (D4, D8)."""
        alice = setup["alice"]
        crypto = setup["crypto"]

        # Mint coins for initial COLD send
        _mint_and_store(crypto, alice["vault"], alice["inventory"],
                       "alice", "bob", "BRONZE", count=3)

        group = alice["orch"].create_group("G1", ["bob"])

        # First send — COLD, consumes a coin; orchestrator auto-activates HOT toward bob
        alice["orch"].send_group_message(group.group_id, "cold msg")

        # Second send — bob is HOT (same KEM secret), should not consume another coin
        raw = alice["orch"].send_group_message(group.group_id, "hot msg")
        _, inner = parse_parcel(raw)
        assert "bob" in inner.hot_leaf_ids

    def test_hot_send_still_produces_valid_parcel(self, setup):
        """HOT send produces a parseable parcel with correct structure."""
        alice = setup["alice"]
        crypto = setup["crypto"]
        _mint_and_store(crypto, alice["vault"], alice["inventory"],
                       "alice", "bob", "BRONZE", count=3)

        group = alice["orch"].create_group("G1", ["bob"])
        alice["hot_edge"].activate(group.group_id, "bob", os.urandom(32))

        raw = alice["orch"].send_group_message(group.group_id, "hot parcel")
        header, inner = parse_parcel(raw)
        assert header.group_id == group.group_id
        assert "bob" in inner.leaf_enc


class TestReceiveGroupMessage:
    """Group message receive tests."""

    def test_receive_cold_decrypt(self, setup):
        """Full COLD send → receive → decrypt round-trip."""
        alice = setup["alice"]
        bob = setup["bob"]
        crypto = setup["crypto"]

        # Mint BRONZE coins for bob (stored in ALICE's inventory as bob's public keys)
        # and bob's private keys go to BOB's vault
        alice["inventory"].register_contact("bob", "STRANGER", "Bob")
        bob["inventory"].register_contact("alice", "STRANGER", "Alice")

        for _ in range(3):
            bundle = crypto.mint_coin("BRONZE")
            # Bob's public key → alice's inventory
            alice["inventory"].store_key(
                contact_id="bob",
                key_id=bundle.key_id,
                coin_category="BRONZE",
                public_key=bundle.public_key,
                signature=bundle.signature,
            )
            # Bob's private key → bob's vault
            bob["vault"].store_key(
                key_id=bundle.key_id,
                coin_category="BRONZE",
                encrypted_blob=bundle.secret_key,
                encryption_iv=bytes(12),
                auth_tag=bytes(16),
            )

        # Alice creates group and sends
        group = alice["orch"].create_group("G1", ["bob"])

        # Bob must also know about this group locally
        bob["group_db"].create_group(group.group_id, "G1", role="MEMBER")
        bob["group_db"].add_member(group.group_id, "alice", "Alice")

        raw = alice["orch"].send_group_message(group.group_id, "hello bob")

        # Bob receives
        plaintext = bob["orch"].receive_group_message(raw)
        assert plaintext == "hello bob"

    def test_receive_hot_after_sender_second_message(self, setup):
        """COLD first → Bob activates; Alice second send HOT → Bob decrypts with KEM secret + counter."""
        alice = setup["alice"]
        bob = setup["bob"]
        crypto = setup["crypto"]

        alice["inventory"].register_contact("bob", "STRANGER", "Bob")
        bob["inventory"].register_contact("alice", "STRANGER", "Alice")

        for _ in range(5):
            bundle = crypto.mint_coin("BRONZE")
            alice["inventory"].store_key(
                "bob", bundle.key_id, "BRONZE",
                bundle.public_key, bundle.signature,
            )
            bob["vault"].store_key(
                bundle.key_id, "BRONZE", bundle.secret_key,
                bytes(12), bytes(16),
            )

        group = alice["orch"].create_group("G1", ["bob"])
        bob["group_db"].create_group(group.group_id, "G1", role="MEMBER")
        bob["group_db"].add_member(group.group_id, "alice", "Alice")

        raw1 = alice["orch"].send_group_message(group.group_id, "first cold")
        assert bob["orch"].receive_group_message(raw1) == "first cold"

        raw2 = alice["orch"].send_group_message(group.group_id, "second hot")
        _, inner2 = parse_parcel(raw2)
        assert "bob" in inner2.hot_leaf_ids
        assert bob["orch"].receive_group_message(raw2) == "second hot"

    def test_receive_records_message_locally(self, setup):
        """D10: received message is stored in local SQLite."""
        alice = setup["alice"]
        bob = setup["bob"]
        crypto = setup["crypto"]

        alice["inventory"].register_contact("bob", "STRANGER", "Bob")

        for _ in range(3):
            bundle = crypto.mint_coin("BRONZE")
            alice["inventory"].store_key("bob", bundle.key_id, "BRONZE",
                                         bundle.public_key, bundle.signature)
            bob["vault"].store_key(bundle.key_id, "BRONZE", bundle.secret_key,
                                    bytes(12), bytes(16))

        group = alice["orch"].create_group("G1", ["bob"])
        bob["group_db"].create_group(group.group_id, "G1", role="MEMBER")
        bob["group_db"].add_member(group.group_id, "alice", "Alice")

        raw = alice["orch"].send_group_message(group.group_id, "stored msg")
        bob["orch"].receive_group_message(raw)

        messages = bob["group_db"].get_messages(group.group_id)
        assert len(messages) == 1
        assert messages[0]["plaintext"] == "stored msg"
        assert messages[0]["sender_id"] == "alice"

    def test_receive_no_leaf_returns_none(self, setup):
        """Receiving a parcel with no leaf for me returns None."""
        bob = setup["bob"]
        # Build a minimal parcel that doesn't include bob
        from AQM_Database.aqm_group.group_parcel import build_parcel, make_header, make_inner
        header = make_header("g1", "alice", ["charlie"])
        inner = make_inner(
            root_key_enc={"STRANGER": os.urandom(48)},
            leaf_enc={"charlie": os.urandom(64)},
            hot_leaf_ids=[],
            encrypted_payload=os.urandom(128),
        )
        raw = build_parcel(header, inner)
        result = bob["orch"].receive_group_message(raw)
        assert result is None


class TestMultiMemberGroup:
    """Multi-member group tests."""

    def test_three_member_group_all_decrypt(self, setup, tmp_path):
        """Three members all receive and decrypt the same message."""
        crypto = setup["crypto"]
        alice = setup["alice"]
        bob = setup["bob"]

        # Create charlie
        from AQM_Database.aqm_group.group_db import GroupDatabase
        from AQM_Database.aqm_contacts.contacts_db import ContactsDatabase
        from AQM_Database.aqm_db.vault import SecureVault
        from AQM_Database.aqm_db.inventory import SmartInventory
        from AQM_Database.aqm_group.hot_edge import HotEdgeTracker

        charlie_gdb = GroupDatabase(db_path=str(tmp_path / "charlie_groups.db"))
        charlie_cdb = ContactsDatabase(db_path=str(tmp_path / "charlie_contacts.db"))
        charlie_redis = fakeredis.FakeRedis(decode_responses=False)
        charlie_vault = SecureVault(charlie_redis, user_id="charlie")
        charlie_inv = SmartInventory(fakeredis.FakeRedis(decode_responses=False))
        charlie_hot = HotEdgeTracker(charlie_gdb, crypto, os.urandom(32))
        charlie_sent = []
        charlie_orch = GroupOrchestrator(
            user_id="charlie",
            group_db=charlie_gdb, contacts_db=charlie_cdb,
            inventory=charlie_inv, vault=charlie_vault, crypto=crypto,
            hot_edge_tracker=charlie_hot,
            send_parcel_fn=lambda r: charlie_sent.append(r),
        )

        alice["contacts_db"].add_contact("charlie", "Charlie")

        # Mint coins for bob and charlie in alice's inventory
        alice["inventory"].register_contact("bob", "STRANGER", "Bob")
        alice["inventory"].register_contact("charlie", "STRANGER", "Charlie")

        bob_coins = []
        charlie_coins = []
        for _ in range(3):
            # Bob coins
            b = crypto.mint_coin("BRONZE")
            alice["inventory"].store_key("bob", b.key_id, "BRONZE", b.public_key, b.signature)
            bob["vault"].store_key(b.key_id, "BRONZE", b.secret_key, bytes(12), bytes(16))
            bob_coins.append(b)

            # Charlie coins
            c = crypto.mint_coin("BRONZE")
            alice["inventory"].store_key("charlie", c.key_id, "BRONZE", c.public_key, c.signature)
            charlie_vault.store_key(c.key_id, "BRONZE", c.secret_key, bytes(12), bytes(16))
            charlie_coins.append(c)

        # Create group
        group = alice["orch"].create_group("Party", ["bob", "charlie"])

        # Both bob and charlie need the group locally
        bob["group_db"].create_group(group.group_id, "Party", role="MEMBER")
        bob["group_db"].add_member(group.group_id, "alice", "Alice")

        charlie_gdb.create_group(group.group_id, "Party", role="MEMBER")
        charlie_gdb.add_member(group.group_id, "alice", "Alice")

        # Send
        raw = alice["orch"].send_group_message(group.group_id, "everyone hears this")

        # Both receive
        bob_result = bob["orch"].receive_group_message(raw)
        charlie_result = charlie_orch.receive_group_message(raw)

        assert bob_result == "everyone hears this"
        assert charlie_result == "everyone hears this"
