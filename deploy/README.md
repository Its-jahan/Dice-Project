# Deploying DICE

Run as root on the server:

```bash
apt-get update && apt-get install -y git
git clone <your-repo-url> /root/dice-src
cd /root/dice-src

# no domain, reached by IP — encrypted immediately
bash deploy/install.sh 203.0.113.10 --self-signed

# or, if you do have a domain
bash deploy/install.sh dice.example.com
```

Re-run the same command to deploy a later version — the script is idempotent
and will not regenerate a certificate you have already trusted.

What it sets up:

| Piece | Where |
| --- | --- |
| Code | `/opt/dice`, owned by the `dice` system user |
| Service | `dice.service` — uvicorn on `127.0.0.1:8000`, 2 workers, auto-restart |
| Config | `/etc/dice/dice.env` (mode 640, root-owned) |
| Proxy | nginx site `dice`, shared body in `snippets/dice-proxy.conf` |
| Certificate | `/etc/dice/tls/` when `--self-signed` is used |

The app never listens on a public interface; nginx is the only thing facing the
network.

## No domain? You still have two ways to get HTTPS

This matters more than it might seem: the Dune API key travels from the browser
to your server **in a request header**. Over plain HTTP, anyone who can watch
the network — the same café Wi-Fi, a hop between you and the datacentre — reads
it in the clear and can then spend your Dune credits.

### Option A — a real, publicly-trusted certificate, no domain purchase

[sslip.io](https://sslip.io) is a free public DNS service where any hostname of
the form `<your-ip>.sslip.io` resolves to that IP. Let's Encrypt is happy to
issue for such a name, because it is a real DNS name that it can verify.

`install.sh` already adds `<your-ip>.sslip.io` to the nginx `server_name`, so
this works right after install:

```bash
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d 203.0.113.10.sslip.io
```

Then use `https://203.0.113.10.sslip.io` — no browser warning, real
certificate, auto-renewing. This is the recommended route.

The trade-off: you depend on sslip.io continuing to resolve. It is a
long-running free service, but it is not yours. If it disappears, renewal
fails and you fall back to Option B or buy a domain.

### Option B — self-signed certificate on the bare IP

```bash
bash deploy/install.sh 203.0.113.10 --self-signed
```

The certificate carries the IP in its SAN, so `https://203.0.113.10` works.
Traffic is encrypted, which defeats passive eavesdropping. But the certificate
is not signed by a public CA, so:

- the browser shows a warning the first time, and you click through it;
- because you clicked through, an active attacker who can redirect your traffic
  could present *their own* certificate and you would see the same warning —
  the encryption protects you against listeners, not against impersonation.

Good enough for an instance only you use. For anything shared, prefer Option A.

Certbot cannot issue for a bare IP address, which is why these two options
exist at all.

## Decide who can reach it

DICE has no login of its own. Anyone who can open the page can run queries —
they burn *their own* Dune credits, not yours, since each user supplies their
own key, but the instance is still open to the internet. To gate it:

```bash
apt-get install -y apache2-utils
htpasswd -c /etc/nginx/dice.htpasswd <username>
# uncomment the auth_basic lines in /etc/nginx/snippets/dice-proxy.conf
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

- Disable password SSH login and use keys: `PasswordAuthentication no` in
  `/etc/ssh/sshd_config`, then `systemctl restart ssh`.
- Enable the firewall: `ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw enable`.
- Rotate the root password if it has ever been sent over an untrusted channel.
