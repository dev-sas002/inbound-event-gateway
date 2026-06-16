#!/usr/bin/env sh
# Web entrypoint: migrate, seed on an empty database, then serve.
set -e

python manage.py migrate --noinput

# Seed only what is missing. The command keys every payload on a stable
# idempotency key, so a restart adds nothing and an existing deployment is
# untouched. Set SEED_DEMO=0 to skip it entirely.
if [ "${SEED_DEMO:-1}" = "1" ]; then
  # DEMO_ADMIN_PASSWORD empty (the default outside compose) creates no
  # account at all; compose sets one so the review console is reachable.
  python manage.py seed_demo \
    --per-vendor "${SEED_PER_VENDOR:-6}" \
    --admin-username "${DEMO_ADMIN_USERNAME:-demo}" \
    --admin-password "${DEMO_ADMIN_PASSWORD:-}"
fi

exec gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-3}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-90}" \
  --access-logfile -
