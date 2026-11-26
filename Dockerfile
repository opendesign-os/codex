# Build from the directory holding both checkouts, since zorvex comes from ../zorvex:
#   docker build -f codex/Dockerfile ..
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root runtime user. Codex runs in the threads' sandboxes, not here.
RUN addgroup --system app && adduser --system --ingroup app --home /home/app app

# -----------------------------
# Builder stage: the dependencies, as uv.lock pins them
# -----------------------------
FROM base AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# pyproject.toml takes zorvex-cli, and zorvex-api with it, from ../zorvex/libs, relative to /app.
COPY zorvex/libs/zorvex-api /zorvex/libs/zorvex-api
COPY zorvex/libs/zorvex-cli /zorvex/libs/zorvex-cli

WORKDIR /app

COPY codex/pyproject.toml codex/uv.lock ./
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

# -----------------------------
# Runtime stage: zorvex.json and the code it points to (./src)
# -----------------------------
FROM base AS final

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY codex/zorvex.json .
COPY codex/src/ ./src/

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 2027

USER app

CMD ["zorvex", "serve"]
