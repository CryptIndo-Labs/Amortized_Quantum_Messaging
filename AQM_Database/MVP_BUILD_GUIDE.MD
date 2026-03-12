# AQM MVP Build Guide

## Current State

The MVP runs as per-user Flask instances with password-protected login. Each user gets their own subdomain. All infrastructure files are already created and ready to deploy.

### What's Implemented

- **Authentication**: Each Flask instance has its own password (via `--password` flag, `AQM_PASSWORD` env var, or default `aqm-demo-2026`)
- **Login page**: Styled to match the AQM dashboard. Session cookie with HMAC-verified password
- **All API routes protected**: `/api/status`, `/api/send`, `/api/history`, `/api/contacts`, `/api/vault`, `/api/inventory`, `/stream` — all require login
- **`/api/receive` unprotected**: Server-to-server endpoint (Docker internal network only)
- **Configurable hosts**: `REDIS_HOST`, `PG_DSN`, `AQM_CONTACT_HOST_TEMPLATE` env vars for Docker deployment
- **Production files ready**: `Dockerfile`, `docker-compose.prod.yml`, `Caddyfile`, `.env.example`

---

## Running Locally (development)

```bash
# Start backing services
cd AQM_Database && docker compose up -d

# Terminal 1 — Coin Server
uvicorn AQM_Database.aqm_server.api:app --host 0.0.0.0 --port 8000

# Terminal 2 — Alice (password: mypassword)
python -m AQM_Database.flask_app.app --user alice --port 5000 \
  --contacts bob charlie --contact-ports 5001 5002 --password mypassword

# Terminal 3 — Bob
python -m AQM_Database.flask_app.app --user bob --port 5001 \
  --contacts alice charlie --contact-ports 5000 5002 --password mypassword

# Open http://localhost:5000 → login with "mypassword"
```

Default password (if `--password` not set): `aqm-demo-2026`

---

## WAN Deployment

### Architecture

```
                    ┌────────────────────────────────┐
                    │   VPS (cloud server)            │
                    │                                 │
                    │   Caddy (auto-HTTPS)            │
                    │   ├─ alice.aqm.example.com ──┐  │
                    │   ├─ bob.aqm.example.com   ──┤  │
                    │   └─ charlie.aqm.example.com ┤  │
                    │                              │  │
                    │   Docker Compose             │  │
                    │   ├─ Flask alice (:5000) ◄────┘  │
                    │   ├─ Flask bob   (:5001)         │
                    │   ├─ Flask charlie (:5002)       │
                    │   ├─ Coin Server  (:8000)        │
                    │   ├─ Redis 7 (internal)          │
                    │   └─ PostgreSQL 16 (internal)    │
                    │                                  │
                    │   Login gate per subdomain        │
                    └────────────────────────────────┘
                         ▲              ▲
                    HTTPS/TLS      HTTPS/TLS
                         │              │
                    Alice's browser  Bob's browser
                    (any network)    (any network)
```

### Step-by-Step Deployment

#### 1. Provision a VPS

Any Linux VPS with 2+ GB RAM:

| Provider       | Cheapest tier | Notes                      |
|----------------|---------------|----------------------------|
| Hetzner        | ~€4/mo        | EU/US, great value         |
| DigitalOcean   | $6/mo         | Simple, good docs          |
| Oracle Cloud   | Free tier     | Always-free ARM (24GB RAM) |

```bash
# On the VPS
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # then re-login
```

#### 2. Clone and configure

```bash
git clone https://github.com/CryptIndo-Labs/AQM_databse.git
cd AQM_databse

# Create secrets
cp .env.example .env
nano .env   # set real passwords
```

**`.env` contents** (all values must be changed):

```bash
AQM_DB_PASSWORD=<strong-random-password>

AQM_ALICE_PASSWORD=<alice-login-password>
AQM_BOB_PASSWORD=<bob-login-password>
AQM_CHARLIE_PASSWORD=<charlie-login-password>

FLASK_SECRET_ALICE=<random-hex-32-chars>
FLASK_SECRET_BOB=<random-hex-32-chars>
FLASK_SECRET_CHARLIE=<random-hex-32-chars>
```

Generate random values:
```bash
python3 -c "import os; print(os.urandom(24).hex())"
```

#### 3. Configure your domain

Edit `Caddyfile` — replace `aqm.example.com` with your domain:

```
alice.yourdomain.com {
    reverse_proxy alice:5000
}

bob.yourdomain.com {
    reverse_proxy bob:5001
}

charlie.yourdomain.com {
    reverse_proxy charlie:5002
}
```

Set DNS A records pointing all three subdomains to your VPS IP address.

#### 4. Launch

```bash
docker compose -f docker-compose.prod.yml up -d --build

# Watch logs
docker compose -f docker-compose.prod.yml logs -f

# Check health
docker compose -f docker-compose.prod.yml ps
```

#### 5. Access

| User    | URL                              | Password                |
|---------|----------------------------------|-------------------------|
| Alice   | https://alice.yourdomain.com     | `AQM_ALICE_PASSWORD`    |
| Bob     | https://bob.yourdomain.com       | `AQM_BOB_PASSWORD`      |
| Charlie | https://charlie.yourdomain.com   | `AQM_CHARLIE_PASSWORD`  |

Each user opens their URL → login page → enter password → dashboard.

---

## Authentication Details

### Flow

```
Browser → https://alice.yourdomain.com/
  → Caddy terminates TLS
  → Reverse proxy to Flask alice:5000
  → Flask checks session cookie
  → No session? → redirect to /login
  → User enters password
  → Flask verifies (SHA-256 + HMAC constant-time compare)
  → Sets session cookie (HttpOnly, SameSite=Lax)
  → Redirect to dashboard
```

### What's Protected

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

## Files Created for Deployment

| File                        | Purpose                                    |
|-----------------------------|--------------------------------------------|
| `Dockerfile`                | Python 3.12 + liboqs build                 |
| `docker-compose.prod.yml`   | Full stack: Redis, Postgres, Caddy, 3 Flask |
| `Caddyfile`                 | Reverse proxy + auto Let's Encrypt TLS     |
| `.env.example`              | Template for secrets                       |

### Code Changes Made

| File                              | Change                                         |
|-----------------------------------|-------------------------------------------------|
| `flask_app/app.py`               | Auth layer, background minting, tier-upgrade rekey, per-user session cookies, `--host`/`--password` flags, Docker host template |
| `flask_app/templates/index.html`  | Glass-morphism dashboard with minting section, PQ indicator dot (tier-based color) |
| `flask_app/templates/login.html`  | Login page (matching AQM visual style)      |
| `aqm_shared/config.py`           | `REDIS_HOST`/`REDIS_PORT` from env vars         |
| `aqm_server/config.py`           | `PG_DSN` from env var                           |
| `.gitignore`                      | Fixed `.env` pattern (was `*.env` which blocked `.env.example`) |

---

## Security Notes

- **TLS**: Caddy auto-provisions Let's Encrypt certificates. Zero config.
- **Passwords**: Hashed with SHA-256, compared with constant-time HMAC. Plaintext scrubbed from memory after hashing.
- **Session cookies**: `HttpOnly` (no JS access), `SameSite=Lax` (CSRF protection), per-user cookie names (`aqm_session_{user}`), 24h permanent sessions.
- **API auth**: `@login_required` returns JSON 401 for API/SSE endpoints, HTML redirect for pages.
- **Internal services**: Redis and PostgreSQL have no exposed ports — Docker internal network only.
- **`/api/receive`**: Unprotected but only reachable within the Docker network (Caddy doesn't expose it externally since it's POST-only between containers).
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

## Cost Estimate

| Component          | Monthly cost          |
|--------------------|-----------------------|
| VPS (Hetzner CX22) | ~€4                  |
| Domain              | ~$1 ($10/year)       |
| TLS certificates    | Free (Let's Encrypt) |
| **Total**           | **~$5/month**        |
