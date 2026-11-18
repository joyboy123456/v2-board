#!/bin/sh
# Install or update v2-dispatch from a checkout at /opt/v2-board. Idempotent.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
[ -f /etc/v2-dispatch.json ] || install -m 600 "$SRC/dispatcher/v2-dispatch.example.json" /etc/v2-dispatch.json
for s in board-ask board-dispatch; do ln -sf "$SRC/scripts/$s" "/usr/local/bin/$s"; done
mkdir -p /var/lib/v2-dispatch
install -m 644 "$SRC/dispatcher/v2-dispatch.service" /etc/systemd/system/v2-dispatch.service
systemctl daemon-reload
systemctl enable v2-dispatch >/dev/null
systemctl restart v2-dispatch
systemctl --no-pager status v2-dispatch | head -5
