# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder stage – compile/install deps with uv on Alpine/musl
# ---------------------------------------------------------------------------
FROM python:3.14-alpine AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Build-time tools needed as a fallback when a package has no musllinux wheel
# and must be compiled from source (e.g. lxml, cryptography).
# These are NOT copied to the final image.
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    libxml2-dev \
    libxslt-dev

# Install Python dependencies first (cached layer – only re-runs when the
# lock file or pyproject.toml changes, not on every source edit).
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
# Runtime stage – minimal Alpine, no compiler toolchain
# ---------------------------------------------------------------------------
FROM python:3.14-alpine

# libgcc  : provides libgcc_s.so.1, required at runtime by pydantic-core and
#           qh3 (both Rust/C extensions that link against it).
# ca-certificates : system trust store for outgoing HTTPS connections.
RUN apk add --no-cache \
    libgcc \
    ca-certificates

# Non-root user (Alpine busybox addgroup/adduser syntax).
RUN addgroup -S appuser \
 && adduser  -S -G appuser -H appuser

WORKDIR /app

# Bring the fully-populated venv and application source from the builder.
COPY --from=builder --chown=appuser:appuser /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    TZ=UTC

USER appuser

CMD ["python", "-m", "immich_calendar_album"]
