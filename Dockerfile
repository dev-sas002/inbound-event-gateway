# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Build stage: resolve and install dependencies, then throw the toolchain away.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# build-essential is needed to compile wheels that have no binary for this
# platform. It exists only in this stage; the runtime image never sees it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Dependencies are installed from the lock file before the source is copied, so
# editing a Python file does not invalidate this layer. The lock file is used
# rather than re-resolving, so an image built today installs the versions the
# project was tested against.
COPY pyproject.toml uv.lock README.md ./
# Dev dependencies are included so the suite can be run inside the image:
# `docker compose exec web python -m pytest`.
RUN uv sync --no-install-project --extra dev

# ---------------------------------------------------------------------------
# Runtime stage: the interpreter, the virtualenv, the source, and nothing else.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings \
    # /app stays owned by root and read-only to the app user, which is the
    # point of running unprivileged. The dev tools want scratch space, so they
    # are pointed at /tmp rather than the source tree being made writable.
    RUFF_CACHE_DIR=/tmp/ruff-cache \
    PYTEST_ADDOPTS="-p no:cacheprovider"

# A non-root user, created before the copy so the files land owned by it.
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app . /app

# Static files are collected at build time rather than on every container
# start: the manifest storage hashes every file, which is slow, and doing it
# here means the runtime filesystem can stay read-only to the app user.
RUN DJANGO_SECRET_KEY=build-time-only python manage.py collectstatic --noinput \
    && chown -R app:app /app/staticfiles \
    && chmod +x /app/scripts/*.sh \
    # Gunicorn 26 opens a control socket under the working directory. The app
    # user cannot create it in a root-owned /app, and the failure is logged at
    # ERROR on every boot, so the directory is created here instead.
    && mkdir -p /app/.gunicorn && chown app:app /app/.gunicorn

USER app

EXPOSE 8000

# Answers on the same endpoint compose polls, so a container that is unhealthy
# to Docker is unhealthy to a load balancer too.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/', timeout=4).status == 200 else 1)"

CMD ["/app/scripts/start-web.sh"]
