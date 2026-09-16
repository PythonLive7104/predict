#!/usr/bin/env bash
#
# Deploy and verify. Run from /opt/predict on the server:
#
#     ./deploy.sh
#
# Every check below exists because that exact failure has already happened once,
# and each one was invisible from Telegram — the bot simply went quiet.
set -uo pipefail
cd "$(dirname "$0")"

BASE="$(grep -E '^FRONTEND_URL=' backend/.env | cut -d= -f2- | tr -d '[:space:]')"
BASE="${BASE:-http://localhost}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }
FAILED=0

say "Pulling"
git pull --ff-only || { bad "git pull failed — resolve that first"; exit 1; }
git log --oneline -1

say "Building"
docker compose build || { bad "build failed"; exit 1; }

# --force-recreate is not optional. Compose will happily report "Running" and
# leave the OLD container in place even when the image has changed, which is how
# a deploy silently ships nothing.
say "Restarting app containers"
docker compose up -d --force-recreate web worker frontend

# The frontend must come up AFTER web. nginx resolves the backend by name, and
# although it now re-resolves on a timer, starting in the right order means the
# first request already works instead of 502ing for ten seconds.
say "Waiting for the stack"
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$BASE/api/picks/today/" || true)"
  [ "$code" = "200" ] && break
  sleep 2
done

say "Verifying"

[ "$code" = "200" ] \
  && ok "API answering ($BASE/api/picks/today/)" \
  || bad "API returned $code — check: docker compose logs --tail=40 web"

admin="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$BASE/admin/login/" || true)"
[ "$admin" = "200" ] && ok "Admin reachable" || bad "Admin returned $admin"

# 403 is correct here: the view rejects anyone without the secret token. A 502
# means nginx cannot reach the backend, which is what silences the bot.
hook="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$BASE/hooks/telegram/" || true)"
case "$hook" in
  403) ok "Webhook endpoint live (403 = rejecting unsigned callers, correct)" ;;
  502) bad "Webhook endpoint 502 — nginx cannot reach web; the bot will be silent" ;;
  *)   bad "Webhook endpoint returned $hook" ;;
esac

# runbot deletes the webhook on startup. If anyone has run it, Telegram has
# nowhere to deliver and queues updates in silence.
#
# Checked by exit code, not by grepping prose — the previous version matched
# against the printed URL and reported a perfectly healthy webhook as missing
# for a week, which cost more trust than the check was worth.
if docker compose exec -T web python manage.py set_webhook --show >/dev/null 2>&1; then
  ok "Telegram webhook registered"
else
  bad "Telegram webhook NOT registered — fix: docker compose exec web python manage.py set_webhook"
fi

pending="$(docker compose exec -T web python manage.py set_webhook --show 2>/dev/null \
           | grep 'pending updates' | grep -oE '[0-9]+' || echo 0)"
[ "${pending:-0}" -gt 5 ] \
  && bad "$pending updates queued at Telegram — they are not being delivered" \
  || ok "No update backlog ($pending pending)"

if docker compose logs --tail=50 worker 2>/dev/null | grep -q "celery@.* ready"; then
  ok "Celery worker consuming"
else
  bad "Worker may not be ready — check: docker compose logs --tail=40 worker"
fi

queued="$(docker compose exec -T redis redis-cli llen celery 2>/dev/null | tr -d '[:space:]' || echo '?')"
[ "$queued" = "0" ] && ok "Task queue empty" || bad "$queued tasks queued — worker not keeping up"

say "Containers"
docker compose ps --format 'table {{.Service}}\t{{.Status}}'

if [ "$FAILED" -eq 0 ]; then
  printf '\n\033[32m  Deploy OK — bot and site verified.\033[0m\n\n'
else
  printf '\n\033[31m  Deploy finished with problems. See the ✗ lines above.\033[0m\n\n'
  exit 1
fi
