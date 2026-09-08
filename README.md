# Predict

AI-assisted football prediction bot (Telegram) with a shared Django core ready for
a web app and Telegram Mini App.

## Quick start

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in API_FOOTBALL_KEY, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_plans
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runbot        # bot, long-polling
```

In a second terminal (needs Redis running):

```bash
cd backend && .venv/bin/celery -A config worker -B --loglevel=info
```

## Frontend

```bash
cd frontend
npm install
npm run dev        # proxies /api to the Django dev server
```

The same build is the public web app and the Telegram Mini App — inside Telegram
it authenticates from `initData`, on the web from a stored refresh token.

## Deploy

```bash
cp backend/.env.example backend/.env   # set DJANGO_DEBUG=False + real keys
docker compose up -d --build
```

Then point Telegram at the webhook:

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://your-domain.com/hooks/telegram/" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

See [CLAUDE.md](CLAUDE.md) for architecture, the engine design, and the gotchas.
