#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Erstelle virtuelle Umgebung …"
  python3.12 -m venv .venv
fi

./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/alembic upgrade head
./.venv/bin/python -m app.cli seed-sources

echo "Starte Worker und Web-App auf http://127.0.0.1:${APP_PORT:-8765} …"
./.venv/bin/python -m app.worker &
WORKER_PID=$!
trap 'kill "$WORKER_PID" 2>/dev/null || true' EXIT INT TERM

./.venv/bin/python -m app.cli serve
