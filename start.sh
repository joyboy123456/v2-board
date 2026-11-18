#!/bin/sh
# Foreground preview, no process killing. Default :3161, explicit port supported.
set -eu
PORT=${1:-3161}
SRC=$(nice -n 19 realpath "$(nice -n 19 dirname "$0")")
cd "$SRC/frontend"
nice -n 19 npx vite build
cd "$SRC/backend"
exec nice -n 19 "$SRC/.venv/bin/python" -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
