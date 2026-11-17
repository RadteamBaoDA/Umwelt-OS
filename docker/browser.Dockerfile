FROM python:3.12.14-slim-bookworm
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends iptables gosu ca-certificates && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.12.11 && uv sync --frozen --no-dev --group browser && playwright install --with-deps chromium
COPY apps ./apps
COPY core ./core
COPY modules ./modules
RUN groupadd --system --gid 10001 bbd && useradd --system --uid 10001 --gid bbd --no-create-home bbd && chown -R bbd:bbd /app /ms-playwright
COPY docker/browser-entrypoint.sh /usr/local/bin/browser-entrypoint
RUN chmod 0755 /usr/local/bin/browser-entrypoint
EXPOSE 8001
ENTRYPOINT ["/usr/local/bin/browser-entrypoint"]
