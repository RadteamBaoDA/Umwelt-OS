# Engineering scorecard

Re-scored 2026-11-06 at `cf6dcdb6` (branch `feature/fix-ui`) by a read-only review. Equal weights across 11 dimensions. Scores come from the repo plus locally reported runs; run logs are kept outside the repo.

**Overall: 8.9 / 10** (8.3 at `e70e51b7`, 8.7 at `150fa728`, 8.8 at `628248cb`; target 9).

| Dimension | Prev | Now | Evidence / remaining gap |
|---|---|---|---|
| Architecture | 9.2 | 9.3 | Import-linter (3 contracts) in CI. Observations public contract, chat routes helpers and document cleanup worker now also split into private submodules behind unchanged facades (on top of the ~20 earlier splits). FE cross-module imports go through `<module>/public-api` (ESLint `no-restricted-imports` allows only that entry point), so the command palette no longer pulls whole barrels. Gap: GitNexus rates the big split merges CRITICAL (pure moves, verified by tests/mypy/import-linter). |
| Async | 9.2 | 9.2 | `test_event_loop_offload.py` deterministic (thread identity, not wall clock) and run as its own CI step without coverage tracing (`ci.yml:51`). Purge-replay race fixed; `set_event_delivery` compare-and-set. Gap: remaining long functions are mostly lock/transaction ordering. |
| Security | 9.0 | 9.0 | `test_auth_race` 3/3 on a fresh stack; npm audit 0; RustFS fails closed on empty/default keys and sits on a dedicated `storage` network; action SHAs verified upstream. Gap: integration security evidence is local only (no CI run yet). |
| Testing | 8.6 | 8.9 | Unit 6856 passed / 2 skipped (~340 new focused tests: MCP transport policy, Google/Telegram/GitHub/Notion/Slack mappers, export cursor integrity, Langfuse export); Python branch coverage measured 68.0%, CI floor 65 → 67 (`ci.yml:50`); integration 102/102 re-run at `ad92db9f`/`cf6dcdb6` after the worker splits (one stale test patch fixed, test-only); e2e generative-ui 11, chat-live 11, system 3, dashboard-datasources 26/26, earlier FE e2e green after the bundle cut. Gaps: 1-point coverage margin; nightly acceptance never executed on GitHub. |
| Load / perf | 8.2 | 8.2 | `docker-compose.limits.yml` (2 CPU / 8 GiB) committed and used by `LOAD_TEST_LIMITS=1` and CI `load-smoke`; 3 runs at `084f0fc0` pass every SLO (medians/spreads in `docs/performance-report.md`); `scripts/load-test.sh` portable on Windows Git Bash. Gaps: k6 not re-run after the latest refactors; no `docker stats` sampling, not the target host, CI `load-smoke` never executed. |
| Maintainability | 8.9 | 9.1 | File-size baseline 7 → 4 entries (max 1791, none > 2000); function-length baseline 125 → 99; 11 legacy FE files Prettier-formatted with oversized components split; patch helpers keep test patch targets correct. Debt: 4 files > 1500 (`core/model_gateway/client.py`, `modules/connectors/routes.py`, `modules/connectors/service/github_hints.py`, `modules/connectors/provider_specs.py`); 99 long functions mostly lock/transaction ordering. |
| Conventions | 8.8 | 8.9 | `ruff format --check`, S rules, length/size ratchets, docstring convention in CI; web format gate portable (`endOfLine: auto`, `format-check.mjs` via `process.execPath`); legacy FE files brought under Prettier; FE module-boundary lint rule narrowed to `public-api`. |
| Folder structure | 8.7 | 8.7 | `provisioning_routes.py` moved into `provisioning/`; corrections split. Gap: new splits (observations, chat routes, documents worker) are again flat `_worker_*` / `_area_*` siblings rather than subpackages. |
| Storage / data | 9.0 | 9.0 | `set_event_delivery` guards terminal statuses; RustFS replaces MinIO with S3 tests passing; 0021 downgrade/upgrade and outbox gauges carry over. Gap: no restore/retention test for objects in RustFS. |
| FE perf | 8.3 | 8.7 | Recharts out of the shared workspace chunk (chart components and weather card via `next/dynamic` with skeletons). First-load gzip: `/chat` 911 → 666 KB, dashboard/settings 755 → 632, knowledge/timeline 535, `/search` 433, `/news` 398, public 245–375; max route 765; budget max 1005 → 841 / total 2313; Lighthouse CI on public routes (local perf 0.97–1.0, a11y 1.0). Gaps: total client JS rose 1984 → 2103 KB from chunk splitting; max route only ~9% under budget; no authenticated Lighthouse route; Lighthouse never run on GitHub. |
| Ops / data | 9.0 | 9.2 | `severity=page` → distinct `owner-page` receiver; CI amtool tests both receivers; promtool rule tests; synthetic end-to-end delivery (`scripts/alert-delivery-smoke.sh`: real Alertmanager + stub sink, asserts ticket→`owner-webhook` and page→`owner-page`; passes locally, fails on a forced misroute) runs in the push/PR monitoring-rules job (`ci.yml:71`). Gaps: page receiver defaults to the same webhook URL; nightly jobs unexecuted. |

## Top actions toward 9 (gain per effort)

1. Run the nightly `acceptance`, `load-smoke` and `lighthouse` jobs on GitHub (`gh workflow run ci.yml --ref feature/fix-ui -f run_docker_acceptance=true` after push), fix reds, keep run links. ≈ +0.06.
2. Load evidence: add `docker stats` sampling to `scripts/load-test.sh`; re-run k6 3x at HEAD (on the target host if possible). ≈ +0.02.
3. FE perf: add an authenticated Lighthouse route (dashboard or `/chat` with a seeded session); lower `maxRouteFirstLoadKb` 841 → ~800 and take back some of the total-JS growth. ≈ +0.02.
4. Structure: group the flat `_worker_*` / `_area_*` siblings into subpackages (start with the newest splits); get under 3 files > 1500 (start with `core/model_gateway/client.py`). ≈ +0.03.
5. Test gates: widen the coverage margin (floor 67 → 68 with measured ≥ 70). ≈ +0.01.
6. Ops: require a distinct `ALERT_PAGE_WEBHOOK_URL_FILE` in production compose (or warn at start-up when it equals the ticket URL). ≈ +0.01.

Action 1 alone reaches about 8.99; 9.0 needs 1 plus any one of 2–4, or 2–6 without 1.
