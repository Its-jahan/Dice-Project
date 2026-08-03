#!/usr/bin/env bash
#
# Install or update DICE on a Debian/Ubuntu server. Idempotent: safe to re-run
# to deploy a new version.
#
# Run as root, on the server:
#
#     bash deploy/install.sh <domain-or-ip> [--self-signed] [--with-graph]
#
#     bash deploy/install.sh dice.example.com     # domain: add certbot after
#     bash deploy/install.sh 203.0.113.10 --self-signed
#                                                 # bare IP: encrypted at once
#     bash deploy/install.sh 203.0.113.10         # bare IP, plain HTTP
#     bash deploy/install.sh dice.example.com --with-graph
#                                                 # also build the /graph code map
#
# With no domain, --self-signed is strongly recommended: the Dune API key
# travels in a request header, and plain HTTP puts it on the wire in the clear.
# deploy/README.md also describes the sslip.io route, which yields a real
# publicly-trusted certificate for a bare IP.
#
set -euo pipefail

SERVER_NAME="${1:-}"
SELF_SIGNED=false
WITH_GRAPH=false
for arg in "${@:2}"; do
    case "$arg" in
        --self-signed) SELF_SIGNED=true ;;
        --with-graph)  WITH_GRAPH=true ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [[ -z "$SERVER_NAME" ]]; then
    echo "usage: bash deploy/install.sh <domain-or-ip> [--self-signed] [--with-graph]" >&2
    exit 2
fi

APP_DIR=/opt/dice
APP_USER=dice
# Kept outside APP_DIR: the rsync below runs --delete, so a graph built into
# the app directory would be destroyed by the next deploy.
GRAPH_DIR=/opt/graph-site
GRAPH_VENV=/opt/graphify-venv
TLS_DIR=/etc/dice/tls
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# A bare IPv4 address gets an sslip.io alias too, so the operator can switch to
# a real Let's Encrypt certificate later without touching this config.
IS_IP=false
SSLIP_NAME=""
if [[ "$SERVER_NAME" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    IS_IP=true
    SSLIP_NAME="${SERVER_NAME}.sslip.io"
fi

log() { printf '\n== %s\n' "$*"; }

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx rsync openssl

log "Creating the $APP_USER service account"
id -u "$APP_USER" >/dev/null 2>&1 \
    || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

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
    # UI, so the server never holds one. Setting it here makes it the fallback
    # for every keyless request that reaches the site, including other people's.
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

# --------------------------------------------------------------- code graph
#
# Graphify turns the codebase into a queryable knowledge graph and a browsable
# HTML map, served at /graph. Rebuilt on every deploy so it never describes
# code that is no longer running. Installed on first use with --with-graph;
# after that it refreshes itself because the venv is present.

if [[ "$WITH_GRAPH" == true || -x "$GRAPH_VENV/bin/graphify" ]]; then
    if [[ ! -x "$GRAPH_VENV/bin/graphify" ]]; then
        log "Installing Graphify (isolated from the app venv)"
        python3 -m venv "$GRAPH_VENV"
        "$GRAPH_VENV/bin/pip" install --quiet --upgrade pip
        "$GRAPH_VENV/bin/pip" install --quiet graphifyy
    fi
    log "Rebuilding the code graph"
    # Build inside APP_DIR because that is where the source is, then move the
    # output out of the way of the next deploy.
    if "$GRAPH_VENV/bin/graphify" update "$APP_DIR" >/dev/null 2>&1; then
        mkdir -p "$GRAPH_DIR"
        rsync -a --delete "$APP_DIR/graphify-out/" "$GRAPH_DIR/"
        rm -rf "$APP_DIR/graphify-out"
    else
        # A failed graph must never fail a deploy: the app does not need it.
        log "Graph rebuild failed; leaving the previous one in place"
    fi
fi

# --------------------------------------------------------------------- nginx

server_names="$SERVER_NAME"
[[ -n "$SSLIP_NAME" ]] && server_names="$SERVER_NAME $SSLIP_NAME"

# An existing Let's Encrypt certificate beats generating a self-signed one:
# certbot may already have been run against the IP's sslip.io name, and this
# installer replaces the site file certbot wrote its config into.
LE_NAME=""
for candidate in "$SERVER_NAME" "$SSLIP_NAME"; do
    if [[ -n "$candidate" && -f "/etc/letsencrypt/live/$candidate/fullchain.pem" ]]; then
        LE_NAME="$candidate"
        break
    fi
done

if [[ -n "$LE_NAME" ]] && ! $SELF_SIGNED; then
    log "Using the existing Let's Encrypt certificate for $LE_NAME"
    ssl_cert="/etc/letsencrypt/live/$LE_NAME/fullchain.pem"
    ssl_key="/etc/letsencrypt/live/$LE_NAME/privkey.pem"
    template="$APP_DIR/deploy/nginx-tls.conf"
elif $SELF_SIGNED; then
    log "Issuing a self-signed certificate for $SERVER_NAME"
    mkdir -p "$TLS_DIR"
    if $IS_IP; then
        san="IP:$SERVER_NAME,DNS:$SSLIP_NAME"
    else
        san="DNS:$SERVER_NAME"
    fi
    # Regenerate only when missing, so re-running the installer doesn't
    # invalidate a certificate the operator has already trusted.
    if [[ ! -f "$TLS_DIR/dice.crt" ]]; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
            -keyout "$TLS_DIR/dice.key" -out "$TLS_DIR/dice.crt" \
            -subj "/CN=$SERVER_NAME" -addext "subjectAltName=$san" 2>/dev/null
    fi
    chmod 600 "$TLS_DIR/dice.key"
    chmod 644 "$TLS_DIR/dice.crt"
    ssl_cert="$TLS_DIR/dice.crt"
    ssl_key="$TLS_DIR/dice.key"
    template="$APP_DIR/deploy/nginx-tls.conf"
else
    template="$APP_DIR/deploy/nginx-http.conf"
fi

log "Configuring nginx for $server_names"
install -m 644 "$APP_DIR/deploy/nginx-proxy.conf" /etc/nginx/snippets/dice-proxy.conf
sed -e "s|__SERVER_NAME__|$server_names|g" \
    -e "s|__SSL_CERT__|${ssl_cert:-}|g" \
    -e "s|__SSL_KEY__|${ssl_key:-}|g" \
    "$template" > /etc/nginx/sites-available/dice
mkdir -p /var/www/html   # ACME challenge root referenced by the TLS template

# `http2 on;` only exists from nginx 1.25.1. Ubuntu 24.04 ships 1.24, where it
# is an unknown directive and fails the config test outright.
nginx_version="$(nginx -v 2>&1 | sed 's/.*nginx\///;s/[^0-9.].*//')"
if [[ "$(printf '%s\n1.25.1\n' "$nginx_version" | sort -V | head -1)" == "1.25.1" ]]; then
    sed -i 's|__HTTP2__|http2 on;|' /etc/nginx/sites-available/dice
else
    sed -i '/__HTTP2__/d' /etc/nginx/sites-available/dice
fi
# nginx refuses to start with an IPv6 listener on a host that has no IPv6.
if [[ ! -f /proc/net/if_inet6 ]]; then
    echo "   no IPv6 on this host — dropping the IPv6 listeners"
    sed -i '/listen \[::\]:/d' /etc/nginx/sites-available/dice
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
if ! curl -fsS --max-time 10 http://127.0.0.1:8000/api/health >/dev/null; then
    echo "Health check failed. Logs:" >&2
    journalctl -u dice -n 40 --no-pager >&2
    exit 1
fi

if [[ -n "$LE_NAME" ]] && ! $SELF_SIGNED; then
    echo "DICE is running at https://$LE_NAME"
elif $SELF_SIGNED; then
    echo "DICE is running at https://$SERVER_NAME"
else
    echo "DICE is running at http://$SERVER_NAME"
fi

# ---------------------------------------------------------------- next steps

if [[ -n "$LE_NAME" ]] && ! $SELF_SIGNED; then
cat <<NEXT

HTTPS is served with the Let's Encrypt certificate for $LE_NAME, and
plain HTTP redirects to it. Renewal keeps working: the ACME challenge path
stays on cleartext and the certificate is read from its /etc/letsencrypt
path, so `certbot renew` needs no further changes.

Check renewal any time with:
    certbot renew --dry-run
NEXT
elif $SELF_SIGNED; then
cat <<NEXT

The certificate is self-signed, so the browser will warn once — that is
expected. Traffic is encrypted, but for a certificate browsers accept
without a warning, and without buying a domain:

    apt-get install -y certbot python3-certbot-nginx
    certbot --nginx -d $SSLIP_NAME

$SSLIP_NAME resolves to $SERVER_NAME through the public sslip.io
wildcard DNS service, which is enough for Let's Encrypt to verify you.
The site already answers on that name.
NEXT
elif $IS_IP; then
cat <<NEXT

WARNING: this instance is plain HTTP. A Dune API key entered in the browser
crosses the network in the clear. Fix it one of these two ways:

  Real, publicly-trusted certificate (no domain purchase needed):
      apt-get install -y certbot python3-certbot-nginx
      certbot --nginx -d $SSLIP_NAME
    $SSLIP_NAME already points at $SERVER_NAME via sslip.io,
    and this site already answers on that name.

  Or encrypt immediately with a self-signed certificate:
      bash deploy/install.sh $SERVER_NAME --self-signed
NEXT
else
cat <<NEXT

Add a certificate before anyone enters a Dune API key:
    apt-get install -y certbot python3-certbot-nginx
    certbot --nginx -d $SERVER_NAME
NEXT
fi

cat <<'NEXT'

Optional password gate (DICE has no login of its own):
    apt-get install -y apache2-utils
    htpasswd -c /etc/nginx/dice.htpasswd <username>
    # uncomment the auth_basic lines in /etc/nginx/snippets/dice-proxy.conf
    systemctl reload nginx

Service management:
    systemctl status dice
    journalctl -u dice -f
NEXT
