# AQM — Amortized Quantum Messaging

A post-quantum secure messaging system that amortizes expensive key encapsulation operations across hundreds of messages using a symmetric ratchet. One ML-KEM-768 operation covers up to 250 messages — providing post-quantum resistance without per-message KEM overhead.

---

## How It Works

```
Message 1  → KEM encapsulate (ML-KEM-768) → establish ratchet → encrypt
Message 2  → derive ratchet key            → encrypt  (no KEM)
Message 3  → derive ratchet key            → encrypt  (no KEM)
...
Message 250 → ratchet exhausted → KEM encapsulate again → new ratchet
```

Each coin is a public/private keypair. The sender uses the receiver's public key once to establish a shared secret, then both sides derive message keys via HKDF without further KEM operations until the ratchet limit is reached.

---

## Coin Tiers

| Tier   | KEM Algorithm | Signature  | Ratchet Limit | Public Key Size |
|--------|--------------|------------|---------------|-----------------|
| GOLD   | ML-KEM-768   | ML-DSA-65  | 250 messages  | ~3.6 KB         |
| SILVER | ML-KEM-768   | Ed25519    | 150 messages  | ~1.2 KB         |
| BRONZE | X25519       | Ed25519    | 75 messages   | ~96 B           |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Flask Web UI                        │
│  (Per-user instance — Alice :5000, Bob :5001, ...)      │
└────────────────┬───────────────────┬────────────────────┘
                 │                   │
    ┌────────────▼──────┐   ┌────────▼──────────┐
    │   SecureVault     │   │  SmartInventory   │
    │  (Redis — local)  │   │  (Redis — local)  │
    │  Own private keys │   │  Partner pub keys │
    │  Burn on decrypt  │   │  Budget-capped    │
    └───────────────────┘   └────────┬──────────┘
                                     │ sync
                            ┌────────▼──────────┐
                            │  CoinInventory    │
                            │  Server (FastAPI) │
                            │  PostgreSQL 16    │
                            │  Delete-on-Fetch  │
                            └───────────────────┘
```

---

## Project Structure

```
AQM_Database/
├── flask_app/                     # Web UI (Flask + SSE)
│   ├── app.py                     # Per-user Flask server
│   ├── aqm_bridge.py              # Async-to-sync helper
│   └── templates/index.html       # SPA UI (real-time updates)
├── aqm_shared/                    # Shared types, errors, config, crypto
│   ├── config.py                  # Constants, budget caps, thresholds
│   ├── crypto_engine.py           # ML-KEM-768, ML-DSA-65, X25519, AEAD
│   ├── context_manager.py         # Device context → tier selection
│   ├── types.py                   # Shared dataclasses
│   └── errors.py                  # Exception hierarchy
├── aqm_db/                        # Redis layer (vault + inventory)
│   ├── vault.py                   # Private key storage
│   ├── inventory.py               # Partner public key cache
│   ├── connection.py              # Redis client factory
│   └── stats.py                   # Storage reporter
├── aqm_contacts/                  # SQLite contacts + auto-priority
│   ├── contacts_db.py             # Priority + message history
│   └── models.py                  # Contact dataclass
├── aqm_session/                   # HKDF ratchet + persistence
│   ├── ratchet.py                 # Ratchet implementation
│   └── session_store.py           # SQLite session store
├── aqm_server/                    # PostgreSQL coin server
│   ├── api.py                     # FastAPI endpoints
│   ├── coin_inventory.py          # Server-side coin registry
│   ├── db.py                      # Async connection pool
│   └── migrations/                # SQL schema
├── aqm_network/                   # WebSocket relay (protocol + client)
│   ├── protocol.py                # Message framing
│   ├── relay_server.py            # WebSocket hub
│   └── client.py                  # Device-side client
├── aqm_app/                       # Application orchestrator
│   └── orchestrator.py            # Wires all subsystems
├── bridge.py                      # upload_coins / sync_inventory
├── prototype.py                   # Headless lifecycle demo
├── requirements.txt               # Pip dependencies
├── environment.yml                # Conda environment spec
└── docker-compose.yml             # Redis 7 + PostgreSQL 16
```

---

## Quick Start — Running the Demo

### Prerequisites

**System dependencies:**

```bash
# Fedora / RHEL
sudo dnf install redis postgresql16-server liboqs-devel

# macOS
brew install redis postgresql@16 liboqs

# Start services
# Fedora:
sudo systemctl start redis postgresql
# macOS:
brew services start redis && brew services start postgresql@16
```

**Python environment:**

```bash
conda env create -f AQM_Database/environment.yml
conda activate aqm-db
pip install -r AQM_Database/requirements.txt
```

**Database setup (Docker — recommended):**

```bash
cd AQM_Database && docker compose up -d
# This starts Redis 7 (port 6379) + PostgreSQL 16 (port 5433)
# Migrations auto-run via /docker-entrypoint-initdb.d mount
```

### Running the Web UI Demo

**Step 1 — Reset all state (fresh demo):**

```bash
redis-cli FLUSHALL
PGPASSWORD=aqm_dev_password psql -h localhost -p 5433 -U aqm_user -d aqm -c "DELETE FROM coin_inventory;"
rm -f ~/.aqm/alice_contacts.db ~/.aqm/bob_contacts.db ~/.aqm/charlie_contacts.db
rm -f alice_sessions.db bob_sessions.db charlie_sessions.db
```

**Step 2 — Start the Coin Server (Terminal 1):**

```bash
conda activate aqm-db
uvicorn AQM_Database.aqm_server.api:app --host 0.0.0.0 --port 8000
```

**Step 3 — Start Flask instances (one per terminal):**

```bash
# Terminal 2 — Alice
conda activate aqm-db
python -m AQM_Database.flask_app.app --user alice --port 5000 \
  --contacts bob charlie --contact-ports 5001 5002

# Terminal 3 — Bob
conda activate aqm-db
python -m AQM_Database.flask_app.app --user bob --port 5001 \
  --contacts alice charlie --contact-ports 5000 5002

# Terminal 4 — Charlie
conda activate aqm-db
python -m AQM_Database.flask_app.app --user charlie --port 5002 \
  --contacts alice bob --contact-ports 5000 5001
```

**Step 4 — Open in browser:**

| User    | URL                    |
|---------|------------------------|
| Alice   | http://localhost:5000  |
| Bob     | http://localhost:5001  |
| Charlie | http://localhost:5002  |

### Running the Headless Demo (no UI)

```bash
python -m AQM_Database.prototype    # 4-phase lifecycle demo (mint → fetch → send → burn)
```

---

## Demo UI Features

- **Real-time SSE** — messages appear instantly without polling
- **Session Tier** — shows the tier currently securing the active ratchet session
- **Next Rekey Tier** — shows what tier the next coin would use based on current device context (updates every 8 seconds)
- **Ratchet progress bar** — shows how many messages remain before the next rekey
- **Vault burn counter** — tracks private keys destroyed (perfect forward secrecy)
- **Priority promotion bars** — live progress toward MATE and BESTIE thresholds
- **Per-contact coin inventory** — GOLD/SILVER/BRONZE counts on each contact card

---

## Context-Based Tier Selection

The device context simulator updates every 8 seconds with random values:

```
battery < 5%                    → BRONZE   (critical battery — conserve)
no WiFi + signal < -100 dBm    → BRONZE   (poor signal — small keys)
WiFi + battery < 20%           → BRONZE   (low battery — conserve)
no WiFi + signal >= -100 dBm   → SILVER   (decent cellular)
WiFi + 20% <= battery < 50%    → SILVER   (moderate conditions)
WiFi + battery >= 50%          → GOLD     (ideal conditions)
```

The selected tier is further capped by the contact's priority level:

| Priority | Ceiling | GOLD cap | SILVER cap | BRONZE cap |
|----------|---------|----------|------------|------------|
| STRANGER | BRONZE  | 0        | 0          | 5          |
| MATE     | SILVER  | 0        | 6          | 4          |
| BESTIE   | GOLD    | 5        | 4          | 1          |

Contacts are auto-promoted based on messaging frequency:
- **STRANGER → MATE**: 4 messages within 30 days
- **MATE → BESTIE**: 5 messages within 7 days

---

## Running Tests

```bash
# All tests (274 total — needs Docker for server tests)
pytest AQM_Database/ -v

# By subsystem (no Docker required)
pytest AQM_Database/aqm_shared/tests/ -v    # 48 tests — crypto + context
pytest AQM_Database/aqm_db/tests/ -v        # 70 tests — vault + inventory (fakeredis)
pytest AQM_Database/aqm_contacts/tests/ -v  # 28 tests — contacts (SQLite)
pytest AQM_Database/aqm_session/tests/ -v   # 35 tests — ratchet
pytest AQM_Database/aqm_network/tests/ -v   # 26 tests — network protocol
pytest AQM_Database/aqm_app/tests/ -v       # 30 tests — orchestrator

# Requires Docker (Redis + PostgreSQL)
pytest AQM_Database/aqm_server/tests/ -v    # 37 tests — coin server + bridge
```

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `liboqs-python` | ML-KEM-768 + ML-DSA-65 (post-quantum) |
| `PyNaCl` | X25519 + Ed25519 + AEAD (libsodium) |
| `cryptography` | HKDF key derivation |
| `flask` | Web UI server |
| `fastapi` + `uvicorn` | Coin inventory REST API |
| `asyncpg` | Async PostgreSQL driver |
| `redis-py` | Vault + inventory (Redis) |
| `pydantic` | Request/response validation |

See `AQM_Database/requirements.txt` for full version-pinned list.

---

## Key Design Decisions

**Why amortize KEM operations?** ML-KEM-768 key generation and encapsulation are computationally expensive compared to classical ECDH. The ratchet amortizes this cost to approximately one KEM per 250 messages for GOLD tier.

**Why burn private keys?** Each private key is used exactly once and then deleted. This provides perfect forward secrecy — even if an attacker later compromises the device, past messages cannot be decrypted because the keys no longer exist.

**Why three tiers?** Different device conditions (battery, network quality) warrant different security/efficiency tradeoffs. A device on WiFi with full battery can afford ML-KEM-768; a device with 3% battery in a tunnel should use X25519.

**Why priority-based caps?** GOLD coins (scarce, expensive to generate) are reserved for trusted contacts who you communicate with frequently.

## Troubleshooting

**Inventory shows 0 coins after start:** Partner Flask instance must be started first so their coins are on the server. Background sync retries every 10 seconds automatically.

**`liboqs` version mismatch warning:** Cosmetic only. To fix: `pip install liboqs-python==0.15.0`.

**Priority shows BESTIE on fresh start:** SQLite was not fully deleted. Run the reset commands above.
