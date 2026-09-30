#!/bin/bash
# Start the analyst console. Reads .env for the retriever binding; ARANGO_CLOUD_*
# and ANTHROPIC_API_KEY come from your shell environment.
cd "$(dirname "$0")"
set -a; [ -f .env ] && . ./.env; set +a
exec ./.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 "$@"
