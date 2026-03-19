# AQM — System Explained

This document serves as an instruction manual and architectural explainer for the AQM (Amortized Quantum Messaging) codebase.

---

## Overview

AQM is a post-quantum secure messaging system. Instead of performing an expensive ML-KEM-768 key encapsulation for every message, AQM uses a symmetric ratchet to derive message keys from a single shared secret. One KEM operation covers up to 250 messages — providing post-quantum resistance without per-message overhead.

---

## Authentication

### Flow

```
Browser → https://alice.yourdomain.com/
  → Caddy terminates TLS
  → Reverse proxy to Flask (per-user instance)
  → Flask checks session cookie
  → No session? → redirect to /login
  → User enters password
  → Flask verifies (SHA-256 + HMAC constant-time compare)
  → Sets session cookie (HttpOnly, SameSite=Lax)
  → Redirect to dashboard
```

### Route Protection

| Route                        | Auth Required | Notes                          |
|------------------------------|---------------|--------------------------------|
| `/login`                     | No            | Login page                     |
| `/logout`                    | No            | Clears session                 |
| `/`                          | Yes           | Dashboard                      |
| `/api/status`                | Yes           | Status JSON                    |
| `/api/send`                  | Yes           | Send message                   |
| `/api/history`               | Yes           | Message history                |
| `/api/contacts`              | Yes           | Contact list                   |
| `/api/contacts/<id>/priority`| Yes           | Set priority                   |
| `/api/vault`                 | Yes           | Vault stats                    |
| `/api/inventory`             | Yes           | Coin inventory                 |
| `/api/debug/server-coins`    | Yes           | Debug endpoint                 |
| `/stream`                    | Yes           | SSE real-time updates          |
| `/api/receive`               | **No**        | Server-to-server (internal)    |

### Password Configuration (priority order)

1. `--password` CLI flag
2. `AQM_PASSWORD` environment variable
3. Default: `aqm-demo-2026`

---

## Coin Tiers

| Tier | KEM Algorithm | Signature | Ratchet Limit | Public Key Size |
|------|---------------|-----------|---------------|-----------------|
| GOLD | ML-KEM-768 | ML-DSA-65 | 250 messages | ~3.6 KB |
| SILVER | ML-KEM-768 | Ed25519 | 150 messages | ~1.2 KB |
| BRONZE | X25519 | Ed25519 | 75 messages | ~96 B |

Each coin is a public/private keypair. The sender uses the receiver's public key once to establish a shared secret, then both sides derive message keys via HKDF. When the ratchet limit is reached, a new coin is consumed and a new session begins.

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

---

## Priority & Promotion

Contacts are auto-promoted based on **bidirectional** messaging frequency:

- **STRANGER → MATE**: 4 mutual messages within 30 days
- **MATE → BESTIE**: 5 mutual messages within 7 days

Mutual messages are counted as `2 * MIN(sent, received)` — one-sided spam does not count.

### Proof-of-Burn Promotion

Contacts can also be promoted by burning coins:

- **STRANGER → MATE**: 2 mutual bronze burns (both sides must burn)
- **MATE → BESTIE**: 2 mutual silver burns OR 4 mutual bronze burns

Burn counters reset to 0 after each promotion.

---

## Session Ratchet

The `SessionRatchet` uses dual independent chains — one for sending, one for receiving. The `is_initiator` flag ensures both parties derive the same send/recv keys without coordination.

- `derive_send_key()` — advances the send chain, returns the next encryption key
- `derive_recv_key()` — advances the recv chain, returns the next decryption key
- The tier is **locked at session start** — mid-session promotion does not force a rekey
- When the ratchet is exhausted (message count hits the tier limit), the next message triggers a rekey using a new coin

---

## Subsystem Modules

### `aqm_shared/` — Shared types, errors, config, crypto

- **`crypto_engine.py`**: Implements ML-KEM-768, ML-DSA-65, X25519, and AEAD encryption/decryption. All cryptographic operations go through this module.
- **`context_manager.py`**: Simulates device context (battery, WiFi, signal strength) and selects the appropriate tier.
- **`config.py`**: Constants, budget caps, thresholds, Redis/PostgreSQL host config (from env vars).
- **`types.py`**: Shared dataclasses (`Coin`, `CoinTier`, `DeviceContext`, etc.).
- **`errors.py`**: Exception hierarchy (`VaultError`, `InventoryError`, `CryptoError`, etc.).

### `aqm_db/` — Redis layer (vault + inventory)

- **`vault.py`** (`SecureVault`): Stores the user's own private keys in Redis. Keys are **burned on use** — once a private key decrypts a message, it is deleted from Redis (perfect forward secrecy).
- **`inventory.py`** (`SmartInventory`): Caches partner public keys locally. Budget-capped per tier per contact based on priority.
- **`connection.py`**: Redis client factory. Uses `decode_responses=False` (binary blobs).
- **`stats.py`**: Storage size reporter for vault/inventory.

### `aqm_contacts/` — SQLite contacts database

- **`contacts_db.py`**: Manages contacts, priority levels, message history, and burn counters. Stores per-contact metadata including `my_burned_bronze`, `their_burned_bronze`, `my_burned_silver`, `their_burned_silver`.
- **`models.py`**: `Contact` dataclass.

### `aqm_session/` — HKDF ratchet + persistence

- **`ratchet.py`**: The core `SessionRatchet` — derives send/recv keys via HKDF from a shared secret, tracks message count, signals when rekey is needed.
- **`session_store.py`**: SQLite-backed session persistence. Stores active ratchet state so sessions survive restarts.

### `aqm_server/` — PostgreSQL coin server (FastAPI)

- **`api.py`**: REST endpoints for uploading and fetching coins. Delete-on-fetch — once a coin is fetched by a partner, it's gone from the server.
- **`coin_inventory.py`**: Server-side coin registry logic.
- **`db.py`**: Async PostgreSQL connection pool (asyncpg).
- **`migrations/`**: SQL schema files, auto-run on first start.

### `aqm_network/` — WebSocket relay

- **`protocol.py`**: Message framing (header + encrypted payload).
- **`relay_server.py`**: WebSocket hub that routes messages between connected clients.
- **`client.py`**: Device-side WebSocket client.

### `aqm_app/` — Application orchestrator

- **`orchestrator.py`**: Wires all subsystems together — vault, inventory, contacts, sessions, network. Entry point for the headless demo.

### `flask_app/` — Web UI

- **`app.py`**: Per-user Flask server with login, SSE streaming, background coin minting, and all API routes.
- **`aqm_bridge.py`**: Async-to-sync helper (bridges Flask's sync world to the async subsystems).
- **`templates/index.html`**: SPA dashboard with real-time SSE updates, ratchet progress bars, vault stats, and priority promotion indicators.
- **`templates/login.html`**: Password login page.

### `bridge.py` — Redis ↔ PostgreSQL bridge

- `upload_coins()`: Pushes locally minted coins to the PostgreSQL server so partners can fetch them.
- `sync_inventory()`: Pulls partner coins from the server into the local Redis inventory.

---

## Deployment Files

| File                        | Purpose                                    |
|-----------------------------|--------------------------------------------|
| `Dockerfile`                | Python 3.12 + liboqs build                 |
| `docker-compose.prod.yml`   | Full stack: Redis, Postgres, Caddy, N Flask instances |
| `Caddyfile`                 | Reverse proxy + auto Let's Encrypt TLS     |
| `deploy.py`                 | Generates `.env`, `docker-compose.prod.yml`, `Caddyfile` for N users |
| `.env.example`              | Template for secrets                       |
| `docker-compose.yml`        | Local dev: Redis 7 + PostgreSQL 16         |

---

## Security Notes

- **TLS**: Caddy auto-provisions Let's Encrypt certificates. Zero config.
- **Passwords**: Hashed with SHA-256, compared with constant-time HMAC. Plaintext scrubbed from memory after hashing.
- **Session cookies**: `HttpOnly` (no JS access), `SameSite=Lax` (CSRF protection), per-user cookie names (`aqm_session_{user}`), 24h permanent sessions.
- **API auth**: `@login_required` returns JSON 401 for API/SSE endpoints, HTML redirect for pages.
- **Internal services**: Redis and PostgreSQL have no exposed ports — Docker internal network only.
- **`/api/receive`**: Unprotected but only reachable within the Docker network.
- **Private key burn**: Keys are deleted immediately after use — compromising the device later cannot decrypt past messages.
- **`.env`**: Gitignored. Never committed.

---

## Maintenance

```bash
# View logs
docker compose -f docker-compose.prod.yml logs -f alice

# Restart a single service
docker compose -f docker-compose.prod.yml restart alice

# Update code (pull + rebuild)
git pull
docker compose -f docker-compose.prod.yml up -d --build

# Reset all state
docker compose -f docker-compose.prod.yml down -v
docker compose -f docker-compose.prod.yml up -d --build
```

---

## Troubleshooting

**Inventory shows 0 coins after start:** Partner Flask instance must be started first so their coins are on the server. Background sync retries every 10 seconds automatically.

**`liboqs` version mismatch warning:** Cosmetic only. To fix: `pip install liboqs-python==0.15.0`.

**DB password authentication failed after redeploying:** The PostgreSQL volume has the old password baked in. Run `docker volume rm aqm_databse_pg_data` then restart.

**Cloudflare proxy breaks SSE:** If using Cloudflare, set DNS records to DNS-only (grey cloud). The orange-cloud proxy buffers SSE streams and injects scripts that cause blank screens.
