#!/usr/bin/env bash
# Starts RESONANCE on http://127.0.0.1:8000 (API + built frontend + job worker in one process).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
. .venv/bin/activate
cd backend && exec uvicorn resonance.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
