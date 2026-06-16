from __future__ import annotations

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    DJANGO_LOG_LEVEL=(str, "INFO"),
    CELERY_TASK_ALWAYS_EAGER=(bool, False),
    CELERY_TASK_EAGER_PROPAGATES=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="unsafe-default-key")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["*"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "apps.ingestion",
    "apps.normalization",
    "apps.entities",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "config.middleware.CorrelationIdMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", default="webhooks"),
        "USER": env("POSTGRES_USER", default="webhooks"),
        "PASSWORD": env("POSTGRES_PASSWORD", default="webhooks"),
        "HOST": env("POSTGRES_HOST", default="postgres"),
        "PORT": env.int("POSTGRES_PORT", default=5432),
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
        "ATOMIC_REQUESTS": False,
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": env("DRF_ANON_THROTTLE", default="1200/min")},
}

SPECTACULAR_SETTINGS = {
    "TITLE": "AI Webhook Ingestion Platform API",
    "DESCRIPTION": (
        "Webhook ingestion and AI normalization platform APIs. "
        "Use `POST /api/webhooks/` to submit vendor payloads for async normalization."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

OPENAI_API_KEY = env("OPENAI_API_KEY", default="")

# Which normaliser to use: auto | openai | rules.
# "auto" follows the presence of an API key, so a clone with no key still runs
# the full pipeline through the rule-based normaliser.
NORMALIZATION_BACKEND = env("NORMALIZATION_BACKEND", default="auto")
OPENAI_MODEL = env("OPENAI_MODEL", default="gpt-4.1-mini")
OPENAI_TIMEOUT_SECONDS = env.int("OPENAI_TIMEOUT_SECONDS", default=30)
NORMALIZATION_PROMPT_VERSION = env("NORMALIZATION_PROMPT_VERSION", default="v1")
LOW_CONFIDENCE_THRESHOLD = env.float("LOW_CONFIDENCE_THRESHOLD", default=0.7)

# Page size for the review and low-confidence listings. Capped so a caller
# cannot ask for the whole table in one request.
DEFAULT_PAGE_SIZE = env.int("DEFAULT_PAGE_SIZE", default=50)
MAX_PAGE_SIZE = env.int("MAX_PAGE_SIZE", default=200)


def _parse_signing_secrets(raw: str) -> dict[str, str]:
    """
    Parse `vendor=secret,other=secret` into a mapping.

    Split once per pair so a base64 secret containing "=" survives, and keep
    the vendor key lowercase because vendors are not consistent about casing.
    Use `*` as the vendor to require one shared secret from everybody.
    """
    secrets: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        vendor, secret = pair.split("=", 1)
        vendor, secret = vendor.strip().lower(), secret.strip()
        if vendor and secret:
            secrets[vendor] = secret
    return secrets


# Webhook authenticity. Empty means no vendor is verified, which is what keeps
# the service runnable with no configuration; a vendor listed here must send a
# matching X-Webhook-Signature or its requests are refused.
WEBHOOK_SIGNING_SECRETS = _parse_signing_secrets(env("WEBHOOK_SIGNING_SECRETS", default=""))
# When true, a vendor with no configured secret is refused rather than trusted.
WEBHOOK_REQUIRE_SIGNATURE = env.bool("WEBHOOK_REQUIRE_SIGNATURE", default=False)

# The cache backs the vendor-profile lookup and the arrival-rate counter. Both
# tolerate a cold or unavailable cache: the profile is re-read from the
# database, and the rate counter falls back to "not bursting", which is the
# behaviour this service had before routing existed. LocMemCache is the default
# so the test suite and `manage.py` need no Redis.
CACHE_URL = env("CACHE_URL", default="")
if CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": CACHE_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "webhook-platform",
        }
    }

REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_TASK_ALWAYS_EAGER = env("CELERY_TASK_ALWAYS_EAGER")
CELERY_TASK_EAGER_PROPAGATES = env("CELERY_TASK_EAGER_PROPAGATES")
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_DEFAULT_RETRY_DELAY = 2
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=120)
CELERY_TASK_SOFT_TIME_LIMIT = env.int("CELERY_TASK_SOFT_TIME_LIMIT", default=90)
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# Two lanes, not one. A vendor whose arrival rate crosses NOISY_VENDOR_BURST
# inside NOISY_VENDOR_WINDOW_SECONDS has its work routed to the bulk lane,
# which has its own worker, so its burst cannot monopolise the pool every other
# vendor is waiting on. See apps/ingestion/routing.py.
NORMALIZATION_QUEUE = env("NORMALIZATION_QUEUE", default="normalization")
NORMALIZATION_BULK_QUEUE = env("NORMALIZATION_BULK_QUEUE", default="normalization.bulk")
NOISY_VENDOR_BURST = env.int("NOISY_VENDOR_BURST", default=200)
NOISY_VENDOR_WINDOW_SECONDS = env.int("NOISY_VENDOR_WINDOW_SECONDS", default=60)

# The default route. Dispatch sites that choose a lane pass `queue=` and
# override this; anything that does not lands here.
CELERY_TASK_ROUTES = {
    "apps.normalization.tasks.process_raw_webhook": {"queue": NORMALIZATION_QUEUE},
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "config.logging.JsonFormatter",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        }
    },
    "root": {
        "handlers": ["console"],
        "level": env("DJANGO_LOG_LEVEL"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": env("DJANGO_LOG_LEVEL"),
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": env("DJANGO_LOG_LEVEL"),
            "propagate": False,
        },
    },
}
