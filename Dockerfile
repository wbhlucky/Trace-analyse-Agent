# syntax=docker/dockerfile:1

# ---------- base: uv + Python, build standard ----------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1
WORKDIR /app

# Dependency layer is copied first so it only rebuilds when the lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --no-install-project --extra claude --no-dev

# Source and project assets (skills / trace_streamer are force-included by hatch)
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra claude --no-dev

# ---------- runtime: only .venv + project, non-root ----------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home app

WORKDIR /app
COPY --from=build --chown=app:app /app /app

USER app
ENTRYPOINT ["diting-agent"]
CMD ["--help"]