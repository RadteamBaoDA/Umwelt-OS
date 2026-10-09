# Native collectors and optional n8n runbook

Native RSS, API and provider collection needs only API, worker, PostgreSQL and Redis. n8n and the browser sidecar are optional; their absence never degrades native sources or the `overall` health status.

## Stacks

| Need | Command |
| --- | --- |
| Native base | `docker compose up -d --build` |
| Browser collection | `docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d --build` |
| n8n + browser (legacy) | `docker compose -f docker-compose.yml -f docker-compose.connectors.yml up -d --build` |

`docker-compose.connectors.yml` pulls in the browser overlay with `include:`, which needs Docker Compose >= 2.20 (`docker compose version`).

## Keys

- `CONNECTOR_CREDENTIAL_ENCRYPTION_KEY`: needed only to store provider credentials. Create: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- `N8N_API_KEY`: created in the n8n UI (Settings > n8n API); it is not a random value you choose.
- `N8N_ENCRYPTION_KEY`: keep it persistent; changing it makes n8n's stored credentials unreadable.
- `BROWSER_SHARED_TOKEN`: only for browser collection; same value for API and browser containers.

## Health

`GET /api/v1/system/health` reports `n8n` and `browser` as `optional` with `not_configured` or `configured` (a key/token is set; connectivity is not tested). Only postgres, redis, worker and chat worker affect `overall`.

## Scheduler switch

`COLLECTOR_SCHEDULER_ENABLED=false` stops the scheduler from dispatching due collections; manual collect is unaffected.

## Validate, migrate, roll back

- Dry-run the merged model without starting anything: `docker compose -f docker-compose.yml -f docker-compose.connectors.yml config -q`.
- Migrate with the normal release procedure (`docs/deployment.md`). Rollback of a source from n8n to native does not delete installed n8n data; n8n optional means no functional dependency for native sources, not automatic removal.

## 429 troubleshooting

A provider 429 sets a retry-after on the source; the scheduler honours it and does not retry sooner. Lower the poll interval frequency, check the provider's free-tier window in `provider-catalog.md`, and avoid sharing one API key across several sources.

## n8n licence note

n8n is distributed under the Sustainable Use License, not an OSI open-source licence. Check the official n8n FAQ before using it for commercial or hosted offerings. Umwelt-OS ships no n8n code; native collectors remove the need for it.
