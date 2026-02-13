"""
ChatSession — orchestrates the full AQM lifecycle per user.

Setup    → connect Redis (vault/inventory) + PostgreSQL pool
Provision → mint coins, upload to server, register partner, fetch & cache
Send     → ContextManager selects tier → select_coin (with fallback) → encrypt → publish
Receive  → deserialize → vault.fetch_key → decrypt → vault.burn_key → display
"""

import asyncio
import base64
import time
import uuid
from typing import Optional, Callable
from uuid import UUID

import redis

from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.crypto_engine import CryptoEngine, mint_coin
from AQM_Database.aqm_shared.context_manager import (
    ContextManager, DeviceContext,
    SCENARIO_A, SCENARIO_B, SCENARIO_C,
)
from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_db.connection import create_vault_client, create_inventory_client
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer
from AQM_Database.aqm_server import config as srv_config
from AQM_Database.aqm_server.db import create_pool, close_pool
from AQM_Database.bridge import upload_coins, sync_inventory
from AQM_Database.chat.protocol import (
    build_message, simulate_decrypt, ChatMessage,
)
from AQM_Database.chat.transport import ChatTransport
from AQM_Database.prototype import Display


# Mint plan by priority — how many coins the user mints for themselves
MINT_PLANS = {
    "BESTIE":   [("GOLD", 5), ("SILVER", 4), ("BRONZE", 1)],
    "MATE":     [("GOLD", 0), ("SILVER", 6), ("BRONZE", 4)],
    "STRANGER": [],
}


class ChatSession:
    """Full AQM lifecycle for one chat participant."""

    def __init__(
        self,
        user_name: str,
        partner_name: str,
        priority: str,
        *,
        vault_client: Optional[redis.Redis] = None,
        inv_client: Optional[redis.Redis] = None,
        pool=None,
        transport: Optional[ChatTransport] = None,
    ):
        self.user_name = user_name
        self.partner_name = partner_name
        self.priority = priority

        # Deterministic UUIDs derived from names for reproducibility
        self.user_id = UUID(
            bytes=uuid.uuid5(uuid.NAMESPACE_DNS, f"aqm.{user_name}").bytes
        )
        self.partner_id = UUID(
            bytes=uuid.uuid5(uuid.NAMESPACE_DNS, f"aqm.{partner_name}").bytes
        )

        self._vault_client = vault_client
        self._inv_client = inv_client
        self._pool = pool
        self._transport = transport

        self.vault: Optional[SecureVault] = None
        self.inventory: Optional[SmartInventory] = None
        self.server: Optional[CoinInventoryServer] = None
        self.engine = CryptoEngine()
        self.cm = ContextManager()
        self._owns_pool = False
        self._on_receive: Optional[Callable[[str, str, str, bool], None]] = None

    async def setup(self) -> None:
        """Connect to Redis and PostgreSQL."""
        if self._vault_client is None:
            self._vault_client = create_vault_client()
        if self._inv_client is None:
            self._inv_client = create_inventory_client()
        if self._pool is None:
            self._pool = await create_pool(
                srv_config.PG_DSN,
                srv_config.PG_POOL_MIN_SIZE,
                srv_config.PG_POOL_MAX_SIZE,
            )
            self._owns_pool = True

        self.vault = SecureVault(self._vault_client)
        self.inventory = SmartInventory(self._inv_client)
        self.server = CoinInventoryServer(self._pool)

        if self._transport is None:
            self._transport = ChatTransport()

    async def provision(self) -> dict[str, int]:
        """Mint coins for this user and upload to server.

        Returns dict of {tier: count_minted}.
        """
        plan = MINT_PLANS.get(self.priority, [])
        all_uploads: list[CoinUpload] = []
        minted = {}

        for tier, count in plan:
            if count == 0:
                minted[tier] = 0
                continue
            for _ in range(count):
                bundle = mint_coin(self.engine, tier)
                self.vault.store_key(
                    key_id=bundle.key_id,
                    coin_category=bundle.coin_category,
                    encrypted_blob=bundle.encrypted_blob,
                    encryption_iv=bundle.encryption_iv,
                    auth_tag=bundle.auth_tag,
                )
                all_uploads.append(CoinUpload(
                    key_id=bundle.key_id,
                    coin_category=bundle.coin_category,
                    public_key_blob=bundle.public_key,
                    signature_blob=bundle.signature,
                ))
            minted[tier] = count

        if all_uploads:
            await upload_coins(self.server, self.user_id, all_uploads)

        return minted

    async def register_and_fetch(
        self,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> dict[str, int]:
        """Register partner and fetch+cache their public keys.

        Polls the server until partner's keys appear (or timeout).
        Returns dict of {tier: count_fetched}.
        """
        self.inventory.register_contact(
            self.partner_name, self.priority, display_name=self.partner_name,
        )

        caps = config.BUDGET_CAPS[self.priority]
        total_want = sum(caps.values())
        if total_want == 0:
            return {"GOLD": 0, "SILVER": 0, "BRONZE": 0}

        # Poll until partner has uploaded keys
        deadline = time.time() + timeout
        while time.time() < deadline:
            inv = await self.server.get_inventory_count(self.partner_id)
            if inv.gold + inv.silver + inv.bronze > 0:
                break
            await asyncio.sleep(poll_interval)

        return await sync_inventory(
            self.server, self.inventory,
            self.partner_name, self.partner_id, self.user_id,
        )

    def coin_status(self) -> dict[str, int]:
        """Return remaining coin counts per tier in local inventory."""
        try:
            summary = self.inventory.get_inventory(self.partner_name)
            return {
                "GOLD": summary.gold_count,
                "SILVER": summary.silver_count,
                "BRONZE": summary.bronze_count,
            }
        except Exception:
            return {"GOLD": 0, "SILVER": 0, "BRONZE": 0}

    def send_message(
        self,
        plaintext: str,
        context: DeviceContext,
    ) -> Optional[ChatMessage]:
        """Select coin based on device context, encrypt, and publish.

        Returns the ChatMessage sent, or None if no coins available.
        """
        tier = self.cm.select_coin(context)
        entry = self.inventory.select_coin(self.partner_name, tier)
        if entry is None:
            return None

        msg = build_message(
            sender_id=str(self.user_id),
            recipient_id=str(self.partner_id),
            coin_tier=entry.coin_category,
            key_id=entry.key_id,
            public_key=entry.public_key,
            plaintext=plaintext,
            device_context=context.label,
        )

        self._transport.publish(str(self.partner_id), msg)
        return msg

    def start_listening(
        self,
        on_receive: Callable[..., None],
    ) -> None:
        """Start listening for incoming messages.

        on_receive is called with keyword args:
            sender, plaintext, tier, verified, key_id, burned, device_context
        """
        self._on_receive = on_receive
        self._transport.subscribe(str(self.user_id), self._handle_incoming)

    def _handle_incoming(self, msg: ChatMessage) -> None:
        """Process an incoming ChatMessage: decrypt + burn + callback."""
        ciphertext = base64.b64decode(msg.ciphertext_b64)
        public_key = base64.b64decode(msg.public_key_b64)

        # Decrypt
        plaintext, verified = simulate_decrypt(ciphertext, public_key)

        # Burn the private key from vault (if we hold it)
        burned = False
        vault_entry = self.vault.fetch_key(msg.key_id)
        if vault_entry:
            self.vault.burn_key(msg.key_id)
            burned = True

        if self._on_receive:
            self._on_receive(
                sender=msg.sender_id,
                plaintext=plaintext,
                tier=msg.coin_tier,
                verified=verified,
                key_id=msg.key_id,
                burned=burned,
                device_context=msg.device_context,
            )

    def cleanup_user_data(self) -> None:
        """Delete this user's keys from Redis (no flushdb).

        Removes vault keys and inventory data for this user only.
        """
        # Clean vault keys for this user (scan for all vault keys)
        cursor = 0
        while True:
            cursor, keys = self._vault_client.scan(
                cursor=cursor,
                match=f"{config.VAULT_KEY_PREFIX}:*",
                count=100,
            )
            if keys:
                self._vault_client.delete(*keys)
            if cursor == 0:
                break
        self._vault_client.delete(config.VAULT_STATS_KEY)

        # Clean inventory data for the partner contact
        for tier in ("GOLD", "SILVER", "BRONZE"):
            idx_key = f"{config.INV_IDX_PREFIX}:{self.partner_name}:{tier}"
            self._inv_client.delete(idx_key)
        meta_key = f"{config.INV_META_PREFIX}:{self.partner_name}"
        self._inv_client.delete(meta_key)

        # Clean any remaining inventory entry hashes
        cursor = 0
        while True:
            cursor, keys = self._inv_client.scan(
                cursor=cursor,
                match=f"{config.INV_KEY_PREFIX}:{self.partner_name}:*",
                count=100,
            )
            if keys:
                self._inv_client.delete(*keys)
            if cursor == 0:
                break

    async def cleanup_server_data(self) -> None:
        """Delete this user's coins from PostgreSQL (no TRUNCATE)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM coin_inventory WHERE user_id = $1",
                self.user_id,
            )

    async def teardown(self) -> None:
        """Clean up connections."""
        if self._transport:
            self._transport.close()
        if self._vault_client:
            self._vault_client.close()
        if self._inv_client:
            self._inv_client.close()
        if self._owns_pool:
            await close_pool()


async def run_auto_demo() -> None:
    """Single-terminal auto demo — runs all 3 priority scenarios.

    For each priority, Alice and Bob provision, exchange messages under
    three device scenarios, then clean up.
    """
    Display.banner()
    print(f"{Display.CYAN}{Display.BOLD}"
          f"    Two-User Chat Demo — Priority Scenarios"
          f"{Display.RESET}\n")

    scenarios = [
        ("A", SCENARIO_A),
        ("B", SCENARIO_B),
        ("C", SCENARIO_C),
    ]

    for priority in ("BESTIE", "MATE", "STRANGER"):
        Display.phase_header(
            ["BESTIE", "MATE", "STRANGER"].index(priority) + 1,
            f"Priority: {priority}",
        )

        alice = ChatSession("alice", "bob", priority)
        bob = ChatSession("bob", "alice", priority)

        await alice.setup()
        # Bob shares the pool but uses separate Redis clients
        bob._vault_client = create_vault_client()
        bob._inv_client = create_inventory_client()
        bob._pool = alice._pool
        bob.vault = SecureVault(bob._vault_client)
        bob.inventory = SmartInventory(bob._inv_client)
        bob.server = CoinInventoryServer(bob._pool)
        bob._transport = ChatTransport()

        received_messages: list[dict] = []

        def on_bob_receive(**kwargs):
            received_messages.append(kwargs)

        try:
            # Clean slate for this priority scenario
            alice.cleanup_user_data()
            bob.cleanup_user_data()
            await alice.cleanup_server_data()
            await bob.cleanup_server_data()

            # Provision
            Display.section("Provisioning")

            bob_minted = await bob.provision()
            Display.success(f"Bob minted: {bob_minted}")

            alice_minted = await alice.provision()
            Display.success(f"Alice minted: {alice_minted}")

            # Register and fetch
            fetched = await alice.register_and_fetch(timeout=5.0)
            Display.success(f"Alice cached Bob's keys: {fetched}")

            caps = config.BUDGET_CAPS[priority]
            total_cached = sum(fetched.values())

            if total_cached == 0:
                Display.arrow(
                    f"STRANGER has zero budget — cannot message"
                )
                continue

            # Bob listens
            bob.start_listening(on_bob_receive)
            await asyncio.sleep(0.1)

            # Alice sends under each device scenario
            Display.section("Sending messages")
            for label, ctx in scenarios:
                tier = alice.cm.select_coin(ctx)
                Display.arrow(
                    f"Scenario {label}: {ctx.label} "
                    f"→ wants {Display.tier_label(tier)}"
                )

                msg = alice.send_message(
                    f"Hello from scenario {label}!", ctx,
                )
                if msg:
                    Display.success(
                        f"Sent via {Display.tier_label(msg.coin_tier)}  "
                        f"key={msg.key_id[:8]}…"
                    )
                else:
                    Display.arrow(
                        f"No coins available (tier exhausted or fallback empty)"
                    )

            # Give pub/sub a moment to deliver
            await asyncio.sleep(0.3)

            # Show received
            Display.section("Bob received")
            if received_messages:
                for rm in received_messages:
                    tag = f"{Display.GREEN}verified" if rm["verified"] else f"{Display.RED}FAILED"
                    burn_tag = f"  {Display.RED}burned{Display.RESET}" if rm.get("burned") else ""
                    Display.success(
                        f"[{Display.tier_label(rm['tier'])}] \"{rm['plaintext']}\"  "
                        f"({tag}{Display.RESET}){burn_tag}"
                    )
            else:
                Display.arrow("No messages received")

        finally:
            alice.cleanup_user_data()
            bob.cleanup_user_data()
            await alice.cleanup_server_data()
            await bob.cleanup_server_data()

            bob._transport.close()
            bob._vault_client.close()
            bob._inv_client.close()
            await alice.teardown()

    print(f"\n{Display.GREEN}{Display.BOLD}  "
          f"Chat demo complete{Display.RESET}\n")
