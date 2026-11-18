#!/bin/sh
# Grant only the container service identity access; do not change file ownership.
set -eu
DATA=${1:-/srv/v2-wiring-board}
nice -n 19 mkdir -p "$DATA/runs" "$DATA/events"
nice -n 19 chmod 2775 "$DATA/runs" "$DATA/events"
if command -v setfacl >/dev/null 2>&1; then
  nice -n 19 setfacl -R -m u:10001:rwX "$DATA"
  nice -n 19 find "$DATA" -type d -exec nice -n 19 setfacl -m d:u:10001:rwX {} +
else
  echo '需要 acl (setfacl)，请安装后重试；未开放全局写权限' >&2
  exit 1
fi
