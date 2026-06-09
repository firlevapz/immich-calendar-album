# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder stage – install deps with uv inside the project venv
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

# Copy the uv binary from the official image (avoids a separate pip install).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install dependencies first (cached layer – only re-runs when lock file changes).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Copy source and install the package itself.
COPY src/ ./src/
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---------------------------------------------------------------------------
# Final runtime stage – no uv, no build tools
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm

# Non-root user for security.
RUN groupadd --system --gid 999 appuser \
 && useradd  --system --gid 999 --uid 999 --no-create-home appuser

WORKDIR /app

# Bring the fully-populated venv and application source from the builder.
COPY --from=builder --chown=appuser:appuser /app /app

# Put the venv on PATH.
ENV PATH="/app/.venv/bin:$PATH"

# Respect TZ at runtime (set via docker-compose env or env_file).
ENV TZ=UTC

USER appuser

CMD ["python", "-m", "immich_calendar_album"]
