#!/bin/sh
# Install a versioned snapshot: host wrappers never depend on /root/v2-jobs.
set -eu
SRC=$(nice -n 19 realpath "$(nice -n 19 dirname "$0")/..")
nice -n 19 mkdir -p /usr/local/lib/v2-board
for name in agent-run board-report; do
  if [ -f "/usr/local/bin/$name" ] && [ ! -f "/usr/local/bin/$name.bak-20261117" ]; then
    nice -n 19 cp -p "/usr/local/bin/$name" "/usr/local/bin/$name.bak-20261117"
  fi
  nice -n 19 install -m 755 "$SRC/scripts/$name" "/usr/local/bin/$name"
done
nice -n 19 install -m 644 "$SRC/backend/signals.py" /usr/local/lib/v2-board/signals.py
nice -n 19 python3 - <<'PY'
import json, shutil
from pathlib import Path
root = Path('/srv/v2-wiring-board')
root.mkdir(exist_ok=True)
(root / 'agents.json').write_text(json.dumps({c: bool(shutil.which(c)) for c in ('codex', 'claude', 'kiro-cli', 'zcode', 'devin', 'grok')}))
PY
