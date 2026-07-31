#!/usr/bin/env bash
#
# Install or update DICE on a Debian/Ubuntu server. Idempotent: safe to re-run
# to deploy a new version.
#
# Run as root, on the server:
#     bash deploy/install.sh <server-name>
#
# <server-name> is the domain (preferred) or bare IP the site answers on.
#
set -euo pipefail

SERVER_NAME="${1:-}"
if [[ -z "$SERVER_NAME" ]]; then
    echo "usage: bash deploy/install.sh <domain-or-ip>" >&2
    exit 2
fi

APP_DIR=/opt/dice
APP_USER=dice
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\n== %s\n' "$*"; }

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx rsync

log "Creating the $APP_USER service account"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

log "Syncing application code to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.env' \
    "$REPO_ROOT/" "$APP_DIR/"

log "Building the Python environment"
python3 -m venv "$APP_DIR/backend/.venv"
"$APP_DIR/backend/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/backend/.venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "Writing /etc/dice/dice.env"
mkdir -p /etc/dice
if [[ ! -f /etc/dice/dice.env ]]; then
    # No DUNE_API_KEY by default: every user brings their own key through the
    # UI, so the server never holds one. Add it here only for a private,
    # single-user deployment.
    cat > /etc/dice/dice.env <<'ENV'
# DUNE_API_KEY=
# DUNE_QUERY_ID=
DICE_EXECUTION_TIMEOUT=900
DICE_MAX_ROWS=1000000
ENV
fi
chmod 640 /etc/dice/dice.env
chown root:"$APP_USER" /etc/dice/dice.env

log "Installing the systemd unit"
install -m 644 "$APP_DIR/deploy/dice.service" /etc/systemd/system/dice.service
systemctl daemon-reload
systemctl enable --now dice
systemctl restart dice

log "Configuring nginx for $SERVER_NAME"
sed "s|__SERVER_NAME__|$SERVER_NAME|g" "$APP_DIR/deploy/nginx.conf" > /etc/nginx/sites-available/dice
# nginx refuses to start with an IPv6 listener on a host that has no IPv6.
if [[ ! -f /proc/net/if_inet6 ]]; then
    echo "   no IPv6 on this host — dropping the IPv6 listener"
    sed -i '/listen \[::\]:80;/d' /etc/nginx/sites-available/dice
fi
ln -sf /etc/nginx/sites-available/dice /etc/nginx/sites-enabled/dice
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    log "Opening the firewall for nginx"
    ufw allow 'Nginx Full' >/dev/null
fi

log "Checking that the app is up"
sleep 2
if curl -fsS --max-time 10 http://127.0.0.1:8000/api/health >/dev/null; then
    echo "DICE is running at http://$SERVER_NAME"
else
    echo "Health check failed. Logs:" >&2
    journalctl -u dice -n 40 --no-pager >&2
    exit 1
fi

cat <<NEXT

Next steps
  1. TLS — required before anyone types a Dune API key into this site:
         apt-get install -y certbot python3-certbot-nginx
         certbot --nginx -d $SERVER_NAME
     Certbot needs a real domain; it cannot issue a certificate for a bare IP.
  2. Optional password gate (DICE has no login of its own):
         apt-get install -y apache2-utils
         htpasswd -c /etc/nginx/dice.htpasswd <username>
         # then uncomment the two auth_basic lines in /etc/nginx/sites-available/dice
         systemctl reload nginx

Service management
  systemctl status dice
  journalctl -u dice -f
  bash deploy/install.sh $SERVER_NAME    # re-run to deploy an update
NEXT
