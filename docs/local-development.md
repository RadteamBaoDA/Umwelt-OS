# Local development guideline

How to run Umwelt-OS on your own machine or LAN **without HTTPS**, and how that differs from a public deployment. For public hosting see `docs/deployment.md`.

## 1. Modes (`BBD_ENVIRONMENT`)

| Value | When | Effect |
|---|---|---|
| `auto` (default when unset) | Local machine with real secrets, or any server | `development` transport rules if `PUBLIC_ORIGIN` host is local (http and non-Secure cookies allowed, `/docs` on), otherwise `production`. **Placeholder secrets and `bbd:bbd` are still rejected** |
| `development` (`dev`) | Explicit local-only opt-in (what `.env.example` ships) | Everything `auto` relaxes, plus template/placeholder secrets and the default `bbd:bbd` database are accepted |
| `test` | pytest / disposable test stack | Same relaxations as development |
| `production` | Force strict mode | See table below |

Any other value fails closed to `production`.

"Local host" means: `localhost`, `*.localhost`, loopback (`127.x`, `::1`), private/link-local IPs (`10.x`, `172.16-31.x`, `192.168.x`, `169.254.x`), `*.local`, `*.lan`, `*.internal`, `*.home.arpa`, or a single-label name (for example a container name such as `web`). Everything else counts as a public domain.

| Check (API refuses to start when violated) | development / test | production, local origin | production, public domain |
|---|---|---|---|
| `PUBLIC_ORIGIN` must be `https://` | no | no | **yes** |
| `SECURE_COOKIES=true` when origin is https | no | yes | **yes** |
| No `replace-with-*` / `replace-this-*` secrets | no | yes | **yes** |
| No default `bbd:bbd` database credentials | no | yes | **yes** |

(Columns 2-4 above apply to explicit `development`/`test`, then to `production` or `auto` with a local origin, then to `production` or `auto` with a public domain. `auto` with a local origin therefore rejects placeholders even though it allows http.)
| `/docs`, `/redoc`, `/openapi.json` served | yes | no | no |

The frontend follows `NODE_ENV`: `npm run dev` (development) skips `Strict-Transport-Security` and allows the dev-only `unsafe-eval` and websocket CSP sources; `next build`/`next start` (production) sends HSTS. Browsers ignore HSTS over plain http, so a LAN http address keeps working.

## 2. First run (Docker, recommended)

```bash
make setup      # scripts/ensure_env.py: creates/completes .env with generated secrets
make dev        # hot-reload stack; Windows: ./scripts/dev.ps1 dev
make migrate    # applies Alembic migrations (head: r18_auth_session_last_seen)
```

`make setup` writes a working `.env` for local use: `PUBLIC_ORIGIN=http://localhost:3000`, `SECURE_COOKIES=false`, plus every generated secret (real values, so the default `auto` mode accepts them) (database, Redis, graph, browser, n8n, both Fernet keys, `SETUP_TOKEN`, `CSRF_SIGNING_SECRET`). It never overwrites values you already set. Open `http://localhost:3000`, complete owner setup with the `SETUP_TOKEN` from `.env`.

Only loopback ports are published (`127.0.0.1`): web `3000`, Postgres `5432`, Redis `6379`, graph `7687`, n8n `5678`. Nothing is reachable from other machines by default.

## 3. Reach it from another device on your LAN (still http)

The web port binds to `127.0.0.1`, so a phone or second PC cannot connect until you change both the bind and the origin:

1. Edit `docker-compose.yml` web ports from `"127.0.0.1:${WEB_PORT:-3000}:3000"` to `"0.0.0.0:${WEB_PORT:-3000}:3000"` (or your LAN IP), or put a reverse proxy in front.
2. Set `PUBLIC_ORIGIN` to the **exact** URL you type in the browser, for example `PUBLIC_ORIGIN=http://192.168.1.20:3000`. The API checks the `Origin` header of every write against it (CSRF defence); a mismatch returns 403, which looks like "login does nothing".
3. Keep `SECURE_COOKIES=false`. With `BBD_ENVIRONMENT=auto` a `192.168.x.x` origin stays in development mode.
4. Restart: `make stop && make dev`.

Do this only on a trusted network; plain http exposes the session cookie to anyone on the LAN. Use section 6 instead for anything beyond your home or lab.

## 4. Native run (hot reload without containers)

Use this when you run `npm run dev` (API, worker, chat worker and Next dev server on the host). Start only the data services in Docker, then point the native processes at loopback.

```bash
docker compose -f docker-compose.base.yml up -d postgres redis      # add "graph" profile only if needed
```

In `.env` change the container hostnames to `localhost` (this `.env` can no longer be shared with the Docker `api`/`worker` containers; keep a copy):

| Variable | Container value | Native value |
|---|---|---|
| `DATABASE_URL` | `...@postgres:5432/bbd` | `...@localhost:5432/bbd` |
| `REDIS_URL` | `redis://:pw@redis:6379/0` | `redis://:pw@localhost:6379/0` |
| `GRAPH_HOST` | `graph` | `localhost` |
| `N8N_SERVICE_URL` | `http://n8n:5678` | `http://localhost:5678` |
| `API_INTERNAL_URL` (web) | `http://api:8000` | `http://localhost:8000` (default) |
| `DATA_DIR` | `/data` | a writable local folder, for example `./.data` |

Then:

```bash
npm run dev:migrate     # alembic upgrade head
npm run dev             # be + worker + chat + fe, hot reload
```

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| API exits: `PUBLIC_ORIGIN must be https for a non-local host` | Origin is a public domain over http | Use https (section 6) or set `BBD_ENVIRONMENT=development` only if you really are local |
| API exits: `SECURE_COOKIES must be true when PUBLIC_ORIGIN is https` | https origin, insecure cookies | Set `SECURE_COOKIES=true` |
| API exits: `<field> uses a placeholder or default credential` | Production mode with `replace-with-*` or `bbd:bbd` | Run `make setup` (fills real values) or use development mode locally |
| Login or any POST returns 403 | `PUBLIC_ORIGIN` differs from the browser URL (`localhost` vs `127.0.0.1` vs LAN IP) | Make them identical, restart the API |
| Logged in but bounced to `/login` | Cookie not stored: `SECURE_COOKIES=true` on an http origin | Set `SECURE_COOKIES=false` for local http |
| `/docs` returns 404 | Production mode | Set `BBD_ENVIRONMENT=development` |
| Google sign-in fails locally | Google OAuth requires `localhost` or https redirect URIs | Use `http://localhost:3000` (not a LAN IP) or https |
| Browser keeps forcing https on `localhost` | An earlier HSTS entry was cached | `chrome://net-internals/#hsts` → delete domain |

## 6. When you want HTTPS (public or semi-public)

Follow `docs/deployment.md` (Cloudflare Tunnel + Access, or the optional Caddy overlay). Switch these in `.env`, keeping `BBD_ENVIRONMENT=auto`:

```
PUBLIC_ORIGIN=https://umwelt.example.com
SECURE_COOKIES=true
AUTH_CLIENT_IP_HEADER=CF-Connecting-IP   # or X-Real-IP set by your proxy
```

The domain makes `auto` resolve to production, so the API then also rejects placeholder secrets and default database credentials. Run `make setup` first so every secret is real.

## 7. Safety notes for development mode

- Development mode only relaxes transport and secret-format checks. Authentication, CSRF, rate limits, SSRF guards and workspace scoping stay on.
- Explicit `BBD_ENVIRONMENT=development` is the only setting that tolerates template secrets; use it only on your own machine. Never expose a development-mode instance to the internet: its API docs are public and weak secrets are tolerated. Setting `BBD_ENVIRONMENT=production` explicitly on a server removes the dependence on the `auto` heuristic (and its local-origin transport relaxation).
- Do not commit `.env`; only `.env.example` is tracked.
