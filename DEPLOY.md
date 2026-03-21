# AQM Deploy Guide

## Quick Start (Localhost)

```bash
./deploy.sh          # start 3 test instances
./deploy.sh --wipe   # wipe DBs first, then start fresh
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

```bash
rm -f ~/.aqm/galice_*.db* ~/.aqm/gbob_*.db* ~/.aqm/gcharlie_*.db*
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

## Notes

- Ports 6000-6002 are blocked by Firefox/Chrome (X11 range) — use 7000+
- Each group send consumes 1 BRONZE coin per recipient
- SQLite DBs are stored in `~/.aqm/`
- Cloudflare proxy (orange cloud) breaks SSE — use DNS-only (grey cloud)
