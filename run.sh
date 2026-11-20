#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

OPEN_BROWSER=1

for arg in "$@"; do
  case "$arg" in
    --no-browser) OPEN_BROWSER=0 ;;
    -h|--help)
      echo "Usage: $0 [--no-browser]"
      exit 0
      ;;
    *) echo "Unbekannte Option: $arg" >&2; exit 1 ;;
  esac
done

# Port aus der Umgebung oder .env (ohne die Datei zu sourcen).
if [ -z "${APP_PORT:-}" ] && [ -f .env ]; then
  APP_PORT="$(sed -n 's/^[[:space:]]*APP_PORT[[:space:]]*=[[:space:]]*//p' .env | tail -n 1)"
fi
PORT="${APP_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"

if [ ! -d .venv ]; then
  echo "Erstelle virtuelle Umgebung …"
  python3.12 -m venv .venv
fi

./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/alembic upgrade head
./.venv/bin/python -m app.cli seed-sources

echo "Starte Worker und Web-App auf ${URL} …"
./.venv/bin/python -m app.worker &
WORKER_PID=$!

./.venv/bin/python -m app.cli serve &
SERVER_PID=$!
OPENER_PID=""

cleanup() {
  trap - INT TERM
  kill "$SERVER_PID" "$WORKER_PID" 2>/dev/null || true
  if [ -n "$OPENER_PID" ]; then
    kill "$OPENER_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

# Sobald die Web-App antwortet, den Browser öffnen.
if [ "$OPEN_BROWSER" -eq 1 ]; then
  (
    for _ in $(seq 1 60); do
      curl -fsS -m 2 "$URL" >/dev/null 2>&1 && break
      sleep 1
    done
    if command -v open >/dev/null 2>&1; then
      open "$URL" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$URL" >/dev/null 2>&1 || true
    fi
  ) &
  OPENER_PID=$!
fi

wait "$SERVER_PID"
