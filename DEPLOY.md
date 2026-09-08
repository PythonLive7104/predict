# Deploying to a VPS

The stack is five containers on one host: Caddy (TLS) → nginx (SPA + routing) →
gunicorn, plus a Celery worker with embedded beat, Postgres and Redis.

## Why a VPS and not a PaaS

Celery beat has to run continuously — hourly settlement, the 08:30 generation,
the 5-minute rationale collector. Anything that scales to zero stops the
scheduler, and the slate silently never generates. That rules out free tiers on
Render/Railway and every serverless option.

## Sizing

**2 GB RAM is the floor, 4 GB is comfortable.** The host runs Postgres, Redis,
gunicorn (3 workers) and a Celery worker that imports numpy and scipy. On a 2 GB
box, drop `--workers 3` to 2 and `--concurrency 2` to 1 in `docker-compose.yml`.

## 1. Point DNS first

Create an `A` record for your domain at the server's IP **before** the first
`docker compose up`. Caddy proves control of the domain over port 80, and
failed attempts count against a Let's Encrypt rate limit.

Confirm it has propagated:

```bash
dig +short your-domain.com     # must print the server IP
```

## 2. Prepare the server

```bash
ssh root@your-server-ip

apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh          # Docker + compose plugin

# Only 80, 443 and SSH. Postgres and Redis are reachable only inside the
# compose network — never publish them.
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
```

## 3. Get the code and configure

```bash
git clone <your-repo> /opt/predict && cd /opt/predict

cp .env.example .env                 # compose-level: DOMAIN, ACME_EMAIL, DB password
cp backend/.env.example backend/.env # application-level
```

`backend/.env` must have, at minimum:

```ini
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<50+ random chars>
DJANGO_ALLOWED_HOSTS=your-domain.com
FRONTEND_URL=https://your-domain.com

TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_BOT_USERNAME=<without the @>
TELEGRAM_WEBHOOK_URL=https://your-domain.com/hooks/telegram/
TELEGRAM_WEBHOOK_SECRET=<32+ random chars, your own invention>
```

Generate the secrets on the server rather than reusing anything from a laptop:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

`DATABASE_URL` in `backend/.env` is ignored — compose builds its own from the
`POSTGRES_*` values in `./.env` and points the app at the in-cluster `postgres`
service, resolving to `postgresql://<user>:<pass>@postgres:5432/<db>`.

The `postgres` service publishes `127.0.0.1:5435` so local dev and `psql` can
reach the same database. That bind address is not decoration: `"5435:5432"`
without it publishes Postgres on every interface, which on a VPS means the open
internet. If you don't need host access in production, delete the `ports:` block
entirely — the app reaches it over the compose network either way.

## 4. Start it

```bash
docker compose up -d --build
docker compose logs -f caddy    # watch the certificate get issued
```

Migrations run automatically as part of the `web` container's command.

```bash
docker compose exec web python manage.py seed_plans
docker compose exec web python manage.py createsuperuser
```

## 5. Register the Telegram webhook

Nothing calls Telegram automatically — `TELEGRAM_WEBHOOK_URL` is a statement of
intent until this runs. Skip it and you get a bot that looks healthy and never
receives a message.

```bash
docker compose exec web python manage.py set_webhook
docker compose exec web python manage.py set_webhook --show
```

`--show` reports Telegram's own `last_error_message`, which is where a bad
certificate chain or a rejected secret token actually surfaces.

## 6. Verify

```bash
curl -I https://your-domain.com                       # 200, valid cert
curl -s https://your-domain.com/api/picks/today/      # JSON
docker compose exec web python manage.py check --deploy
docker compose ps                                     # all five up
docker compose logs worker | grep -i beat             # scheduler running
```

Then message the bot on Telegram. `docker compose logs -f worker` should show
the update arriving.

## Demo data

For a client walkthrough before real fixtures exist:

```bash
docker compose exec web python manage.py seed_demo --yes
```

**This is one-way.** `seed_demo` writes a fake league on `api_id=39` — the real
Premier League id — and every ingest is an upsert keyed on `api_id`, so real
fixtures would merge into simulated rows. Either keep this host as a throwaway
demo, or wipe the database before pointing it at live data:

```bash
docker compose down -v && docker compose up -d
```

## Operations

```bash
docker compose logs -f web worker           # tail
docker compose restart worker               # after a schedule change
docker compose down && docker compose up -d --build   # deploy a new build

# Back up the database. The `caddy-data` volume matters too — losing it
# re-requests certificates on every restart until the rate limit bites.
docker compose exec postgres pg_dump -U predict predict > backup-$(date +%F).sql
```

## After TLS is confirmed working

Enable HSTS in `backend/.env`, working upward — browsers cache it and will
refuse to connect if TLS later breaks, so it is genuinely hard to undo:

```ini
DJANGO_HSTS_SECONDS=3600      # then 86400, then 31536000
```
