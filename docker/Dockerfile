# One image for the gateway and the demo upstream; compose picks the command.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /usr/local/bin/uv

WORKDIR /app
# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev
COPY upstream ./upstream
COPY policies.yaml ./

USER nobody
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=2s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/_gw/health')"

CMD ["uvicorn", "--factory", "gateway.main:app_factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--loop", "uvloop", "--http", "httptools", \
     "--no-access-log", "--log-level", "warning", \
     "--backlog", "4096", "--timeout-keep-alive", "30"]
