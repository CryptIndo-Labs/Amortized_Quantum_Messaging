# AQM — Amortized Quantum Messaging

A post-quantum secure messaging system that amortizes expensive key encapsulation operations across hundreds of messages using a symmetric ratchet. One ML-KEM-768 operation covers up to 250 messages — providing post-quantum resistance without per-message KEM overhead.

Supports both **1-to-1 direct messaging** and **end-to-end encrypted group chat** using a Categorical B-Tree key distribution scheme with Hot Edge session caching.

---

## Live Deployment

The chat is running at [cryptindo-aqm.org](https://cryptindo-aqm.org) on a DigitalOcean droplet with Caddy reverse proxy and auto-provisioned Let's Encrypt TLS.

| User | URL | Password |
|------|-----|----------|
| biprarshi | https://biprarshi.cryptindo-aqm.org | `biprarshi` |
| protyasha | https://protyasha.cryptindo-aqm.org | `protyasha` |
| shirsa | https://shirsa.cryptindo-aqm.org | `shirsa` |

Open your URL in a browser, enter your password, and start messaging. Direct chat and group chat are available as tabs in the UI.

---

## How It Works

### Direct Messaging

```
Message 1  → KEM encapsulate (ML-KEM-768) → establish ratchet → encrypt
Message 2  → derive ratchet key            → encrypt  (no KEM)
Message 3  → derive ratchet key            → encrypt  (no KEM)
...
Message 250 → ratchet exhausted → KEM encapsulate again → new ratchet
```

Each coin is a public/private keypair. The sender uses the receiver's public key once to establish a shared secret, then both sides derive message keys via HKDF without further KEM operations until the ratchet limit is reached.

### Group Chat — Categorical B-Tree

Group messages use a hierarchical key tree that distributes a single symmetric message key to all members while respecting per-member trust tiers:

```
                    ┌──────────────┐
                    │   Root Key   │  (random per-message)
                    └──────┬───────┘
               ┌───────────┼───────────┐
          ┌────▼────┐ ┌────▼────┐ ┌────▼────┐
          │  GOLD   │ │ SILVER  │ │ BRONZE  │   Branch Keys
          │ Branch  │ │ Branch  │ │ Branch  │   (one per tier)
          └────┬────┘ └────┬────┘ └────┬────┘
            ┌──▼──┐     ┌──▼──┐     ┌──▼──┐
            │Leaf │     │Leaf │     │Leaf │     Per-member KEM
            │Alice│     │ Bob │     │Carol│     (or HOT edge)
            └─────┘     └─────┘     └─────┘
```

The sender builds the tree, encrypts the root key down through branch keys to per-member leaves, and sends the resulting **group parcel** to the server. The server fans out to each recipient. Each recipient decrypts upward from their leaf to recover the root key and decrypt the message.

**Hot Edge optimization**: after the first COLD (KEM-based) exchange with a member, a `SessionRatchet` is activated for that edge. Subsequent messages within a 10-minute window use the ratchet-derived key directly — bypassing the KEM leaf entirely. After 10 minutes of silence, the edge returns to COLD and the ephemeral chain key is burned.

---

## Coin Tiers

| Tier | KEM Algorithm | Signature | Ratchet Limit | Public Key Size |
|------|---------------|-----------|---------------|-----------------|
| GOLD | ML-KEM-768 | ML-DSA-65 | 250 messages | ~3.6 KB |
| SILVER | ML-KEM-768 | Ed25519 | 150 messages | ~1.2 KB |
| BRONZE | X25519 | Ed25519 | 75 messages | ~96 B |

In group chat, each COLD leaf consumes one coin of the appropriate tier. HOT edges consume zero coins.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         Flask Web UI                             │
│  (Per-user instance — one container per user)                    │
│  Direct Chat (1-to-1)  │  Group Chat (N-way, B-Tree encrypted)  │
└────────┬───────────┬────┴──────┬───────────────┬─────────────────┘
         │           │           │               │
┌────────▼──────┐ ┌──▼──────────▼──┐ ┌──────────▼───────────────┐
│  SecureVault  │ │ SmartInventory  │ │  Contacts DB + Group DB  │
│ (Redis—local) │ │ (Redis—local)   │ │  (SQLite—local)          │
│ Own priv keys │ │ Partner pubs    │ │  Priority / history /    │
│ Burn on use   │ │ Budget-capped   │ │  groups / members /      │
└───────────────┘ └──────┬──────────┘ │  hot_edges / messages    │
                         │ sync       └────────────────────────────┘
                ┌────────▼──────────┐
                │  CoinInventory    │
                │  Server (FastAPI) │
                │  PostgreSQL 16    │
                │  Delete-on-Fetch  │
                └───────────────────┘
```

### Group Chat Module Map

| Concept (from PDF spec) | Code Class / File |
|--------------------------|-------------------|
| Categorical B-Tree | `GroupKeyTree` (`key_tree.py`) |
| Level 0: Root Key | `TreeBuildResult.root_key` |
| Level 1: Branch Keys | `BranchResult.branch_key` (per tier) |
| Level 2: Leaves | `LeafResult` (per member) |
| Hot Edge State Machine | `HotEdgeTracker` (`hot_edge.py`) |
| Blind Star Graph Routing | `RelayServer.handle_group_parcel()` (`relay_server.py`) |
| Group Parcel Wire Format | `build_parcel` / `parse_parcel` (`group_parcel.py`) |
| Orchestrator (send/recv) | `GroupOrchestrator` (`group_orchestrator.py`) |
| Client-side Persistence | `GroupDatabase` (`group_db.py`) |
| Server-side Routing | `003_group_and_mailbox_extension.sql` |
| Flask UI | `group_routes.py` + `templates/group.html` |

---

## Project Structure

```
AQM_Database/
├── flask_app/                     # Web UI (Flask + SSE)
│   ├── app.py                     # Per-user Flask server
│   ├── aqm_bridge.py              # Async-to-sync helper
│   ├── group_routes.py            # Group chat Flask blueprint
│   └── templates/
│       ├── index.html             # SPA dashboard (direct + group tabs)
│       ├── group.html             # Group chat template
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
├── aqm_group/                     # Group chat subsystem
│   ├── key_tree.py                # Categorical B-Tree (build/decrypt)
│   ├── hot_edge.py                # Hot Edge state machine + TTL
│   ├── group_parcel.py            # Parcel wire format (build/parse)
│   ├── group_orchestrator.py      # Send/receive orchestration
│   ├── group_db.py                # SQLite: groups, members, hot_edges, messages
│   ├── group_types.py             # Dataclasses (header, inner, edge state, etc.)
│   └── tests/                     # 70 tests (key_tree, hot_edge, parcel, orchestrator)
├── aqm_server/                    # PostgreSQL coin server
│   ├── api.py                     # FastAPI endpoints
│   ├── coin_inventory.py          # Server-side coin registry
│   ├── db.py                      # Async connection pool
│   └── migrations/                # SQL schema (incl. 003_group_and_mailbox_extension)
├── aqm_network/                   # WebSocket relay (protocol + client)
│   ├── protocol.py                # Message framing
│   ├── relay_server.py            # WebSocket hub + group parcel fan-out
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

## Group Chat Usage Guide

### Creating a Group

1. Log in and switch to the **Groups** tab in the UI
2. Click **New Group**, enter a name, and check the contacts to include
3. The group is created locally and all members are notified immediately via HTTP fan-out (`/group/api/notify_create`)
4. All members see the group in their sidebar without waiting for a first message

### Sending a Group Message

1. Select a group from the sidebar
2. Type a message and send
3. The sender's `GroupOrchestrator` builds a B-Tree parcel, encrypting the root key per-member
4. The parcel is POSTed to the server, which fans it out to each recipient
5. Recipients decrypt, store locally, and display via SSE push

### Group API Endpoints

| Endpoint | Method | Body | Description |
|----------|--------|------|-------------|
| `/group/api/create` | POST | `{name, members}` | Create a new group |
| `/group/api/send` | POST | `{group_id, message}` | Send a group message |
| `/group/api/notify_create` | POST | (parcel) | Receive group creation notification |
| `/group/api/receive` | POST | (parcel) | Receive a group message parcel |

### Local Testing (3 Users)

```bash
# Quick deploy (all 3 instances)
./deploy.sh          # start protyasha:7000, biprarshi:7001, shirsa:7002
./deploy.sh --wipe   # wipe DBs first, then start fresh

# Manual start
python -m AQM_Database.flask_app.app --user protyasha --port 7000 --host 127.0.0.1 --contacts biprarshi shirsa --contact-ports 7001 7002
python -m AQM_Database.flask_app.app --user biprarshi --port 7001 --host 127.0.0.1 --contacts protyasha shirsa --contact-ports 7000 7002
python -m AQM_Database.flask_app.app --user shirsa --port 7002 --host 127.0.0.1 --contacts protyasha biprarshi --contact-ports 7000 7001

# Default password: aqm-demo-2026
# NOTE: Ports 6000-6002 are blocked by Firefox/Chrome (X11 range) — use 7000+
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

Each user visits `https://username.yourdomain.com` and logs in with their password. Caddy handles TLS automatically. Both direct and group chat work out of the box.

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
| Direct / Groups tabs | Toggle between 1-to-1 and group conversations |
| Group creation modal | Select contacts from a checklist, name the group |
| Group member list | Panel in vault sidebar (bottom right, visible in group mode) |
| Instant group visibility | All members see a new group immediately on creation |
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
# All tests (302 total — needs Docker for server tests)
pytest AQM_Database/ -v

# By subsystem (no Docker required)
pytest AQM_Database/aqm_shared/tests/ -v    # 48 tests — crypto + context
pytest AQM_Database/aqm_db/tests/ -v        # 70 tests — vault + inventory (fakeredis)
pytest AQM_Database/aqm_contacts/tests/ -v  # 33 tests — contacts (SQLite)
pytest AQM_Database/aqm_session/tests/ -v   # 44 tests — ratchet + session
pytest AQM_Database/aqm_network/tests/ -v   # 26 tests — network protocol
pytest AQM_Database/aqm_app/tests/ -v       # 30 tests — orchestrator
pytest AQM_Database/aqm_group/tests/ -v     # 70 tests — group chat (key_tree, hot_edge, parcel, orchestrator)

# Requires Docker (Redis + PostgreSQL)
pytest AQM_Database/aqm_server/tests/ -v    # 37 tests — coin server + bridge
```

### Group Test Breakdown

| Module | Tests |
|--------|-------|
| `test_key_tree.py` | 20 — B-Tree build/decrypt, tier partitioning |
| `test_hot_edge.py` | 17 — edge activation, TTL expiry, key burn |
| `test_group_parcel.py` | 14 — wire format build/parse round-trip |
| `test_group_orchestrator.py` | 19 — send/receive flow, auto-create on receive |

---

## Key Design Decisions

### Direct Messaging

**Why amortize KEM operations?** ML-KEM-768 key generation and encapsulation are computationally expensive compared to classical ECDH. The ratchet amortizes this cost to approximately one KEM per 250 messages for GOLD tier.

**Why burn private keys?** Each private key is used exactly once and then deleted. This provides perfect forward secrecy — even if an attacker later compromises the device, past messages cannot be decrypted because the keys no longer exist.

**Why three tiers?** Different device conditions (battery, network quality) warrant different security/efficiency tradeoffs. A device on WiFi with full battery can afford ML-KEM-768; a device with 3% battery in a tunnel should use X25519.

**Why priority-based caps?** GOLD coins (scarce, expensive to generate) are reserved for trusted contacts who you communicate with frequently.

### Group Chat

| ID | Decision | Rationale |
|----|----------|-----------|
| D1 | Tier assignment is sender-local | The sender partitions members into tiers based on their own local contacts database |
| D2 | Sender is never a leaf in own parcel | The sender constructs the tree — they already know the root key |
| D3 | HOT edge scope is per (group_id, member_id) | B-Tree is per-group; edges are independent per member |
| D4 | HOT edge wraps existing SessionRatchet | Reuses the proven ephemeral AES-256 ratchet — no new crypto primitives |
| D5 | Group creation is creator-only (Phase I) | No invite/join protocol yet — creator specifies all members at creation |
| D6 | Member removal is out of scope (Phase I) | Requires branch key rotation — deferred to Phase II |
| D7 | Offline delivery reuses per-user mailbox | Extended the existing mailbox with a nullable `group_id` column |
| D8 | One coin consumed per COLD leaf member | Each leaf encrypts its branch_key share with a fresh KEM exchange |
| D9 | STRANGER coins fetched on-demand | Unknown users trigger synchronous network fetch for BRONZE coins |
| D10 | Group message history is client-only | The server is zero-knowledge — it routes encrypted parcels without storing them |

**Why a B-Tree instead of pairwise encryption?** Pairwise encryption costs O(N) KEM operations per message. The B-Tree groups members by tier (branch level), so the branch key only needs one KEM per leaf. Members sharing a branch share a single branch key encrypted once, reducing total KEM operations.

**Why Hot Edges?** After the first COLD exchange, subsequent messages to the same member within 10 minutes skip the KEM entirely and derive keys from a `SessionRatchet`. This makes rapid group conversation nearly free in coin cost while maintaining forward secrecy (chain key is burned on TTL expiry).

**Why zero-knowledge server?** The relay server fans out opaque parcels. It never sees plaintext, root keys, or branch keys. Group membership is known only to participants (the server sees recipient IDs for routing, but not message content or group structure).

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

## Troubleshooting

**Inventory shows 0 coins after start:** Partner Flask instance must be started first so their coins are on the server. Background sync retries every 10 seconds automatically.

**`liboqs` version mismatch warning:** Cosmetic only. To fix: `pip install liboqs-python==0.15.0`.

**DB password authentication failed after redeploying:** The PostgreSQL volume has the old password baked in. Run `docker volume rm aqm_databse_pg_data` then restart.

**Coin exhaustion 500 errors during rapid group sends:** This is expected when sending many messages quickly — each COLD leaf burns a coin. The background minter auto-replenishes; messages will succeed again within ~120 seconds.

**Group not visible to non-creators:** Ensure the creator's instance can reach all member instances via HTTP. The `/group/api/notify_create` fan-out must succeed for instant group visibility.

---

## Phase II Roadmap

- **Member removal + branch key rotation**: removing a member requires regenerating all branch keys for branches they had access to and re-encrypting for remaining members
- **Batched STRANGER fetch**: pipeline on-demand BRONZE coin fetches instead of sequential per-member lookups
- **Group admin management**: invite links, role changes, multi-admin support
- **Bidirectional HOT edge activation**: receiver mirrors the HOT state from the shared secret in the received parcel (currently only sender activates)
