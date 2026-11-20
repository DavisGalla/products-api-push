#!/usr/bin/env bash
# Set up (or update) the product pushers on a Linux server: Ubuntu 22.04 / 24.04 or Debian 12.
# Run as root. Safe to run again: it updates the code, Python packages, bws and the timers.
#
#   sudo bash deploy/setup_server.sh
#
# Settings (environment variables, all optional):
#   REPO_URL     git repository           (default: https://github.com/DavisGalla/products-api-push.git)
#   APP_DIR      install folder           (default: /opt/product-pushers)
#   APP_USER     user the jobs run as     (default: pushers)
#   BWS_VERSION  Bitwarden Secrets CLI    (default: 2.1.0)
#   TIMEZONE     e.g. Europe/Madrid       (default: leave the server's timezone as it is)
#   DAILY_TIME   push_all start, HH:MM    (default: 06:00, server time)
#   SYNC_EVERY   stock sync interval, min (default: 15)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/DavisGalla/products-api-push.git}"
APP_DIR="${APP_DIR:-/opt/product-pushers}"
APP_USER="${APP_USER:-pushers}"
BWS_VERSION="${BWS_VERSION:-2.1.0}"
DAILY_TIME="${DAILY_TIME:-06:00}"
SYNC_EVERY="${SYNC_EVERY:-15}"
TOKEN_FILE="/etc/product-pushers.token"

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run this as root: sudo bash $0"
command -v apt-get >/dev/null || fail "this script is for Ubuntu / Debian (apt-get not found)"
[[ "$DAILY_TIME" =~ ^[0-2][0-9]:[0-5][0-9]$ ]] || fail "DAILY_TIME must look like 06:00"
[[ "$SYNC_EVERY" =~ ^[0-9]+$ ]] && [ "$SYNC_EVERY" -ge 5 ] && [ "$SYNC_EVERY" -le 60 ] \
    || fail "SYNC_EVERY must be a number of minutes between 5 and 60"

say "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip unzip curl ca-certificates fonts-dejavu-core >/dev/null

if [ -n "${TIMEZONE:-}" ]; then
    say "Timezone: $TIMEZONE"
    timedatectl set-timezone "$TIMEZONE"
fi

say "User '$APP_USER'"
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/var/lib/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
fi

say "Code in $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    runuser -u "$APP_USER" -- git -C "$APP_DIR" pull --ff-only
else
    [ -e "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ] && fail "$APP_DIR exists and is not a git checkout"
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    runuser -u "$APP_USER" -- git clone --quiet "$REPO_URL" "$APP_DIR"
fi

say "Python packages (virtual environment $APP_DIR/.venv)"
runuser -u "$APP_USER" -- python3 -m venv "$APP_DIR/.venv"
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

say "Bitwarden Secrets Manager CLI (bws $BWS_VERSION)"
if ! command -v bws >/dev/null || ! bws --version 2>/dev/null | grep -q "$BWS_VERSION"; then
    arch="$(uname -m)"
    case "$arch" in
        x86_64)  target="x86_64-unknown-linux-gnu" ;;
        aarch64) target="aarch64-unknown-linux-gnu" ;;
        *) fail "no bws build for CPU '$arch'" ;;
    esac
    base="https://github.com/bitwarden/sdk-sm/releases/download/bws-v$BWS_VERSION"
    zip="bws-$target-$BWS_VERSION.zip"
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/$zip" "$base/$zip"
    curl -fsSL -o "$tmp/sums.txt" "$base/bws-sha256-checksums-$BWS_VERSION.txt"
    (cd "$tmp" && grep " $zip\$" sums.txt | sha256sum -c --quiet -) || fail "bws download failed its checksum"
    unzip -q -o "$tmp/$zip" -d "$tmp"
    install -m 755 "$tmp/bws" /usr/local/bin/bws
    rm -rf "$tmp"
fi
bws --version

say "Bitwarden access token file $TOKEN_FILE"
if [ ! -f "$TOKEN_FILE" ]; then
    install -m 640 -o root -g "$APP_USER" /dev/null "$TOKEN_FILE"
fi
chown root:"$APP_USER" "$TOKEN_FILE"
chmod 640 "$TOKEN_FILE"

say "Configuration"
if [ ! -f "$APP_DIR/push_all_config.json" ]; then
    runuser -u "$APP_USER" -- cp "$APP_DIR/push_all_config.example.json" "$APP_DIR/push_all_config.json"
    echo "Created $APP_DIR/push_all_config.json from the example - edit it (see docs/deployment.md)."
fi
chmod +x "$APP_DIR/deploy/run.sh"

say "Timers: push_all daily at $DAILY_TIME, stock sync every $SYNC_EVERY minutes"
for f in "$APP_DIR"/deploy/systemd/*.service "$APP_DIR"/deploy/systemd/*.timer; do
    sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@APP_USER@|$APP_USER|g" \
        -e "s|@DAILY_TIME@|$DAILY_TIME|g" -e "s|@SYNC_EVERY@|$SYNC_EVERY|g" \
        "$f" > "/etc/systemd/system/$(basename "$f")"
done
systemctl daemon-reload
systemctl enable --now product-pushers-daily.timer product-pushers-stock-sync.timer >/dev/null

say "Quick self-test (the runner's own tests, no API contacted)"
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" "$APP_DIR/tests/run_all.py" push_all stock_sync orders

echo
echo "Done. Server IP address (give it to eMAG / OnBuy if they ask to whitelist it):"
curl -fsS -m 10 https://api.ipify.org || echo "(could not detect it)"
echo
if [ ! -s "$TOKEN_FILE" ]; then
    echo
    echo "NEXT: put the Bitwarden access token into $TOKEN_FILE (see docs/deployment.md, step 4)."
    echo "      Until then the scheduled runs stop with 'no Bitwarden token'."
fi
systemctl list-timers 'product-pushers-*' --no-pager
