# Predict — AI football prediction bot + web app

## Positioning (read this before writing user-facing copy)

The product sells **calibrated confidence with a public record**, not certainty.
No pick is ever described as "sure", "guaranteed", "banker", or "100%". Best-in-class
football models top out around 55-60% on 1X2; claiming otherwise gets the bot
mass-reported on Telegram, gets the payment processor to drop the merchant, and
churns users the first bad weekend.

Both of the client's own references take this line — ClawSportBot markets
"we do not predict, we verify", Tikitaka ships a confidence percentage and
publishes accuracy. Every published pick here is graded and rolled into
`AccuracySnapshot`, and the bot's **📊 Our Record** view shows winners and losers
alike, including ROI at level stakes (a 70% strike rate at short prices can still
lose money — say so).

## Architecture

One Django core, two front doors.

```
Django 5 + DRF ──┬─ Telegram bot (aiogram 3)      ← phase 1, primary surface
  Celery + Redis  ├─ React/Vite SPA (phase 3) ─┬─ public web app
  Postgres        │                            └─ Telegram Mini App (same build)
                  └─ Django admin / ops
```

The bot is a **thin client**. Every entitlement decision (can this user see this
pick, what does this plan cost, is the subscription current) lives in
`apps.billing` / `apps.predictions` and is reached through `apps/bot/services.py`,
so the phase-3 web app and Mini App get identical answers without reimplementing
the rules. Handlers are presentation only.

## Frontend

One React build serves both surfaces. `src/telegram.js` decides which: `initData`
is non-empty **only** inside the Telegram webview, so it doubles as the
environment check and the credential — there is no separate "am I in Telegram"
flag to keep in sync. In Telegram the app posts initData to
`/api/auth/telegram/` and gets the same JWT pair the public web app uses.

- `src/api.js` — access token in memory, refresh in localStorage (wrapped in
  try/catch: private mode throws). A 401 triggers one refresh-and-retry, then
  logs out rather than looping.
- `src/styles.css` — tokens on bare `:root`, redefined under both
  `prefers-color-scheme: dark` and `[data-theme="dark"]`, so the Telegram theme
  and the system preference both work.
- Routes render only after the auth attempt settles. Rendering earlier lets a
  page fetch before the token exists, and a paying subscriber's first view of the
  slate comes back fully locked.

## Apps

| App | Responsibility |
|---|---|
| `apps.fixtures` | API-Football ingest: leagues, teams, fixtures, odds, injuries. Every row keeps `api_id` (idempotent upserts) and `raw` (re-derive features without re-paying for the call). |
| `apps.predictions` | The hybrid engine, settlement, and the accuracy rollup. |
| `apps.billing` | Plans, subscriptions, credit ledger, crypto payments. |
| `apps.bot` | Telegram surface: sessions, keyboards, handlers, broadcasts, deliveries. |
| `apps.accounts` | Telegram-first user model; referral codes. |

## The prediction engine

`apps/predictions/engine/` — three files, one direction of flow:

1. **`pipeline.build_stat_pack()`** freezes everything the engine may reason over.
   Whatever isn't in the pack didn't influence the pick.
2. **`poisson.py`** — Dixon-Coles-adjusted bivariate Poisson over team
   attack/defence strengths, blended with an Elo prior (`ELO_BLEND = 0.30`,
   because Poisson strengths are noisy early in a season). Produces the score
   matrix; every market we sell is a sum over its cells. **This owns the numbers.**
3. **`llm.py`** — an LLM reads the same pack and writes the user-facing read,
   flagging context the model structurally cannot see (cup final, manager sacked
   Thursday, keeper ruled out after the data pull). **It never sets the
   probability** and never overrides the selection — disagreement goes in
   `caveats`. A failure here is non-fatal: a pick with no prose still ships.
   Because this layer owns no numbers, the provider is a cost-and-voice choice,
   not a modelling one. It currently runs on OpenAI's Responses API
   (`OPENAI_API_KEY`, `PREDICTION_MODEL`); swapping providers touches this one
   file and nothing else in the stack calls an LLM. Reasoning tokens bill as
   output and count against `MAX_OUTPUT_TOKENS`, so a long think can truncate the
   JSON — that surfaces as `status == "incomplete"` and is raised, never handed
   to `json.loads`.

Each published `Prediction` freezes `stat_snapshot` and `engine_version`, so the
record stays auditable and a regression is attributable to a version.
`MIN_PUBLISH_CONFIDENCE` (default 55) is a hard gate — below it, a pick is stored
but never published.

**Refitting:** `RHO = -0.1` (Dixon-Coles low-score correction) and `ELO_BLEND` are
empirical defaults. Refit per-league once there's a season of settled predictions.

## Ratings — what feeds the engine

`apps/predictions/engine/ratings.py` maintains the two inputs `poisson.py` reads.
Without it every team looks identical and every pick is a coin flip with home
advantage, so this runs nightly (04:00, after results settle).

- **Attack / defence strengths** are goal-rate ratios against the *league* average
  (1.20 attack = scores 20% more than the average side in that league).
  Exponentially time-decayed (`HALF_LIFE_DAYS = 60`), then **shrunk toward 1.0**
  in proportion to how few games a team has (`MIN_GAMES_FOR_FULL_WEIGHT = 8`).
  The shrinkage is what stops one 6-0 making a promoted side look like Barcelona,
  and the `MIN_STRENGTH`/`MAX_STRENGTH` clamp is the backstop.
- **Venue splits** (`home_attack_strength` and friends) are the same ratios
  computed over home and away fixtures separately, because a side that is a
  fortress at home and dreadful away averages into mush under one rating — and
  that is exactly the fixture the model most needs to get right. Two stages of
  shrinkage: the overall ratio toward 1.0, then each venue ratio toward *that
  team's own overall rating* (`MIN_VENUE_GAMES_FOR_FULL_WEIGHT = 5`). Shrinking
  the split toward the league instead would discard the team information we
  already have.
- **Home advantage is measured, not assumed.** `league_venue_averages` returns
  goals-per-game for home and away sides separately, and each venue strength is
  normalised against its own side of that pair. The strengths stay centred on
  1.0 and the home edge lives in the baseline, per league. This is why
  `poisson.expected_goals` **ignores** `home_advantage` in venue mode: applying
  the old flat 1.15 on top would count the home edge twice.
- **Elo** reacts faster and stabilises thin goal samples. Margin of victory is
  dampened with `log(margin + 1)` — a 4-0 says more than a 1-0, but not four times
  more. It is **path-dependent**, so `refresh_all` applies pending fixtures in
  chronological order and `Fixture.elo_applied` guarantees each result counts once.

Neither reads a `Prediction`, so ratings can never learn from our own picks.

## Publishing

Generation prices every fixture; `apps/predictions/publishing.py` decides what
ships. The **best** picks go free, deliberately — the free tier is the shop window
*and* what the public record is judged on. One free pick per fixture, so the slate
shows breadth rather than four angles on one match.

Slips (`build_slips`) are what actually convert. Two rules that matter:
**one leg per fixture** (correlated legs make the combined odds lie about the real
risk) and **no padding** — a thin slate legitimately produces no slip, and
inventing one to hit a quota is how a record gets ruined. A slip settles as lost
the moment any leg loses; nothing later can save it.

## Money

Credits are a **ledger** (`CreditEntry`), not a mutable counter — `Wallet.balance`
is a cache the ledger can rebuild. Disputes in this niche are constant ("the bot
ate my credits") and only a ledger settles them.

Payments are crypto (NOWPayments primary, Cryptomus fallback). A plan is granted
**only** from a signature-verified IPN (`apps/bot/webhooks.py` → `settle_payment`),
never from the client claiming it paid. Settlement is idempotent — providers retry
IPNs, and `Payment.status == PAID` is the lock. Renewals extend from the current
expiry, not from now, so renewing early loses nothing.

## Scheduling

Celery beat (embedded in the worker via `-B`), staggered in UTC:

| Task | When | Why then |
|---|---|---|
| `sync_fixtures` | 07:00, 23:00 | 07:00 lands the slate before generation; 23:00 catches the evening's results in time for settlement and the 04:00 ratings refresh. Hours and horizon are env-tunable (`API_FOOTBALL_SYNC_HOURS`, `API_FOOTBALL_SYNC_DAYS_AHEAD`) because the API-Football quota, not compute, is what this task runs out of. |
| `generate_daily_predictions` | 08:30 | After the morning's team news lands. Makes no API calls in batch mode — it prices the slate and chains `submit_rationale_batch`. |
| `collect_rationale_batches` | every 5 min | Backfills prose onto already-published picks as soon as a batch returns. |
| `settle_predictions` | hourly at :15 | Offset from the fixture sync. Triggers `rebuild_accuracy`. |
| `expire_subscriptions` | 03:00 | Keeps stored status from drifting from `is_current`. |

**Prose is bought asynchronously** (`LLM_BATCH_ENABLED`, default on): the batch
endpoint is half price and the daily run is the ideal workload for it — scheduled,
with nobody waiting on the output. The cost is that results are only guaranteed
within the completion window, so 09:30 publishing ships whatever prose exists and
`collect_rationale_batches` fills the rest in. That is the existing "a pick with
no prose still ships" rule, extended in time rather than a new compromise.

`Prediction.rationale_batch` is what makes it safe to retry. A pick is claimed
only *after* the provider accepts the batch (claiming first would strand it if
submission threw), and a batch that ends `failed`/`expired`/`cancelled` calls
`release_predictions()` to hand its picks back. Without that release the FK stays
set, `predictions_awaiting_prose()` keeps skipping them, and those picks are mute
forever.

`rebuild_accuracy` recomputes rollups wholesale — cheap enough, and it means a
corrected result can never leave a stale win-rate on the site.

## Notifications

Push is the retention engine — a prediction bot nobody hears from is dead — and
it is also the fastest way to get banned. Two rules, both enforced in
`apps/bot/notifications.py`:

1. **Never send to someone who opted out or blocked us.** `audience()` filters on
   `notifications_enabled` and `is_blocked`. `is_blocked` is set *only* from
   Telegram's own 403 ("bot was blocked" / "user is deactivated") in
   `deliver_broadcast` — never inferred from silence.
2. **Never send the same thing twice.** `Delivery` is unique per
   (user, broadcast), so a retried Celery task re-sends to nobody.

`publish_and_build_slips` queues two pushes:

| Push | Audience | Contents |
|---|---|---|
| Free picks | everyone reachable | fixture, market, confidence — **never the selection**, or nobody spends a credit |
| Banker of the Day | current VIP subscribers | the actual legs; they've paid for them |

Only the banker is pushed, not every slip — one notification per slip is spam.
The opt-out is reachable from **My Plan** in the bot and `PATCH /api/me/` on the
web; `notifications_enabled` is the only field that endpoint will accept, since
everything else on the profile is derived from subscriptions.

## Backtesting

`manage.py backtest` replays settled fixtures **in chronological order**, pricing
each with ratings built only from earlier results. That ordering is the point:
rating a match with strengths that already contain its result is lookahead bias,
and it yields a backtest that looks brilliant and predicts nothing.

Chronological order alone does not deliver that. Both ways it leaked were
invisible in the output — the number just got better:

1. **`update_strengths` must be bounded by `as_of`.** During a replay every
   fixture in the table is already `FINISHED`, so an unbounded refresh rates a
   match using results that had not happened yet. `as_of` bounds the window, the
   league averages and the recency decay to one instant. Production passes
   nothing, where nothing later is `FINISHED` anyway.
2. **The fixture row must be re-read before pricing.** `select_related` caches
   `Team` rows on each fixture at list-build time — *before* the command resets
   the ratings — so pricing off those cached objects reads the previous run's
   end-of-season strengths. `as_of` cannot help here: the numbers never come
   from a query. The tell is that re-running the command changes the answer,
   which is what `predictions/tests/test_backtest.py` pins.

The command warns when overall accuracy clears 65%, because that almost always
means the fixtures were simulated rather than real — `manage.py seed_demo`
generates results from a Poisson process, which is exactly what the model
assumes, so the engine ends up graded against its own assumptions. **Only a
backtest over real API-Football history is evidence of anything.**

## Gotchas

- **API-Football returns HTTP 200 with an `errors` object** for quota and parameter
  problems. `ApiFootballClient._get` raises on it; don't bypass the client. An
  exhausted daily quota arrives this way too, so overspending does not look like
  overspending — it reads as a parameter bug. `sync_fixtures` logs its request
  count for exactly this reason.
- **Request volume is `leagues x (days_ahead + 1) x runs-per-day`**, and the free
  plan allows 100/day. `API_FOOTBALL_LEAGUES` narrows the active leagues
  (`fixtures.tasks.leagues_to_sync`) and is the cheapest lever — one league
  removed is `days_ahead + 1` requests saved every run. It narrows the active set
  rather than overriding it: a league with `is_active=False` stays unsynced even
  when listed.
- **Telegram flood limits** (~30 msg/s to distinct chats) will ban a bot outright.
  `deliver_broadcast` sleeps 0.05s per send and records a `Delivery` per user under
  a unique constraint, so a retried task re-sends to nobody.
- **aiogram is async, the Django ORM is not.** Everything in `apps/bot/services.py`
  is wrapped in `sync_to_async`. Handlers must not touch the ORM directly.
- **Only `Odds` on the 2.5 line** are stored for over/under; other lines are dropped
  in `_map_selection`.
- **"Today" is the UTC calendar date.** `publish_daily`, `todays_free_picks` and
  `build_slips` all key on `fixture__kickoff__date`. This is consistent across the
  stack, but a late kickoff in the Americas falls on the next UTC date — revisit if
  the audience moves west.
- **`Prediction.tier` defaults to FREE on unpublished rows.** Any query filtering by
  tier must also require `published_at__isnull=False`, or it counts picks that never
  went out. This bit the tests; it will bite the API too.
- **`values_list(...).distinct()` does not dedupe on `Prediction`.** The model has
  `Meta.ordering`, so Django adds the ORDER BY column to the SELECT and DISTINCT
  operates on `(field, kickoff)` pairs. Use `.order_by().values(...).annotate(...)`
  — see `access.record_by_market`.
- **Double chance never offers the `12` pair.** Mechanically "home or away" wins
  whenever the draw is least likely, which for a heavy favourite it often is — but
  it is a bet *against a draw*, not a safer way to back the favourite, and it
  renders as nonsense ("Man City or Burnley").
- **`telegram-web-app.js` defines `window.Telegram.WebApp` on any page**, reporting
  `colorScheme: "light"` outside the client. Gate anything Telegram-specific on
  `isMiniApp()` (non-empty `initData`), not on the object existing.
- **VIP audience is filtered in Python, not re-expressed as a query.** `audience()`
  walks `current_subscription()` so "is this subscription current" has exactly one
  definition. Rewriting it as a `.filter(subscriptions__status="active")` silently
  includes lapsed rows whose status hasn't been swept yet.
- **`settle_payment` resolves the IPN by `order_id`, then `payment_id`** — both
  guarded against empty values. Filtering `provider_ref=""` would match any
  un-invoiced payment and credit the wrong user.
- **`refresh_from_db()` does not refresh `select_related` relations** whose FK
  value is unchanged, so `fixture.home` keeps the `Team` instance loaded with the
  original queryset. Anything that resets or recomputes team ratings after
  building a fixture list reads stale numbers until the row is re-read. This
  silently inflated the backtest by ~10 points.

## Commands

```bash
cd backend
.venv/bin/python manage.py seed_plans          # create the plan tiers
.venv/bin/python manage.py seed_demo --yes     # demo season + today's slate (DEV)
.venv/bin/python manage.py backtest --warmup 30 # walk-forward accuracy report
.venv/bin/python manage.py runbot              # long-polling, DEV ONLY
.venv/bin/celery -A config worker -B -l info

cd ../frontend && npm run dev                  # SPA + Mini App, proxies /api
```

Production runs the bot via webhook (`hooks/telegram/`) so updates land on Celery
instead of holding a process open. `runbot` is for local dev, where no public URL
exists.

Deployment is `docker compose up` on one host — see `DEPLOY.md`. Caddy terminates
TLS in front of the frontend nginx; `web` and `worker` publish no ports, which is
what makes `SECURE_PROXY_SSL_HEADER` safe to trust. Two things bite on a first
deploy: `TELEGRAM_WEBHOOK_URL` is inert until `manage.py set_webhook` runs (a bot
that looks healthy and receives nothing), and Telegram reports webhook failures
only to itself — `set_webhook --show` is the one place a bad certificate chain or
a rejected secret token becomes visible.
