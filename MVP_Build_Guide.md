# AQM MVP Build Guide
---

## 0. Project Context

### What AQM Is
Amortized Quantum Messaging — a post-quantum secure messaging protocol where:
- Expensive PQC key generation (minting) happens when device is idle (charging/WiFi)
- Keys are pre-fetched and cached locally per contact priority (Bestie/Mate/Stranger)
- Actual messaging is 0-RTT using cached keys
- Server is a "Blind Courier" — stores public keys, relays encrypted parcels, decrypts nothing
- Three coin tiers: GOLD (full PQC), SILVER (hybrid), BRONZE (classical fallback)

### What Exists (KEEP — production-tested)
```
aqm_db/vault.py           — SecureVault (Redis db=0), private key lifecycle
aqm_db/inventory.py       — SmartInventory (Redis db=1), public key cache + FIFO selection
aqm_db/garbage_collector.py — inactive contact cleanup
aqm_db/connection.py      — Redis client factory
aqm_db/stats.py           — StorageReporter
aqm_server/db.py           — PostgreSQL connection pool (asyncpg)
aqm_server/coin_inventory.py — CoinInventoryServer (upload, fetch, purge, hard_delete)
aqm_server/api.py          — FastAPI endpoints
aqm_server/migrations/     — SQL schema
aqm_shared/config.py       — constants, budget caps, enums
aqm_shared/types.py        — dataclasses (VaultEntry, InventoryEntry, CoinUpload, etc.)
aqm_shared/errors.py       — exception hierarchy
bridge.py                  — fetch_and_cache, upload_coins, sync_inventory
prototype.py               — 4-phase lifecycle demo (terminal output)
docker-compose.yml         — Redis 7 + PostgreSQL 16
173 tests                  — all passing
```

### What Gets DELETED
```
chat/                      — ENTIRE folder. Terminal pub/sub chat is throwaway.
                             The new MVP replaces this with real networking + UI.
```

### What Gets REWRITTEN (not deleted — rebuilt in place)
```
aqm_shared/crypto_engine.py — Currently uses mock/pseudo PQC. Must use REAL
                               liboqs for Kyber-768 + Dilithium-3. Zero mocks.
                               Zero fallbacks to random padding.
                               See Section 3 for requirements.

aqm_shared/context_manager.py — Logic is correct but needs real sensor integration
                                 hooks (battery/WiFi/signal APIs) instead of
                                 hardcoded SCENARIO_A/B/C constants.
```

### What Gets BUILT (new)
```
aqm_contacts/              — NEW: Contacts database (SQLite on device)
aqm_network/               — NEW: Real LAN/WAN networking (WebSocket + HTTP)
aqm_chat_ui/               — NEW: Chat frontend (web-based or desktop)
aqm_session/               — NEW: Session ratchet (AES-256 key derivation, 100-msg window)
```

---

## 1. Architecture Overview — The MVP

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ALICE'S DEVICE                               │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌───────────────────┐ │
│  │ Chat UI  │  │ Context  │  │ Session   │  │ Crypto Engine     │ │
│  │ (Web/    │  │ Manager  │  │ Ratchet   │  │ (REAL liboqs)     │ │
│  │ Desktop) │  │          │  │           │  │ Kyber+Dilithium   │ │
│  └────┬─────┘  └────┬─────┘  └─────┬─────┘  └────────┬──────────┘ │
│       │              │              │                  │            │
│  ┌────┴──────────────┴──────────────┴──────────────────┴──────────┐ │
│  │                    Application Layer                            │ │
│  │         (orchestrates mint → fetch → send → burn)              │ │
│  └────┬────────────────┬────────────────┬─────────────────────────┘ │
│       │                │                │                           │
│  ┌────┴─────┐   ┌──────┴──────┐   ┌────┴──────────┐               │
│  │ Vault    │   │ Inventory   │   │ Contacts DB   │               │
│  │ Redis    │   │ Redis       │   │ SQLite        │               │
│  │ db=0     │   │ db=1        │   │ (NEW)         │               │
│  └──────────┘   └─────────────┘   └───────────────┘               │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                    Network Layer (NEW)                        │  │
│  │  WebSocket client ←→ AQM Relay Server ←→ WebSocket client   │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTPS + WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        AQM SERVER                                   │
│                                                                     │
│  ┌──────────────────┐   ┌───────────────────┐                      │
│  │ Coin Directory   │   │ Message Relay     │                      │
│  │ (PostgreSQL)     │   │ (WebSocket Hub)   │                      │
│  │ Upload/Fetch     │   │ Store & Forward   │                      │
│  │ Delete-on-Fetch  │   │ (NEW)             │                      │
│  └──────────────────┘   └───────────────────┘                      │
│                                                                     │
│  ┌──────────────────┐                                              │
│  │ FastAPI          │  ← existing endpoints + new relay routes      │
│  └──────────────────┘                                              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Tech Stack

### Keep
| Component | Tech | Status |
|-----------|------|--------|
| Private key vault | Redis 7 | ✅ Done |
| Public key inventory | Redis 7 | ✅ Done |
| Server coin directory | PostgreSQL 16 | ✅ Done |
| Server API | FastAPI + uvicorn | ✅ Done |
| Bridge (Redis ↔ PG) | asyncpg + redis-py | ✅ Done |
| Container orchestration | Docker Compose | ✅ Done |
| Testing | pytest + pytest-asyncio + fakeredis | ✅ Done |

### Add
| Component | Tech | Why |
|-----------|------|-----|
| **Contacts DB** | SQLite3 (stdlib) | Lightweight, file-based, perfect for per-device contact storage. No new dependencies. |
| **Real PQC crypto** | `liboqs-python` (pip) | Real Kyber-768 KEM + Dilithium-3 signatures. No mocks. |
| **Classical crypto** | `PyNaCl` (already installed) | X25519 DH, Ed25519 signing, NaCl SecretBox AEAD |
| **Session ratchet** | `cryptography` (pip) | HKDF-SHA256 for key derivation chain |
| **Chat networking** | `websockets` (pip) | Async WebSocket for real-time message relay (LAN/WAN) |
| **Chat frontend** | React (Vite) or Flask + HTMX | Web-based chat UI accessible from browser |
| **Message serialization** | `msgpack` or JSON | Compact binary or standard JSON for parcel format |
| **Device sensor mock** | `psutil` (pip) | Real battery % on laptops, mock signal for dev |

### Explicit: NO Mock Crypto
```
⛔ NEVER use os.urandom() as a substitute for real key generation
⛔ NEVER use random padding to simulate Dilithium signature sizes
⛔ NEVER fall back to "mock keygen" if liboqs is not installed
⛔ NEVER use SHA-256(public_key) as a symmetric key derivation shortcut

✅ ALWAYS use oqs.KeyEncapsulation("Kyber768") for KEM
✅ ALWAYS use oqs.Signature("Dilithium3") for signing
✅ ALWAYS use NaCl SecretBox with KEM-derived shared secret
✅ ALWAYS use HKDF for ratchet key derivation
✅ If liboqs is not installed, FAIL LOUDLY with ImportError — do not silently degrade
```

### Install Commands
```bash
# Add to conda environment
pip install liboqs-python --break-system-packages
pip install websockets msgpack psutil cryptography --break-system-packages

# Verify liboqs works
python -c "import oqs; kem = oqs.KeyEncapsulation('Kyber768'); print('Kyber OK:', len(kem.generate_keypair()))"
python -c "import oqs; sig = oqs.Signature('Dilithium3'); print('Dilithium OK:', len(sig.generate_keypair()))"
```

---

## 3. Module-by-Module Build Plan

### Phase 0: Cleanup (Day 1)

```
ACTION: Delete chat/ folder entirely
ACTION: Delete demo.py references to --chat, --demo-pair, --chat-bench
ACTION: Strip crypto_engine.py of ALL mock/fallback paths
ACTION: Verify 70 Redis tests + 37 server tests still pass after cleanup
```

Do NOT touch: vault.py, inventory.py, gc.py, coin_inventory.py, bridge.py, api.py

---

### Phase 1: Real Crypto Engine (Days 2-4)

**File: `aqm_shared/crypto_engine.py` — REWRITE**

The current crypto engine has three backends: liboqs+pynacl → pynacl-only → urandom-mock. Delete backends 2 and 3. Only real crypto.

```python
# What the rewritten crypto_engine.py must provide:

class CryptoEngine:
    """
    All PQC operations. No mocks. No fallbacks.
    Fails loudly if liboqs is not installed.
    """

    # ── Key Generation ──

    def generate_keypair_gold() -> tuple[bytes, bytes]:
        """
        Kyber-768 KEM keypair.
        Returns (public_key: 1184 bytes, secret_key: 2400 bytes)
        Uses: oqs.KeyEncapsulation("Kyber768")
        """

    def generate_keypair_silver() -> tuple[bytes, bytes]:
        """
        Same as Gold for KEM: Kyber-768 keypair.
        Difference is in signing (Ed25519 instead of Dilithium).
        Returns (public_key: 1184 bytes, secret_key: 2400 bytes)
        """

    def generate_keypair_bronze() -> tuple[bytes, bytes]:
        """
        X25519 DH keypair.
        Returns (public_key: 32 bytes, secret_key: 32 bytes)
        Uses: nacl.public.PrivateKey.generate()
        """

    # ── Signing ──

    def sign_dilithium(data: bytes, signing_key: bytes) -> bytes:
        """
        Dilithium-3 signature.
        Returns signature: ~3293 bytes (variable)
        Uses: oqs.Signature("Dilithium3")
        """

    def verify_dilithium(data: bytes, signature: bytes, verify_key: bytes) -> bool:
        """Dilithium-3 verify. Returns True/False."""

    def sign_ed25519(data: bytes, signing_key: bytes) -> bytes:
        """
        Ed25519 signature.
        Returns signature: 64 bytes
        Uses: nacl.signing.SigningKey
        """

    def verify_ed25519(data: bytes, signature: bytes, verify_key: bytes) -> bool:
        """Ed25519 verify. Returns True/False."""

    # ── KEM (Key Encapsulation) ──

    def kem_encapsulate(public_key: bytes) -> tuple[bytes, bytes]:
        """
        Kyber-768 encapsulation.
        Returns (ciphertext: 1088 bytes, shared_secret: 32 bytes)
        The shared_secret is the symmetric key for AES-GCM.
        Uses: oqs.KeyEncapsulation("Kyber768")
        """

    def kem_decapsulate(ciphertext: bytes, secret_key: bytes) -> bytes:
        """
        Kyber-768 decapsulation.
        Returns shared_secret: 32 bytes
        """

    # ── DH Key Exchange (Bronze) ──

    def dh_exchange(my_secret: bytes, their_public: bytes) -> bytes:
        """
        X25519 Diffie-Hellman.
        Returns shared_secret: 32 bytes
        Uses: nacl.public.Box or nacl.bindings.crypto_scalarmult
        """

    # ── Symmetric Encryption ──

    def encrypt_aead(plaintext: bytes, key: bytes, aad: bytes = b"") -> bytes:
        """
        AES-256-GCM encryption.
        Returns: nonce (12 bytes) || ciphertext || tag (16 bytes)
        Uses: cryptography.hazmat.primitives.ciphers.aead.AESGCM
        """

    def decrypt_aead(ciphertext_blob: bytes, key: bytes, aad: bytes = b"") -> bytes:
        """
        AES-256-GCM decryption.
        Input: nonce (12 bytes) || ciphertext || tag (16 bytes)
        Returns: plaintext
        Raises: InvalidTag on tamper
        """

    # ── Minting (combines keygen + signing) ──

    def mint_coin(tier: str) -> MintedCoinBundle:
        """
        Full coin minting workflow:
        1. Generate KEM/DH keypair based on tier
        2. Generate signing keypair based on tier (Dilithium for GOLD, Ed25519 for SILVER/BRONZE)
        3. Sign the public key with signing key
        4. Return MintedCoinBundle containing all components

        GOLD:   Kyber-768 KEM + Dilithium-3 signing
        SILVER: Kyber-768 KEM + Ed25519 signing
        BRONZE: X25519 DH + Ed25519 signing
        """
```

**Testing approach:**
- Test real key sizes (Kyber pk = 1184B, Dilithium sig = variable ~3293B)
- Test encapsulate → decapsulate roundtrip produces same shared secret
- Test sign → verify roundtrip
- Test tampered signature/ciphertext fails verification
- Test encrypt → decrypt AEAD roundtrip
- NO mocks in tests — all tests use real crypto

---

### Phase 2: Contacts Database (Days 5-7)

**NEW module: `aqm_contacts/`**

This does NOT exist yet. The current system hardcodes contact priorities in the demo. The MVP needs a persistent contacts database.

```
aqm_contacts/
├── __init__.py
├── contacts_db.py         — ContactsDatabase class (SQLite)
├── models.py              — Contact dataclass
└── tests/
    ├── conftest.py
    └── test_contacts.py
```

**Schema (SQLite):**
```sql
CREATE TABLE contacts (
    contact_id    TEXT PRIMARY KEY,          -- UUID string
    display_name  TEXT NOT NULL,
    priority      TEXT NOT NULL DEFAULT 'STRANGER'
                  CHECK (priority IN ('BESTIE', 'MATE', 'STRANGER')),
    public_signing_key  BLOB,               -- their long-term identity key (Dilithium/Ed25519)
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_msg_at   TIMESTAMP,
    msg_count     INTEGER DEFAULT 0,
    is_blocked    BOOLEAN DEFAULT 0
);

CREATE INDEX idx_priority ON contacts (priority);
CREATE INDEX idx_last_msg ON contacts (last_msg_at);
```

**Why SQLite, not Redis?**
Contacts are relational, persistent, and queried by multiple fields. Redis is for hot key caches. SQLite is for structured data that survives reboots.

**Class: ContactsDatabase**
```
__init__(db_path: str = "~/.aqm/contacts.db")
    Opens or creates SQLite file. Runs CREATE TABLE IF NOT EXISTS.

add_contact(contact_id, display_name, priority='STRANGER', signing_key=None) -> Contact
    Idempotent — ON CONFLICT DO UPDATE on display_name/priority.

remove_contact(contact_id) -> bool
    Hard delete. Also triggers GarbageCollector + inventory cleanup.

set_priority(contact_id, priority) -> Contact
    Updates priority. If downgraded (BESTIE→MATE), triggers SmartInventory._trim_excess.

get_contact(contact_id) -> Contact | None

get_contacts_by_priority(priority) -> list[Contact]
    Returns all contacts of a given priority.

get_all_contacts() -> list[Contact]

record_message(contact_id) -> None
    Increments msg_count, updates last_msg_at.
    Auto-promotes: if msg_count crosses threshold, suggest priority upgrade.

get_inactive_contacts(days: int = 30) -> list[Contact]
    For garbage collection — contacts with last_msg_at older than threshold.

block_contact(contact_id) -> None
    Sets is_blocked=True. Triggers inventory cleanup for this contact.

search_contacts(query: str) -> list[Contact]
    LIKE search on display_name.
```

**Integration points:**
- `bridge.fetch_and_cache()` reads priority from ContactsDB to determine budget caps
- `GarbageCollector` uses `get_inactive_contacts()` to find cleanup candidates
- Chat UI displays contact list from ContactsDB
- Adding a new contact triggers initial key fetch from server

---

### Phase 3: Session Ratchet (Days 8-10)

**NEW module: `aqm_session/`**

One coin establishes a master secret. The next 250 messages for Gold Coin, 150 messages for Silver Coin and 75 messages for Bronze Coin derive AES-256 keys from it using HKDF ratchet. After respective messages, consume a new coin.

```
aqm_session/
├── __init__.py
├── ratchet.py             — SessionRatchet class
├── session_store.py       — Persists ratchet state (SQLite or Redis hash)
└── tests/
    ├── conftest.py
    └── test_ratchet.py
```

**Class: SessionRatchet**
```
__init__(contact_id: str, master_secret: bytes)
    Initializes chain from master secret.
    msg_counter = 0
    current_chain_key = HKDF(master_secret, info=b"aqm-chain-init")

derive_message_key() -> bytes:
    """
    Derives next 32-byte AES key from chain.
    Uses HKDF-SHA256:
        message_key = HKDF(current_chain_key, info=b"aqm-msg-" + counter_bytes)
        current_chain_key = HKDF(current_chain_key, info=b"aqm-chain-advance")
    Increments msg_counter.
    Returns message_key.
    """

needs_rekey() -> bool:
    return self.msg_counter >= 100

rekey(new_master_secret: bytes) -> None:
    """
    Consume a new coin's shared secret.
    Reset counter to 0.
    Derive new chain key from new master secret.
    """

get_state() -> dict:
    """Serialize for persistence."""

@classmethod
from_state(state: dict) -> SessionRatchet:
    """Restore from persistence."""
```

**HKDF usage (from `cryptography` library):**
```python
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

def _hkdf_derive(key_material: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=None,
        info=info,
    ).derive(key_material)
```

**Critical:** After 100 messages, `needs_rekey()` returns True. The application layer must then:
1. Call `SmartInventory.select_coin()` to get a new coin
2. Run KEM encapsulate/decapsulate to get a new shared secret
3. Call `ratchet.rekey(new_shared_secret)`
4. Burn the old coin

---

### Phase 4: Network Layer (Days 11-15)

**NEW module: `aqm_network/`**

Replace Redis pub/sub with real WebSocket networking. Messages travel over LAN or WAN.

```
aqm_network/
├── __init__.py
├── relay_server.py        — WebSocket relay hub (server-side)
├── client.py              — WebSocket client (device-side)
├── protocol.py            — Message framing, serialization
└── tests/
    ├── conftest.py
    └── test_network.py
```

**Architecture:**
```
Alice's Device                    AQM Server                    Bob's Device
     │                                │                              │
     │──── WS connect ───────────────▶│◀──── WS connect ─────────── │
     │                                │                              │
     │  encrypted parcel (binary)     │                              │
     │───────────────────────────────▶│  store in mailbox            │
     │                                │  (if Bob offline)            │
     │                                │                              │
     │                                │  forward parcel              │
     │                                │─────────────────────────────▶│
     │                                │                              │
```

**Server: `relay_server.py`**
```
class RelayServer:
    """
    WebSocket hub. Routes encrypted parcels between users.
    CANNOT decrypt anything — just routes by recipient_id.
    """

    __init__(host, port)

    # Connection management
    handle_connection(websocket) -> None
        On connect: client sends {"type": "auth", "user_id": "alice"}
        Server registers websocket in connected_clients dict.

    # Message routing
    route_parcel(sender_id, recipient_id, encrypted_parcel) -> None
        If recipient online: forward immediately via WebSocket.
        If recipient offline: store in mailbox (PostgreSQL or Redis list).

    # Mailbox (store-and-forward)
    store_parcel(recipient_id, encrypted_parcel) -> None
        INSERT into mailbox table. Parcels wait until recipient reconnects.

    deliver_pending(user_id, websocket) -> None
        On reconnect: flush all pending parcels to user.

    # Integration with existing server
    # Mount alongside existing FastAPI app:
    #   /v1/coins/upload   ← existing
    #   /v1/coins/fetch    ← existing
    #   /ws                ← NEW: WebSocket relay endpoint
```

**Client: `client.py`**
```
class AQMClient:
    """
    Device-side network client. Connects to relay server.
    Handles sending/receiving encrypted parcels.
    """

    __init__(server_url: str, user_id: str)

    connect() -> None
        WebSocket connect + auth handshake.

    send_parcel(recipient_id: str, parcel: bytes) -> None
        Frame and send over WebSocket.

    on_message(callback: Callable) -> None
        Register callback for incoming parcels.

    disconnect() -> None

    # Reconnection with exponential backoff
    # Automatic mailbox flush on reconnect
```

**Parcel format (binary, msgpack or JSON):**
```python
@dataclass
class Parcel:
    version: int              # protocol version (1)
    sender_id: str            # Alice's UUID
    recipient_id: str         # Bob's UUID
    coin_id: str              # which coin was used (Bob looks up private key)
    coin_tier: str            # GOLD/SILVER/BRONZE
    kem_ciphertext: bytes     # Kyber ciphertext (GOLD/SILVER) or X25519 ephemeral pk (BRONZE)
    encrypted_payload: bytes  # AES-GCM(message) — nonce || ciphertext || tag
    signature: bytes          # sender's signature over the parcel
    timestamp: float
```

**Mailbox table (add to PostgreSQL migrations):**
```sql
CREATE TABLE mailbox (
    id          BIGSERIAL PRIMARY KEY,
    recipient_id UUID NOT NULL,
    sender_id    UUID NOT NULL,
    parcel_blob  BYTEA NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    delivered    BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_mailbox_recipient
    ON mailbox (recipient_id, created_at ASC)
    WHERE delivered = FALSE;
```

---

### Phase 5: Chat Frontend (Days 16-22)

**NEW module: `aqm_chat_ui/`**

Two options — pick one:

**Option A: Flask + HTMX (simpler, Python-only)**
```
aqm_chat_ui/
├── app.py                 — Flask app
├── templates/
│   ├── base.html
│   ├── chat.html          — main chat view
│   ├── contacts.html      — contact list + management
│   └── settings.html      — priority management, device info
├── static/
│   ├── style.css
│   └── app.js             — WebSocket client JS, HTMX config
└── tests/
```
Good for: rapid prototyping, demo day, single-page feel with HTMX live updates.

**Option B: React + Vite (richer, separate frontend)**
```
aqm_chat_ui/
├── src/
│   ├── App.jsx
│   ├── components/
│   │   ├── ChatWindow.jsx
│   │   ├── MessageBubble.jsx
│   │   ├── ContactList.jsx
│   │   ├── ContactCard.jsx
│   │   ├── CoinStatus.jsx     — shows [G:5 S:4 B:1] remaining
│   │   ├── DeviceContext.jsx   — shows battery/signal/tier
│   │   └── TierBadge.jsx      — gold/silver/bronze pill badge
│   ├── hooks/
│   │   └── useWebSocket.js
│   └── api/
│       └── aqm.js             — REST calls to FastAPI backend
├── package.json
└── vite.config.js
```
Good for: polished demo, richer interactivity, familiar stack.

**Recommendation: Option A (Flask + HTMX)** for MVP speed. The terminal demo already works — you need a skin, not a SPA framework.

**Core UI features:**
```
1. Contact list sidebar
   - Shows all contacts grouped by priority (Bestie/Mate/Stranger)
   - Add new contact (UUID + display name)
   - Change priority (dropdown)
   - Coin inventory indicator per contact

2. Chat window
   - Message bubbles (sent/received)
   - Each message shows: tier badge (gold/silver/bronze), timestamp
   - Coin counter in header: [G:5 S:4 B:1]
   - Device context indicator: battery %, WiFi status, selected tier

3. Real-time updates
   - WebSocket connection to relay server
   - Incoming messages appear instantly
   - Coin counter decrements on send
   - Tier badge changes color based on context shifts

4. Status bar
   - Connection status (connected/disconnected/reconnecting)
   - Current device context (battery/WiFi/signal)
   - Auto-selected tier with explanation
```

---

### Phase 6: Application Orchestrator (Days 23-25)

**File: `aqm_app/orchestrator.py`**

This is the brain. It wires everything together.

```python
class AQMApp:
    """
    Main application class. Owns all subsystems.
    Called by the Chat UI or CLI.
    """

    def __init__(self, user_id, server_url):
        self.user_id = user_id
        self.vault = SecureVault(create_vault_client())
        self.inventory = SmartInventory(create_inventory_client())
        self.contacts = ContactsDatabase()
        self.server = CoinInventoryServer(pool)
        self.network = AQMClient(server_url, user_id)
        self.crypto = CryptoEngine()
        self.context = ContextManager()
        self.sessions = {}  # contact_id -> SessionRatchet

    async def mint_coins(self):
        """Mint 5G+6S+5B coins. Store private in vault, upload public to server."""

    async def provision_contact(self, contact_id, priority):
        """Register contact, fetch their public keys based on priority budget."""

    async def send_message(self, contact_id, plaintext: str):
        """
        1. Get device context → select tier
        2. Get or create SessionRatchet for contact
        3. If ratchet.needs_rekey(): consume new coin from inventory, run KEM
        4. Else: derive next message key from ratchet
        5. Encrypt plaintext with message key (AES-GCM)
        6. Build Parcel with kem_ciphertext (if new coin) or empty (if ratchet)
        7. Sign parcel
        8. Send via network.send_parcel()
        9. contacts.record_message(contact_id)
        """

    async def receive_message(self, parcel: Parcel):
        """
        1. Verify sender signature
        2. If parcel has kem_ciphertext: decapsulate with vault private key, init new ratchet
        3. Else: derive message key from existing ratchet
        4. Decrypt payload
        5. Burn coin if new KEM exchange
        6. Return plaintext
        """

    async def add_contact(self, contact_id, display_name, priority):
        """Add to ContactsDB + provision keys from server."""

    async def get_device_context(self) -> DeviceContext:
        """Read real battery/WiFi/signal from psutil or system APIs."""
```

---

## 4. Implementation Order

```
Week 1 (Days 1-7):     Foundation
  Day 1:    Delete chat/, clean up demo.py
  Day 2-4:  Rewrite crypto_engine.py with real liboqs
  Day 5-7:  Build aqm_contacts/ (SQLite contacts database)

Week 2 (Days 8-15):    Core Infrastructure
  Day 8-10:  Build aqm_session/ (HKDF ratchet, 100-msg window)
  Day 11-13: Build aqm_network/relay_server.py (WebSocket hub)
  Day 14-15: Build aqm_network/client.py (WebSocket client)

Week 3 (Days 16-22):   Frontend + Integration
  Day 16-18: Build Flask + HTMX chat UI (contact list + chat window)
  Day 19-20: Build orchestrator.py (wires everything)
  Day 21-22: WebSocket integration (live messages in UI)

Week 4 (Days 23-28):   Polish + Demo
  Day 23-24: Multi-scenario demo (WiFi → 2G → emergency transitions in UI)
  Day 25:    LAN test (two laptops, same network)
  Day 26:    WAN test (deploy server to cloud VM, test remotely)
  Day 27:    Load test, edge cases, error handling
  Day 28:    Record demo video, polish UI
```

---

## 5. New File Tree

```
AQM_Database/
├── aqm_shared/                    # Shared (EXISTING — crypto rewritten)
│   ├── config.py                  ✅ keep
│   ├── types.py                   ✅ keep + extend with Parcel, Contact
│   ├── errors.py                  ✅ keep + extend
│   ├── crypto_engine.py           🔄 REWRITE — real liboqs, zero mocks
│   ├── context_manager.py         🔄 UPDATE — real sensor hooks
│   └── tests/
│
├── aqm_db/                        # Redis layer (EXISTING — keep as-is)
│   ├── connection.py              ✅
│   ├── vault.py                   ✅
│   ├── inventory.py               ✅
│   ├── garbage_collector.py       ✅
│   ├── stats.py                   ✅
│   └── tests/                     ✅
│
├── aqm_server/                    # PostgreSQL layer (EXISTING — extend)
│   ├── config.py                  ✅
│   ├── db.py                      ✅
│   ├── coin_inventory.py          ✅
│   ├── api.py                     🔄 ADD WebSocket relay endpoint
│   ├── migrations/
│   │   ├── 001_create_coin_inventory.sql  ✅
│   │   └── 002_create_mailbox.sql         🆕 NEW
│   └── tests/                     ✅ + new relay tests
│
├── aqm_contacts/                  # 🆕 NEW — Contact management
│   ├── __init__.py
│   ├── contacts_db.py
│   ├── models.py
│   └── tests/
│       ├── conftest.py
│       └── test_contacts.py
│
├── aqm_session/                   # 🆕 NEW — Session ratchet
│   ├── __init__.py
│   ├── ratchet.py
│   ├── session_store.py
│   └── tests/
│       ├── conftest.py
│       └── test_ratchet.py
│
├── aqm_network/                   # 🆕 NEW — Real networking
│   ├── __init__.py
│   ├── relay_server.py
│   ├── client.py
│   ├── protocol.py
│   └── tests/
│       ├── conftest.py
│       └── test_network.py
│
├── aqm_chat_ui/                   # 🆕 NEW — Web-based chat frontend
│   ├── app.py                     # Flask app
│   ├── templates/
│   │   ├── base.html
│   │   ├── chat.html
│   │   └── contacts.html
│   ├── static/
│   │   ├── style.css
│   │   └── app.js
│   └── tests/
│
├── aqm_app/                       # 🆕 NEW — Application orchestrator
│   ├── __init__.py
│   └── orchestrator.py
│
├── bridge.py                      ✅ keep
├── prototype.py                   ✅ keep (still useful for headless demo)
├── docker-compose.yml             🔄 UPDATE — add any new services if needed
└── environment.yml                🔄 UPDATE — add new dependencies
```

---

## 6. Testing Strategy

```
Target: 250+ tests total

aqm_shared/tests/         ~25 tests  (real crypto roundtrips, context decisions)
aqm_db/tests/             ~70 tests  (existing, fakeredis)
aqm_server/tests/         ~40 tests  (existing + mailbox + relay)
aqm_contacts/tests/       ~15 tests  (SQLite CRUD, priority changes, search)
aqm_session/tests/        ~15 tests  (ratchet derivation, 100-msg boundary, rekey)
aqm_network/tests/        ~20 tests  (WebSocket connect, send, receive, offline mailbox)
aqm_chat_ui/tests/        ~10 tests  (Flask routes, template rendering)
integration/              ~15 tests  (full lifecycle: mint → fetch → send → receive → burn)

Test commands:
  pytest AQM_Database/ -v                          # all
  pytest AQM_Database/aqm_shared/tests/ -v         # crypto + context (no Docker)
  pytest AQM_Database/aqm_contacts/tests/ -v       # contacts (no Docker)
  pytest AQM_Database/aqm_session/tests/ -v        # ratchet (no Docker)
  pytest AQM_Database/aqm_network/tests/ -v        # network (needs Docker)
  pytest AQM_Database/aqm_server/tests/ -v         # server (needs Docker)
```

---

## 7. Claude Code Pairing Rules

```
PAIR PROGRAMMING MODE — ENFORCED

1. NEVER write a complete file without Bonny reviewing the approach first.
   - Present skeleton → discuss → fill in together.

2. ALWAYS explain WHY before HOW.
   - "We use HKDF here because..." before showing the code.

3. STOP and ask at every decision point:
   - "Flask or React?" — let Bonny choose.
   - "msgpack or JSON for parcels?" — discuss tradeoffs.
   - "SQLite file location?" — let Bonny decide.

4. TEST FIRST where possible.
   - Write the test, show it fails, then implement.

5. NO MOCK CRYPTO. EVER.
   - If liboqs import fails, stop and fix the installation.
   - Do not silently degrade to random bytes.

6. COMMIT OFTEN.
   - After each function works: git add + commit.
   - Commit messages: "feat(contacts): add ContactsDatabase.add_contact()"

7. ONE MODULE AT A TIME.
   - Finish crypto_engine → all tests pass → move to contacts.
   - Never context-switch to networking while ratchet is half-done.

8. PRESERVE EXISTING TESTS.
   - The 173 existing tests must keep passing throughout.
   - Run `pytest AQM_Database/aqm_db/tests/ -v` after any shared/ change.

9. EXPLAIN ERROR HANDLING.
   - Every try/except must have a reason discussed.

10. BONNY TYPES THE CODE.
    - Claude Code provides guidance, Bonny implements.
    - Claude reviews after Bonny writes.
```

---

## 8. Quick Reference: What Goes Where

| "I want to..." | Module | Key Function |
|----------------|--------|-------------|
| Generate a Kyber keypair | `aqm_shared/crypto_engine.py` | `generate_keypair_gold()` |
| Store a private key | `aqm_db/vault.py` | `vault.store_key()` |
| Cache a public key | `aqm_db/inventory.py` | `inventory.store_key()` |
| Upload public keys to server | `aqm_server/coin_inventory.py` | `server.upload_coins()` |
| Fetch keys from server | `bridge.py` | `fetch_and_cache()` |
| Add a contact | `aqm_contacts/contacts_db.py` | `contacts.add_contact()` |
| Derive next message key | `aqm_session/ratchet.py` | `ratchet.derive_message_key()` |
| Send encrypted message | `aqm_app/orchestrator.py` | `app.send_message()` |
| Receive + decrypt message | `aqm_app/orchestrator.py` | `app.receive_message()` |
| Route parcel between users | `aqm_network/relay_server.py` | `relay.route_parcel()` |
| Display chat UI | `aqm_chat_ui/app.py` | Flask routes |
| Read battery/WiFi | `aqm_shared/context_manager.py` | `context.get_device_context()` |
