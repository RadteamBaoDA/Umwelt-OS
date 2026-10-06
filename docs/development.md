# Development

Install Python 3.12, uv, Node.js 24/npm, and Docker Compose v2.24 or newer. Run `./scripts/dev.ps1 setup` in Windows PowerShell or `make setup` on macOS/Linux. Setup creates `.env` only when missing, then installs locked Python and npm dependencies.

Run the hot-reload stack with `./scripts/dev.ps1 dev` or `make dev`. The frontend is at `http://localhost:3000`. Only its loopback port is published; API, PostgreSQL, and Redis stay on the Compose network. To stop services without deleting data, use the matching `stop` command.

For fictional Phase 1, 8, 10 and 12 data, run `./scripts/dev.ps1 seed` or `make seed` after setup. Seeding is explicit and never runs at startup. Stable per-phase owner receipts preserve edits/deletions and ensure the existing `bbd-os.demo.phase-1` source sentinel cannot suppress newer P12 fixtures. P12 adds a project entity, linked person, relationship, manual timeline event, normalized fictional article, and an example conversation only when the owner's durable-history setting permits it. Tasks/goals/topics and disabled automation examples reuse their existing owner seed helpers. A receipt prevents deleted fixtures from being recreated by later seed runs.

To run an isolated local stack, use a unique project such as `bbd-os-ws-a1b2c3d4e5f6`: `./scripts/dev.ps1 dev -Workspace bbd-os-ws-a1b2c3d4e5f6` or `make workspace-up WORKSPACE=bbd-os-ws-a1b2c3d4e5f6`. Each workspace uses project-prefixed named volumes, a project network, and a separately selected loopback web port. Workspace commands force API, worker, and migration database/Redis URLs to those project-local services, regardless of application URLs inherited from `.env`. First registration refuses pre-existing project containers, volumes, or networks and records the Docker daemon ID. Seed it with `./scripts/dev.ps1 seed -Workspace <name>` or `make workspace-seed WORKSPACE=<name>`. Before clearing it, run `./scripts/dev.ps1 reset-preview -Workspace <name>` or `make reset-preview WORKSPACE=<name>`, inspect the name and fingerprint, then pass the exact fingerprint to `./scripts/dev.ps1 reset -Workspace <name> -ConfirmFingerprint <fingerprint>` or `make reset WORKSPACE=<name> CONFIRM_FINGERPRINT=<fingerprint>`. Reset refuses the default project, unregistered workspaces, a changed Compose fingerprint, unexpected volumes/networks, and containers whose Compose project/checkout labels do not match. It deletes only the named local development workspace's Compose resources; owner datasets in shared/external databases are not a supported reset target.

The first-run form requires `SETUP_TOKEN` from the local `.env` file and a password of 12–128 characters. The setup token is separate from the owner password. The application stores only password/session hashes. Do not paste secrets into issues, screenshots, or shared logs.

During production implementation, CI runs dependency installation and the production build only. After all original and reconciliation production tasks are complete, manually dispatch the CI workflow with **Run deferred validation** enabled to run lint, typecheck, and tests. Locally, `test` creates an isolated Compose project and cleans only its uniquely named test volumes on exit. To run only the backend unit tests during the deferred validation stage, use `uv run pytest -q`; frontend lint/typecheck commands are `npm run lint` and `npm run typecheck`.

Migrations are managed through Alembic. Run `migrate` for an existing local stack; startup also runs the one-shot migration service and waits for success. Do not manually edit a live database schema.

## Cross-module read projections

Module persistence models stay private. The following SQL projections are approved for reads only; they grant no write access or separate agent/MCP authorization. Existing owner authentication and source/document visibility checks remain in force.

| Consumer | Owner fields read | Required fences |
| --- | --- | --- |
| Search retrieval (`modules/search/public.py`) | Source: `id,name,type,status,local_only`; document: `id,source_id,current_version,extraction_status,content_type,published_at,observed_at,title,canonical_url`; version: `id,document_id,version_number,observed_at`; chunk: `id,document_version_id,content` | Existing source/document/version/chunk joins; current revision; ready/succeeded extraction; active source. Recheck after provider calls. Lexical may include local-only sources; vector retrieval may not. |
| Search indexing (`modules/search/indexing.py`) | Source: `id,status,local_only`; document: `id,source_id,current_version,extraction_status`; version: `id,document_id,version_number`; chunk: `id,document_version_id,content` | Active, current, extracted and non-local-only eligibility. Hold the source row lock across provider transport and recheck before each send. Remote embeddings also require owner opt-in. |
| Current chunk backfill (`modules/knowledge/documents/public.py`) | Source: `id,status` | Active source, current document version and ready/succeeded extraction. Writes remain document-owned. |
| Raw storage cleanup and purge manifests | Document: `source_id,raw_uri` | Orphan detection includes every retained reference, including paused/archived sources and failed extraction. Purge manifests are scoped to the locked source and generation; keep path validation and retry handling. |

Cross-module mutations use owner commands on the caller's `AsyncSession`; commands preserve source generation fences, row-lock ordering, and transaction boundaries without committing early.
