#!/usr/bin/env sh
# Worker entrypoint. The queue it consumes is passed in, because the two
# worker services differ only in which lane they drain: the default lane is
# live traffic, the bulk lane is whatever one noisy vendor is doing.
set -e

exec celery -A config worker \
  --queues "${WORKER_QUEUES:-normalization}" \
  --hostname "${WORKER_NAME:-worker}@%h" \
  --concurrency "${WORKER_CONCURRENCY:-4}" \
  --loglevel "${WORKER_LOG_LEVEL:-INFO}"
