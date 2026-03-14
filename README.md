# AQM — Amortized Quantum Messaging

A post-quantum secure messaging system that amortizes expensive key encapsulation operations across hundreds of messages using a symmetric ratchet. One ML-KEM-768 operation covers up to 250 messages — providing post-quantum resistance without per-message KEM overhead.

---

## Live Deployment

The chat is running at [cryptindo-aqm.org](https://cryptindo-aqm.org) on a DigitalOcean droplet with Caddy reverse proxy and auto-provisioned Let's Encrypt TLS.

| User | URL | Password |
|------|-----|----------|
| biprarshi | https://biprarshi.cryptindo-aqm.org | `biprarshi` |
| protyasha | https://protyasha.cryptindo-aqm.org | `protyasha` |
| shirsa | https://shirsa.cryptindo-aqm.org | `shirsa` |
| subhamoy | https://subhamoy.cryptindo-aqm.org | `subhamoy` |
| samrat | https://samrat.cryptindo-aqm.org | `samrat` |
| padma | https://padma.cryptindo-aqm.org | `padma` |
| shreejith | https://shreejith.cryptindo-aqm.org | `shreejith` |

Open your URL in a browser, enter your password, and start messaging.

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

| Tier | KEM Algorithm | Signature | Ratchet Limit | Public Key Size |
|------|---------------|-----------|---------------|-----------------|
| GOLD | ML-KEM-768 | ML-DSA-65 | 250 messages | ~3.6 KB |
| SILVER | ML-KEM-768 | Ed25519 | 150 messages | ~1.2 KB |
| BRONZE | X25519 | Ed25519 | 75 messages | ~96 B |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Flask Web UI                        │
│  (Per-user instance — one container per user)           │
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
│   └── templates/
│       ├── index.html             # SPA dashboard (real-time updates)
│       └── login.html             # Login page
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
deploy.py                          # Dynamic N-user deploy script
Dockerfile                         # Production container image
docker-compose.prod.yml            # Production compose (generated by deploy.py)
```

---

## Deploying Your Own Instance

### Prerequisites

- A Linux server with Docker and Docker Compose installed
- SSH access to the server
- A domain name (for HTTPS — Caddy auto-provisions Let's Encrypt certificates)
- This repo cloned on the server

### Step 1 — Point DNS to your server

Create A records for each user subdomain pointing to your server IP:

```
alice.yourdomain.com   → YOUR_SERVER_IP
bob.yourdomain.com     → YOUR_SERVER_IP
charlie.yourdomain.com → YOUR_SERVER_IP
```

### Step 2 — Generate config with `deploy.py`

```bash
# With domain (recommended — adds Caddy reverse proxy + auto TLS):
python deploy.py --users alice:secret1 bob:secret2 charlie:secret3 --domain yourdomain.com

# Without domain (bare IP, no HTTPS):
python deploy.py --users alice:secret1 bob:secret2 charlie:secret3

# Generate + immediately start:
python deploy.py --users alice:secret1 bob:secret2 --domain yourdomain.com --up
```

This creates:
- **`.env`** — auto-generated DB password, Flask secrets, and your user passwords
- **`docker-compose.prod.yml`** — Redis, PostgreSQL, coin-server, one container per user, and Caddy
- **`Caddyfile`** (with `--domain`) — per-user subdomain reverse proxy with auto HTTPS

### Step 3 — Start

```bash
ssh root@YOUR_SERVER
cd /path/to/repo
docker compose -f docker-compose.prod.yml up -d --build
```

### Step 4 — Verify

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Each user visits `https://username.yourdomain.com` and logs in with their password. Caddy handles TLS automatically.

### Redeploying with different users

```bash
docker compose -f docker-compose.prod.yml down
docker volume rm aqm_databse_pg_data    # reset DB if password changed
python deploy.py --users ... --domain yourdomain.com --up
```

---

## Local Development

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

### Running locally

**Start the Coin Server (Terminal 1):**

```bash
uvicorn AQM_Database.aqm_server.api:app --host 0.0.0.0 --port 8000
```

**Start Flask instances (one per terminal):**

```bash
# Terminal 2
python -m AQM_Database.flask_app.app --user alice --port 5000 \
  --contacts bob --contact-ports 5001 --password mypassword

# Terminal 3
python -m AQM_Database.flask_app.app --user bob --port 5001 \
  --contacts alice --contact-ports 5000 --password mypassword
```

Open `http://localhost:5000` and `http://localhost:5001` in your browser.

Default password (if `--password` is not set): `aqm-demo-2026`. Can also be set via `AQM_PASSWORD` env var.

### Running the Headless Demo (no UI)

```bash
python -m AQM_Database.prototype    # 4-phase lifecycle demo (mint → fetch → send → burn)
```

---

## Demo UI Features

| Feature | Description |
|---------|-------------|
| Login gate | Password-protected per-user instances |
| Real-time SSE | Messages appear instantly without polling |
| PQ indicator dot | Green = GOLD/PQ, Yellow = SILVER/Hybrid, Red = BRONZE/Classical |
| Session Tier | Shows the tier currently securing the active ratchet session |
| Next Rekey Tier | Shows what tier the next coin would use based on device context (updates every 20s) |
| Ratchet progress bar | Messages remaining before the next rekey |
| Background minting | Auto-mints new coins when device is ideal and vault is low (120s cooldown) |
| Vault burn counter | Tracks private keys destroyed (perfect forward secrecy) |
| Priority promotion bars | Live progress toward MATE and BESTIE thresholds |
| Per-contact coin inventory | GOLD/SILVER/BRONZE counts on each contact card |

---

## Context-Based Tier Selection

The device context simulator updates every 20 seconds with random values:

| Condition | Tier Selected |
|-----------|---------------|
| battery < 5% | BRONZE (critical battery — conserve) |
| no WiFi + signal < -100 dBm | BRONZE (poor signal — small keys) |
| WiFi + battery < 20% | BRONZE (low battery — conserve) |
| no WiFi + signal >= -100 dBm | SILVER (decent cellular) |
| WiFi + 20% <= battery < 50% | SILVER (moderate conditions) |
| WiFi + battery >= 50% | GOLD (ideal conditions) |

The selected tier is further capped by the contact's priority level:

| Priority | Ceiling | GOLD cap | SILVER cap | BRONZE cap |
|----------|---------|----------|------------|------------|
| STRANGER | BRONZE | 0 | 0 | 5 |
| MATE | SILVER | 0 | 6 | 4 |
| BESTIE | GOLD | 5 | 4 | 1 |

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

**DB password authentication failed after redeploying:** The PostgreSQL volume has the old password baked in. Run `docker volume rm aqm_databse_pg_data` then restart.
