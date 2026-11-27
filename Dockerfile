FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

FROM base AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv

ENV UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --compile-bytecode

FROM base AS final

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY zorvex.json ./
COPY src/ ./src/

ENV PATH="/app/.venv/bin:$PATH"

USER app

EXPOSE 2027

CMD ["zorvex", "serve", "--port", "2027"]
