FROM python:3.12.14-slim-bookworm AS build
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.12.11 && uv sync --frozen --no-dev
COPY apps ./apps
COPY core ./core
COPY modules ./modules
COPY infrastructure/postgres/migrations ./infrastructure/postgres/migrations
COPY infrastructure/n8n/workflows ./infrastructure/n8n/workflows
COPY alembic.ini ./alembic.ini

FROM python:3.12.14-slim-bookworm AS runtime
# WEB_CONCURRENCY is the supported way to set the API process count: uvicorn reads it for --workers and
# create_app() reads it to refuse startup without CSRF_SIGNING_SECRET. Do not pass --workers on the CLI.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WEB_CONCURRENCY=2
WORKDIR /app
RUN groupadd --system --gid 10001 bbd && useradd --system --uid 10001 --gid bbd --no-create-home bbd && mkdir -p /data /opt/bbd-mcp && chown bbd:bbd /data && chown root:root /opt/bbd-mcp && chmod 0555 /opt/bbd-mcp
COPY --from=build --chown=bbd:bbd /app /app
USER bbd
EXPOSE 8000
CMD ["uvicorn", "apps.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio", "--http", "httptools", "--timeout-keep-alive", "15", "--timeout-graceful-shutdown", "8", "--limit-concurrency", "400", "--no-proxy-headers"]
