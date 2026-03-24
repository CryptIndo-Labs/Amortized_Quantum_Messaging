#!/usr/bin/env bash
# AQM Localhost Deploy Script
# Starts 3 instances: protyasha (7000), biprarshi (7001), shirsa (7002)
# Usage: ./deploy.sh [--wipe]  (--wipe removes all local DBs first)

set -euo pipefail

PORTS=(7000 7001 7002)
USERS=(protyasha biprarshi shirsa)
DB_DIR="$HOME/.aqm"

# ── Kill existing instances ──
for port in "${PORTS[@]}"; do
  pid=$(lsof -ti :$port 2>/dev/null || true)
  if [ -n "$pid" ]; then
    echo "Killing process on port $port (PID $pid)"
    kill "$pid" 2>/dev/null || true
  fi
done
sleep 1

# ── Optional DB wipe ──
if [[ "${1:-}" == "--wipe" ]]; then
  echo "Wiping all local databases..."
  for user in "${USERS[@]}"; do
    rm -f "$DB_DIR/${user}_"*.db "$DB_DIR/${user}_"*.db-wal "$DB_DIR/${user}_"*.db-shm
  done
  echo "Done."
fi

# ── Start instances ──
echo "Starting AQM instances..."

python -m AQM_Database.flask_app.app \
  --user protyasha --port 7000 --host 127.0.0.1 \
  --contacts biprarshi shirsa --contact-ports 7001 7002 &

python -m AQM_Database.flask_app.app \
  --user biprarshi --port 7001 --host 127.0.0.1 \
  --contacts protyasha shirsa --contact-ports 7000 7002 &

python -m AQM_Database.flask_app.app \
  --user shirsa --port 7002 --host 127.0.0.1 \
  --contacts protyasha biprarshi --contact-ports 7000 7001 &

sleep 3

# ── Verify ──
OK=0
for i in "${!PORTS[@]}"; do
  if curl -s -o /dev/null -w '' "http://127.0.0.1:${PORTS[$i]}/" 2>/dev/null; then
    echo "  ${USERS[$i]} → http://127.0.0.1:${PORTS[$i]}  ✓"
    OK=$((OK+1))
  else
    echo "  ${USERS[$i]} → http://127.0.0.1:${PORTS[$i]}  ✗ FAILED"
  fi
done

echo ""
if [ "$OK" -eq 3 ]; then
  echo "All 3 instances running. Password: aqm-demo-2026"
else
  echo "WARNING: Only $OK/3 instances started. Check logs above."
fi
