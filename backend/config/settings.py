"""
Django settings for Predict.

Section references point at CLAUDE.md (project spec). Values are read from the
environment so the same settings module serves local dev and production.

Local dev: if DATABASE_URL is unset we fall back to SQLite so the scaffold runs
without a Postgres install. Production must set DATABASE_URL to Postgres.
"""

import sys
from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    API_FOOTBALL_LEAGUES=(list, []),
    TELEGRAM_ADMIN_IDS=(list, []),
    FREE_CREDITS_ON_SIGNUP=(int, 10),
    REFERRAL_BONUS_CREDITS=(int, 5),
    MIN_PUBLISH_CONFIDENCE=(int, 55),
)

environ.Env.read_env(BASE_DIR / ".env")

# --- Core -----------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

# --- Applications ---------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",
    "django_celery_beat",
]

LOCAL_APPS = [
    "apps.common",
    "apps.accounts",
    "apps.fixtures",     # API-Football ingest: leagues, teams, fixtures, odds, injuries
    "apps.predictions",  # hybrid engine (stats baseline + LLM rationale) + settlement
    "apps.billing",      # free credits, paid tiers, crypto payments
    "apps.bot",          # Telegram surface: sessions, broadcasts, deliveries
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- Database -------------------------------------------------------------

DATABASE_URL = env("DATABASE_URL", default="")
if DATABASE_URL:
    import dj_database_url

    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- I18N / static --------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- DRF ------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}

FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:5173")
# --- Behind a TLS-terminating proxy -----------------------------------------
#
# Caddy terminates TLS and nginx forwards X-Forwarded-Proto, but Django ignores
# that header unless told to trust it — so without this it treats every request
# as plain HTTP. That silently breaks absolute URL generation (payment returns,
# Mini App links) and CSRF checks against https:// origins.
#
# Only safe because nothing reaches Django except through the proxy: the compose
# stack publishes no port for `web`. If that ever changes, a client could forge
# the header and Django would believe it.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # Caddy already redirects http->https at the edge; this covers anything that
    # reaches Django by another route.
    SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
    # HSTS is a one-way door — browsers cache it and ignore you if TLS later
    # breaks. Left off until the certificate is confirmed working, then raise it
    # (start ~3600, finish at 31536000).
    SECURE_HSTS_SECONDS = env.int("DJANGO_HSTS_SECONDS", default=0)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0

    # Affects only what Django serves — /api, /admin, /hooks — none of which is
    # ever framed. Deliberately NOT applied to the SPA, which nginx serves: the
    # Telegram Mini App runs inside an iframe on Telegram Web, and a DENY there
    # would blank the app for those users with no error anyone can see.
    X_FRAME_OPTIONS = "DENY"

CORS_ALLOWED_ORIGINS = [FRONTEND_URL]
# Telegram Mini App runs the SPA inside Telegram's webview.
CSRF_TRUSTED_ORIGINS = [FRONTEND_URL]

# --- Celery ---------------------------------------------------------------

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_TIME_LIMIT = 600
CELERY_TASK_SOFT_TIME_LIMIT = 540
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Under test, run queued tasks in-process. Several tasks fan out through
# `.delay()` (settlement triggers the accuracy rollup; generation hands off the
# rationale batch), and without this the suite silently depends on a live broker
# — it passes on a dev box with Redis up and fails in CI, which is the worst
# possible place to discover it.
if "test" in sys.argv:
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = False

# Beat schedule. Times are UTC and deliberately staggered: the fixture list is
# cheap and runs often, generation runs once the morning's team news has landed,
# and settlement chases results through the evening.
CELERY_BEAT_SCHEDULE = {
    # Cadence and horizon are env-tunable because the API-Football quota is the
    # binding constraint, not compute: every run costs
    # (active leagues x days_ahead+1) requests, and the free plan allows 100/day.
    # Default 07:00 (before 08:30 generation, so the slate is fresh) and 23:00
    # (catches the evening's results, so the hourly settlement at 23:15 grades
    # them same-day and the 04:00 ratings refresh sees them).
    "sync-fixtures": {
        "task": "apps.fixtures.tasks.sync_fixtures",
        "schedule": crontab(hour=env("API_FOOTBALL_SYNC_HOURS", default="7,23"), minute=0),
        "kwargs": {"days_ahead": env.int("API_FOOTBALL_SYNC_DAYS_AHEAD", default=3)},
    },
    "generate-daily-predictions": {
        "task": "apps.predictions.tasks.generate_daily_predictions",
        "schedule": crontab(hour=8, minute=30),
    },
    "publish-and-build-slips": {
        "task": "apps.predictions.tasks.publish_and_build_slips",
        "schedule": crontab(hour=9, minute=30),  # an hour after generation
    },
    "refresh-ratings": {
        "task": "apps.predictions.tasks.refresh_ratings",
        "schedule": crontab(hour=4, minute=0),  # after the night's results settle
    },
    # Every 5 minutes: batches usually return in minutes, and a pick that is
    # already published gains its read as soon as one is available.
    "collect-rationale-batches": {
        "task": "apps.predictions.tasks.collect_rationale_batches",
        "schedule": 300.0,
    },
    "settle-predictions": {
        "task": "apps.predictions.tasks.settle_predictions",
        "schedule": crontab(minute=15),  # hourly, offset from the fixture sync
    },
    "expire-subscriptions": {
        "task": "apps.billing.tasks.expire_subscriptions",
        "schedule": crontab(hour=3, minute=0),
    },
}

# --- API-Football ---------------------------------------------------------

API_FOOTBALL_KEY = env("API_FOOTBALL_KEY", default="")
API_FOOTBALL_BASE_URL = env(
    "API_FOOTBALL_BASE_URL", default="https://v3.football.api-sports.io"
)
# League api-ids to sync. Empty means "whatever League rows are marked active";
# non-empty narrows that further, so this is the one knob that caps request
# volume without touching the database. Read by fixtures.tasks.sync_fixtures.
API_FOOTBALL_LEAGUES = [int(x) for x in env("API_FOOTBALL_LEAGUES") if str(x).strip()]

# --- Prediction engine ----------------------------------------------------

OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
# Buy the daily slate's prose through the batch endpoint at half price. The run
# is scheduled and nobody waits on it, so the only cost is that results arrive
# after publishing — picks ship without a read and it is backfilled.
LLM_BATCH_ENABLED = env.bool("LLM_BATCH_ENABLED", default=True)
LLM_BATCH_COMPLETION_WINDOW = env("LLM_BATCH_COMPLETION_WINDOW", default="24h")
PREDICTION_MODEL = env("PREDICTION_MODEL", default="gpt-5.6-terra")
# Guard rail: never publish a pick the stats layer isn't at least this sure of.
MIN_PUBLISH_CONFIDENCE = env("MIN_PUBLISH_CONFIDENCE")

# --- Telegram -------------------------------------------------------------

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_BOT_USERNAME = env("TELEGRAM_BOT_USERNAME", default="")
TELEGRAM_WEBHOOK_URL = env("TELEGRAM_WEBHOOK_URL", default="")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", default="")
TELEGRAM_ADMIN_IDS = [int(x) for x in env("TELEGRAM_ADMIN_IDS") if str(x).strip()]

# --- Billing --------------------------------------------------------------

NOWPAYMENTS_API_KEY = env("NOWPAYMENTS_API_KEY", default="")
NOWPAYMENTS_IPN_SECRET = env("NOWPAYMENTS_IPN_SECRET", default="")
NOWPAYMENTS_BASE_URL = env("NOWPAYMENTS_BASE_URL", default="https://api.nowpayments.io/v1")
CRYPTOMUS_MERCHANT_ID = env("CRYPTOMUS_MERCHANT_ID", default="")
CRYPTOMUS_API_KEY = env("CRYPTOMUS_API_KEY", default="")

FREE_CREDITS_ON_SIGNUP = env("FREE_CREDITS_ON_SIGNUP")
REFERRAL_BONUS_CREDITS = env("REFERRAL_BONUS_CREDITS")

# --- Logging --------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"apps": {"handlers": ["console"], "level": "DEBUG" if DEBUG else "INFO", "propagate": False}},
}
