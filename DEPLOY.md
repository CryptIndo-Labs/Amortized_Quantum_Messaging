# AQM Deploy Guide

## Prerequisites (Localhost)

The app requires **Redis** and **PostgreSQL** (port 5433) to be running before `deploy.sh`.

```bash
# 1. Start Redis (if not already running)
redis-server --daemonize yes
redis-cli ping   # should return PONG

# 2. Start PostgreSQL via Docker Compose (exposes 5433)
cd AQM_Database && docker compose up -d postgres && cd ..
pg_isready -h 127.0.0.1 -p 5433   # should say "accepting connections"
```

## Quick Start (Localhost)

```bash
./deploy.sh          # start 3 test instances
./deploy.sh --wipe   # wipe SQLite DBs first, then start fresh
```

### Instances

| User     | URL                    | Port |
|----------|------------------------|------|
| galice   | http://127.0.0.1:7000  | 7000 |
| gbob     | http://127.0.0.1:7001  | 7001 |
| gcharlie | http://127.0.0.1:7002  | 7002 |

**Password:** `aqm-demo-2026` (override with `--password` or `AQM_PASSWORD` env var)

### Manual Start

```bash
python -m AQM_Database.flask_app.app \
  --user galice --port 7000 --host 127.0.0.1 \
  --contacts gbob gcharlie --contact-ports 7001 7002
```

### Stop All

```bash
pkill -f "AQM_Database.flask_app.app"
```

### Wipe DBs Manually

`deploy.sh --wipe` only removes SQLite files. For a **full reset** (SQLite + PostgreSQL + Redis):

```bash
# SQLite
rm -f ~/.aqm/galice_*.db* ~/.aqm/gbob_*.db* ~/.aqm/gcharlie_*.db*

# PostgreSQL (wipe and re-run migrations)
PGPASSWORD=aqm_dev_password psql -h 127.0.0.1 -p 5433 -U aqm_user -d aqm \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
PGPASSWORD=aqm_dev_password psql -h 127.0.0.1 -p 5433 -U aqm_user -d aqm \
  -f AQM_Database/aqm_server/migrations/create_coin_inventory.sql \
  -f AQM_Database/aqm_server/migrations/003_group_and_mailbox_extension.sql

# Redis
redis-cli FLUSHALL
```

## Production (DigitalOcean Droplet)

**Droplet:** 64.227.181.98 (Ubuntu 24.04)
**Domain:** cryptindo-aqm.org (Caddy + auto TLS)

```bash
ssh root@64.227.181.98
cd /opt/aqm
git pull origin main
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d --build
```

See `AQM_Database/MVP_Build_Guide.md` for full production setup.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `VaultUnavailableError: Cannot connect to Redis at localhost:6379` | Redis not running | `redis-server --daemonize yes` |
| `ConnectionPoolError: Connect call failed ('127.0.0.1', 5433)` | PostgreSQL container not running | `cd AQM_Database && docker compose up -d postgres` |
| `pg_isready -p 5433` says "no response" but `-p 5432` works | Local PG runs on 5432; app expects Docker-mapped 5433 | Start the Docker container, not the system PG |
| Tables missing after DB wipe | Migrations not re-applied after `DROP SCHEMA` | Re-run the `.sql` files (see "Wipe DBs Manually") |

## Notes

- Ports 6000-6002 are blocked by Firefox/Chrome (X11 range) — use 7000+
- Each group send consumes 1 BRONZE coin per recipient
- SQLite DBs are stored in `~/.aqm/`
- PostgreSQL is accessed via Docker on port **5433** (mapped from container's 5432)
- Cloudflare proxy (orange cloud) breaks SSE — use DNS-only (grey cloud)
