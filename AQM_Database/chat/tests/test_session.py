"""Tests for chat.session — lifecycle, priorities, exhaustion.

Tests marked with pytestmark need Docker (PostgreSQL). Tests using only
fakeredis fixtures are synchronous and need no Docker.
"""

import os
import pytest
import pytest_asyncio
import asyncio
from uuid import uuid4

from AQM_Database.aqm_shared.crypto_engine import CryptoEngine, mint_coin
from AQM_Database.aqm_shared.context_manager import (
    SCENARIO_A, SCENARIO_B, SCENARIO_C,
)
from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_shared import config
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.bridge import upload_coins, fetch_and_cache
from AQM_Database.chat.session import ChatSession, MINT_PLANS
from AQM_Database.chat.protocol import simulate_decrypt

pytestmark = pytest.mark.asyncio


# ─── Provision tests ───

async def test_provision_bestie(fake_vault_client, fake_inv_client, server, pg_pool):
    session = ChatSession(
        "alice", "bob", "BESTIE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server

    minted = await session.provision()
    assert minted["GOLD"] == 5
    assert minted["SILVER"] == 4
    assert minted["BRONZE"] == 1


async def test_provision_mate(fake_vault_client, fake_inv_client, server, pg_pool):
    session = ChatSession(
        "alice", "bob", "MATE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server

    minted = await session.provision()
    assert minted.get("GOLD", 0) == 0
    assert minted["SILVER"] == 6
    assert minted["BRONZE"] == 4


async def test_provision_stranger_mints_nothing(fake_vault_client, fake_inv_client, server, pg_pool):
    session = ChatSession(
        "alice", "bob", "STRANGER",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server

    minted = await session.provision()
    assert sum(minted.values()) == 0


# ─── Register and fetch tests ───

async def test_register_and_fetch_bestie(fake_vault_client, fake_inv_client, server, pg_pool):
    engine = CryptoEngine()
    partner_id = uuid4()

    # Simulate partner uploading coins
    uploads = []
    for tier, count in MINT_PLANS["BESTIE"]:
        for _ in range(count):
            bundle = mint_coin(engine, tier)
            uploads.append(CoinUpload(
                key_id=bundle.key_id,
                coin_category=bundle.coin_category,
                public_key_blob=bundle.public_key,
                signature_blob=bundle.signature,
            ))
    await upload_coins(server, partner_id, uploads)

    session = ChatSession(
        "alice", "bob", "BESTIE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server
    session.partner_id = partner_id

    fetched = await session.register_and_fetch(timeout=2.0)
    caps = config.BUDGET_CAPS["BESTIE"]
    assert fetched["GOLD"] == caps["GOLD"]
    assert fetched["SILVER"] == caps["SILVER"]
    assert fetched["BRONZE"] == caps["BRONZE"]


async def test_register_and_fetch_stranger_gets_nothing(fake_vault_client, fake_inv_client, server, pg_pool):
    session = ChatSession(
        "alice", "bob", "STRANGER",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server

    fetched = await session.register_and_fetch(timeout=1.0)
    assert fetched == {"GOLD": 0, "SILVER": 0, "BRONZE": 0}


# ─── Send message tests ───

async def test_send_message_gold(fake_vault_client, fake_inv_client, server, pg_pool):
    engine = CryptoEngine()
    partner_id = uuid4()

    # Upload partner coins
    uploads = []
    for _ in range(5):
        bundle = mint_coin(engine, "GOLD")
        uploads.append(CoinUpload(
            key_id=bundle.key_id,
            coin_category="GOLD",
            public_key_blob=bundle.public_key,
            signature_blob=bundle.signature,
        ))
    await upload_coins(server, partner_id, uploads)

    session = ChatSession(
        "alice", "bob", "BESTIE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server
    session.partner_id = partner_id
    # Need a transport that doesn't require live Redis
    from unittest.mock import MagicMock
    mock_transport = MagicMock()
    session._transport = mock_transport

    await session.register_and_fetch(timeout=2.0)

    msg = session.send_message("Hello!", SCENARIO_A)
    assert msg is not None
    assert msg.coin_tier == "GOLD"


async def test_send_mate_falls_back_from_gold(fake_vault_client, fake_inv_client, server, pg_pool):
    engine = CryptoEngine()
    partner_id = uuid4()

    # Upload partner coins — MATE budget: 0G/6S/4B
    uploads = []
    for _ in range(6):
        bundle = mint_coin(engine, "SILVER")
        uploads.append(CoinUpload(
            key_id=bundle.key_id,
            coin_category="SILVER",
            public_key_blob=bundle.public_key,
            signature_blob=bundle.signature,
        ))
    for _ in range(4):
        bundle = mint_coin(engine, "BRONZE")
        uploads.append(CoinUpload(
            key_id=bundle.key_id,
            coin_category="BRONZE",
            public_key_blob=bundle.public_key,
            signature_blob=bundle.signature,
        ))
    await upload_coins(server, partner_id, uploads)

    session = ChatSession(
        "alice", "bob", "MATE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server
    session.partner_id = partner_id
    from unittest.mock import MagicMock
    session._transport = MagicMock()

    await session.register_and_fetch(timeout=2.0)

    # SCENARIO_A wants GOLD → should fall back to SILVER (MATE has no GOLD)
    msg = session.send_message("Hello mate!", SCENARIO_A)
    assert msg is not None
    assert msg.coin_tier == "SILVER"


async def test_send_stranger_returns_none(fake_vault_client, fake_inv_client, server, pg_pool):
    session = ChatSession(
        "alice", "bob", "STRANGER",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server
    from unittest.mock import MagicMock
    session._transport = MagicMock()

    # Register contact — STRANGER gets 0 budget, register_and_fetch returns 0s
    await session.register_and_fetch(timeout=1.0)

    msg = session.send_message("Hello?", SCENARIO_A)
    assert msg is None


# ─── Key exhaustion tests ───

async def test_key_exhaustion(fake_vault_client, fake_inv_client, server, pg_pool):
    engine = CryptoEngine()
    partner_id = uuid4()

    # Upload exactly 1 BRONZE coin
    bundle = mint_coin(engine, "BRONZE")
    await upload_coins(server, partner_id, [CoinUpload(
        key_id=bundle.key_id,
        coin_category="BRONZE",
        public_key_blob=bundle.public_key,
        signature_blob=bundle.signature,
    )])

    session = ChatSession(
        "alice", "bob", "BESTIE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server
    session.partner_id = partner_id
    from unittest.mock import MagicMock
    session._transport = MagicMock()

    await session.register_and_fetch(timeout=2.0)

    # Use the single BRONZE coin (SCENARIO_C → BRONZE)
    msg1 = session.send_message("First", SCENARIO_C)
    assert msg1 is not None
    assert msg1.coin_tier == "BRONZE"

    # Now BRONZE is exhausted — SCENARIO_C has no fallback
    msg2 = session.send_message("Second", SCENARIO_C)
    assert msg2 is None


# ─── Burn-after-receive tests ───

async def test_burn_after_receive(fake_vault_client, fake_inv_client, server, pg_pool):
    engine = CryptoEngine()

    # Mint a coin and store private key in vault
    bundle = mint_coin(engine, "GOLD")
    vault = SecureVault(fake_vault_client)
    vault.store_key(
        key_id=bundle.key_id,
        coin_category="GOLD",
        encrypted_blob=bundle.encrypted_blob,
        encryption_iv=bundle.encryption_iv,
        auth_tag=bundle.auth_tag,
    )

    # Verify key exists
    assert vault.fetch_key(bundle.key_id) is not None

    session = ChatSession(
        "bob", "alice", "BESTIE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = vault
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server

    # Simulate receiving a message that references this key
    import base64
    from AQM_Database.chat.protocol import build_message, ChatMessage
    msg = build_message(
        sender_id="alice-uuid",
        recipient_id="bob-uuid",
        coin_tier="GOLD",
        key_id=bundle.key_id,
        public_key=bundle.public_key,
        plaintext="Burn test",
    )

    received = []
    session._on_receive = lambda **kwargs: received.append(kwargs)
    session._handle_incoming(msg)

    # Private key should be burned
    assert vault.fetch_key(bundle.key_id) is None
    assert len(received) == 1
    assert received[0]["plaintext"] == "Burn test"
    assert received[0]["verified"] is True
    assert received[0]["burned"] is True


# ─── MINT_PLANS tests ───

def test_mint_plans_bestie_total():
    total = sum(c for _, c in MINT_PLANS["BESTIE"])
    assert total == 10


def test_mint_plans_mate_no_gold():
    for tier, count in MINT_PLANS["MATE"]:
        if tier == "GOLD":
            assert count == 0


def test_mint_plans_stranger_empty():
    assert MINT_PLANS["STRANGER"] == []


# ─── coin_status tests ───

async def test_coin_status_after_fetch(fake_vault_client, fake_inv_client, server, pg_pool):
    engine = CryptoEngine()
    partner_id = uuid4()

    # Upload BESTIE-worth of coins
    uploads = []
    for tier, count in [("GOLD", 5), ("SILVER", 4), ("BRONZE", 1)]:
        for _ in range(count):
            bundle = mint_coin(engine, tier)
            uploads.append(CoinUpload(
                key_id=bundle.key_id,
                coin_category=tier,
                public_key_blob=bundle.public_key,
                signature_blob=bundle.signature,
            ))
    await upload_coins(server, partner_id, uploads)

    session = ChatSession(
        "alice", "bob", "BESTIE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server
    session.partner_id = partner_id

    await session.register_and_fetch(timeout=2.0)

    status = session.coin_status()
    assert status["GOLD"] == 5
    assert status["SILVER"] == 4
    assert status["BRONZE"] == 1


async def test_coin_status_decrements_after_send(fake_vault_client, fake_inv_client, server, pg_pool):
    engine = CryptoEngine()
    partner_id = uuid4()

    bundle = mint_coin(engine, "BRONZE")
    await upload_coins(server, partner_id, [CoinUpload(
        key_id=bundle.key_id,
        coin_category="BRONZE",
        public_key_blob=bundle.public_key,
        signature_blob=bundle.signature,
    )])

    session = ChatSession(
        "alice", "bob", "BESTIE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server
    session.partner_id = partner_id
    from unittest.mock import MagicMock
    session._transport = MagicMock()

    await session.register_and_fetch(timeout=2.0)
    assert session.coin_status()["BRONZE"] == 1

    session.send_message("test", SCENARIO_C)
    assert session.coin_status()["BRONZE"] == 0


# ─── Cleanup tests ───

async def test_cleanup_user_data(fake_vault_client, fake_inv_client, server, pg_pool):
    engine = CryptoEngine()

    session = ChatSession(
        "alice", "bob", "BESTIE",
        vault_client=fake_vault_client,
        inv_client=fake_inv_client,
        pool=pg_pool,
    )
    session.vault = SecureVault(fake_vault_client)
    session.inventory = SmartInventory(fake_inv_client)
    session.server = server

    # Provision to create some data
    await session.provision()

    # Verify data exists
    stats = session.vault.get_stats()
    assert stats.active_gold + stats.active_silver + stats.active_bronze > 0

    # Cleanup
    session.cleanup_user_data()

    # Verify vault is clean
    stats = session.vault.get_stats()
    assert stats.active_gold == 0
    assert stats.active_silver == 0
    assert stats.active_bronze == 0
