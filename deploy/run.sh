#!/usr/bin/env bash
# Runs one job with the credentials from Bitwarden. Used by the systemd timers, and by hand:
#
#   sudo -u pushers /opt/product-pushers/deploy/run.sh push_all              # the daily run
#   sudo -u pushers /opt/product-pushers/deploy/run.sh push_all --dry-run    # safe test run (emails the report)
#   sudo -u pushers /opt/product-pushers/deploy/run.sh push_all --test-email
#   sudo -u pushers /opt/product-pushers/deploy/run.sh stock_sync sync       # what the 15-minute timer runs
#
# Credentials: the Bitwarden access token in /etc/product-pushers.token (bws injects the secrets).
# Without a token, a .env file in the app folder is used instead (no Bitwarden).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOKEN_FILE="${PUSHERS_TOKEN_FILE:-/etc/product-pushers.token}"
PY="$APP_DIR/.venv/bin/python"

job="${1:-}"
case "$job" in
    push_all|stock_sync) shift ;;
    *) echo "usage: $0 push_all|stock_sync [arguments]" >&2; exit 2 ;;
esac
[ -x "$PY" ] || { echo "Python environment missing: run deploy/setup_server.sh" >&2; exit 2; }
cd "$APP_DIR"

if [ -r "$TOKEN_FILE" ] && [ -s "$TOKEN_FILE" ]; then
    BWS_ACCESS_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
    export BWS_ACCESS_TOKEN
    exec bws run -- "$PY" "$job.py" "$@"
elif [ -f "$APP_DIR/.env" ]; then
    exec "$PY" "$job.py" "$@"
else
    echo "No Bitwarden token in $TOKEN_FILE and no .env file: nothing to run with (see docs/deployment.md)." >&2
    exit 2
fi
