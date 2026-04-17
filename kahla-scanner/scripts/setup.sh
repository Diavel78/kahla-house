#!/usr/bin/env bash
# One-time local bootstrap for kahla-scanner on macOS (or any Unix).
# Idempotent — safe to re-run after a pip-requirements update.
#
# Usage:
#   ./kahla-scanner/scripts/setup.sh

set -eu
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3.11}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "ERROR: $PY not found. Install via 'brew install python@3.11' or set PYTHON=..." >&2
    exit 1
fi

if [ ! -d venv ]; then
    echo "Creating venv at $(pwd)/venv"
    "$PY" -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo ">>> Created kahla-scanner/.env from template."
    echo ">>> Fill in SUPABASE_URL, SUPABASE_SERVICE_KEY, POLYMARKET_KEY_ID,"
    echo ">>> POLYMARKET_SECRET_KEY, then run scripts/poll.sh"
fi

echo
echo "Setup complete. Next:"
echo "  ./scripts/poll.sh"
