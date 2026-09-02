# Putting Orbit online for free

## What Orbit actually needs

Measured on a real capture, not estimated:

| Piece | Memory |
|---|---|
| PostgreSQL | 32 MB |
| Redis | 4 MB |
| MinIO | 155 MB |
| Go API | 24 MB |
| Python CV worker | 239 MB idle, **~700 MB while stitching** |
| **Total** | **~450 MB idle, ~1 GB peak** |

Two of those numbers decide where this can be hosted:

- **The worker must run all the time.** It sits on a Redis stream waiting for
  jobs. A host that sleeps idle services will never process a capture.
- **It needs ~700 MB of headroom in bursts.** Anything with a 512 MB ceiling
  will have the stitch killed halfway through.

That rules out most of the "free web service" tiers, which sleep after 15
minutes and cap memory at 512 MB.

**And the camera only works over HTTPS.** Browsers refuse `getUserMedia` on a
plain `http://` address that is not `localhost`. Any host you pick must give
you a real certificate.

---

## Recommended: one free VM

**Oracle Cloud Always Free** gives an ARM VM of **2 CPUs / 12 GB RAM / 200 GB
disk / 10 TB egress a month**, free indefinitely, no expiry. Oracle halved this
from 4 CPUs / 24 GB in mid-2026, and 12 GB is still roughly twelve times what
Orbit needs.

The whole stack runs there with `docker compose up`, exactly as it does on your
laptop. Nothing has to be re-architected.

> A payment card is required for identity verification. Always Free resources
> are not billed, but do not create anything outside the Always Free shapes.

### Steps

**1. Create the VM**

- Sign up at `cloud.oracle.com`, pick a home region near you.
- Compute → Instances → Create.
- Shape: **Ampere A1 (ARM)**, 2 OCPU, 12 GB. It must say *Always Free eligible*.
- Image: Ubuntu 24.04.
- Save the SSH private key it offers. You cannot download it again.

**2. Open the firewall**

Oracle blocks everything by default, in two places, and missing the second is
the usual reason people think their server is broken:

```bash
# on the VM
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT
sudo netfilter-persistent save
```

Then in the console: Networking → your VCN → Security List → add an ingress
rule for TCP 8080.

**3. Install Docker and Go**

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 golang-go python3-venv git
sudo usermod -aG docker $USER && newgrp docker
```

**4. Deploy**

```bash
git clone https://github.com/Vishal-2029/Orbit.git && cd Orbit
docker compose up -d
docker exec -i orbit-postgres psql -U orbit -d orbit < backend/migrations/001_init.sql
docker exec -i orbit-postgres psql -U orbit -d orbit < backend/migrations/002_quaternion.sql

cd cv-worker && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cd ..
```

Everything has ARM64 builds, including the OpenCV wheels, so nothing needs
compiling from source.

**5. Free HTTPS with Cloudflare Tunnel**

This is the part that makes the camera work. A tunnel gives you a real
certificate without buying a domain or exposing a port.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared
chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/

cloudflared tunnel login
cloudflared tunnel create orbit
cloudflared tunnel route dns orbit orbit.yourdomain.com
cloudflared tunnel run --url http://localhost:8080 orbit
```

No domain? `cloudflared tunnel --url http://localhost:8080` prints a temporary
`https://something.trycloudflare.com` address that works immediately. Good for
testing, but it changes every restart.

**6. Point the app at its public address**

The manifest builds image URLs from this, so getting it wrong shows a 360 with
broken images:

```bash
PUBLIC_BASE_URL=https://orbit.yourdomain.com
```

**7. Keep it running**

`scripts/dev.sh` is for development; it dies with your SSH session. Use systemd
so both services restart on reboot:

```ini
# /etc/systemd/system/orbit-api.service
[Unit]
After=docker.service
Requires=docker.service
[Service]
WorkingDirectory=/home/ubuntu/Orbit/backend
Environment=PUBLIC_BASE_URL=https://orbit.yourdomain.com
ExecStart=/usr/bin/go run ./cmd/api
Restart=always
User=ubuntu
[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/orbit-worker.service
[Unit]
After=docker.service
[Service]
WorkingDirectory=/home/ubuntu/Orbit/cv-worker
ExecStart=/home/ubuntu/Orbit/cv-worker/.venv/bin/python worker.py
Restart=always
User=ubuntu
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now orbit-api orbit-worker
```

Build the Go binary once (`make build`) and point `ExecStart` at
`backend/bin/api` instead, so a restart is instant.

---

## Other options, and why they do not fit

| Host | Free tier | Why not |
|---|---|---|
| **Render** | Web services + Postgres, no card | Free services **sleep after 15 min** and cap at 512 MB. The worker must stay awake, and 512 MB kills the stitch. |
| **Fly.io** | None for new accounts | No longer has a free tier. |
| **Railway** | $5 trial, then $1/month credit | Enough to try, not to run. |
| **Koyeb** | Closed to new users | Acquired in 2026, free tier withdrawn. |
| **Google Cloud Run** | Generous request-based tier | Scales to zero, so a stream consumer cannot live there without rework. |

### A managed split, if you would rather not run a VM

Free-tier managed pieces do exist:

- **Neon** or **Supabase** — PostgreSQL
- **Upstash** — Redis, with a per-command free allowance
- **Cloudflare R2** — 10 GB storage, S3-compatible, so `storage/minio.go` works
  unchanged

But **there is no free host for the CV worker**, which is the one piece that
must be always-on with ~700 MB of headroom. You would still need the VM. Given
that, running everything on it is simpler and costs the same: nothing.

---

## Read this before you make it public

**Orbit has no authentication, no rate limiting and no storage quotas.**

There is a `users` table and a `JWT_SECRET` setting, but no login flow is wired
up. Every endpoint is open. On a public URL that means anyone who finds it can
create captures, upload photos until the disk is full, and read or delete other
people's 360s.

For a link you share with a few people, add a password at the tunnel:
Cloudflare Zero Trust → Access → add an application in front of the hostname.
That is free and takes a few minutes, and it keeps the whole thing private
without touching the code.

For anything more public, the API needs real authentication, per-user quotas
and a rate limit first. See the "deliberately not built" section of
[`CHALLENGES.md`](CHALLENGES.md).

Also change these before exposing anything:

```bash
MINIO_ACCESS_KEY / MINIO_SECRET_KEY   # currently orbitadmin / orbitadmin123
POSTGRES_PASSWORD                      # currently orbit
JWT_SECRET                             # currently dev-only-change-me
```
