# Deploying DICE

One command on the server, run as root:

```bash
apt-get update && apt-get install -y git
git clone <your-repo-url> /root/dice-src
cd /root/dice-src
bash deploy/install.sh your-domain.example
```

Re-run the same command to deploy a later version — the script is idempotent.

What it sets up:

| Piece | Where |
| --- | --- |
| Code | `/opt/dice`, owned by the `dice` system user |
| Service | `dice.service` — uvicorn on `127.0.0.1:8000`, 2 workers, auto-restart |
| Config | `/etc/dice/dice.env` (mode 640, root-owned) |
| Proxy | nginx site `dice`, 900s timeouts for slow Dune executions |

The app itself never listens on a public interface; nginx is the only thing
facing the network.

## Do these two things after the first install

**1. TLS.** The Dune API key travels from the browser to your server in a
request header. Over plain HTTP anyone on the path can read it.

```bash
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d your-domain.example
```

Certbot needs a real domain — it cannot issue a certificate for a bare IP
address. If you only have an IP, point a domain at it first; until then, treat
the deployment as private and don't enter a real key.

**2. Decide who can reach it.** DICE has no login of its own. Anyone who can
open the page can run queries — they burn *their own* Dune credits, not yours,
since each user supplies their own key, but the instance is still open to the
internet. To gate it:

```bash
apt-get install -y apache2-utils
htpasswd -c /etc/nginx/dice.htpasswd <username>
# uncomment the two auth_basic lines in /etc/nginx/sites-available/dice
systemctl reload nginx
```

## API keys on the server

`/etc/dice/dice.env` ships with **no** `DUNE_API_KEY`, which is the right
default: each user pastes their own key into the UI and the server never holds
one. Set `DUNE_API_KEY` there only for a private single-user instance — and
note that it then becomes the fallback for *every* request that arrives without
a key header, including from anyone else who can reach the site.

## Operating

```bash
systemctl status dice
journalctl -u dice -f
systemctl restart dice
```

Health check: `curl http://127.0.0.1:8000/api/health`.

## Server hardening worth doing

- Disable password SSH login and use keys:
  `PasswordAuthentication no` in `/etc/ssh/sshd_config`, then
  `systemctl restart ssh`.
- Enable the firewall: `ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw enable`.
- Rotate the root password if it has ever been sent over an untrusted channel.
