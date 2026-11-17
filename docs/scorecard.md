# Engineering scorecard

Re-scored 2026-11-07 at `8bb840c2` (branch `feature/fix-ui`) by a read-only review. Equal weights across 11 dimensions. Scores come from the repo plus locally reported runs; run logs are kept outside the repo.

**Overall: 9.0 / 10** (9.03; 8.3 at `e70e51b7`, 8.7 at `150fa728`, 8.8 at `628248cb`, 8.9 at `cf6dcdb6`; next target 9.5).

| Dimension | Prev | Now | Evidence / remaining gap |
|---|---|---|---|
| Architecture | 9.3 | 9.3 | Import-linter (3 contracts) in CI; ~20 earlier splits plus the observations/chat routes/document cleanup splits sit behind unchanged facades; private families now live in `_<family>/` subpackages, still private to their module. FE cross-module imports go only through `<module>/public-api` (ESLint `no-restricted-imports`). Gap: GitNexus rates the big move merges CRITICAL (pure moves, verified by unit, mypy, import-linter and integration 102/102). |
| Async | 9.2 | 9.2 | `test_event_loop_offload.py` deterministic (thread identity) and run as its own CI step without coverage tracing; purge-replay race fixed; `set_event_delivery` compare-and-set. Gap: remaining long functions are mostly lock/transaction ordering. |
| Security | 9.0 | 9.0 | `test_auth_race` 3/3 on a fresh stack; npm audit 0; RustFS fails closed on empty/default keys on a dedicated `storage` network; action SHAs pinned and verified. Gap: integration security evidence is local only (no CI run yet). |
| Testing | 8.9 | 8.9 | Unit 6884 passed / 2 skipped (+28 core tests: cron state, tool schema validator bounds, chunking); branch coverage ~68.1%, CI floor 67 (`ci.yml:50`); integration 102/102 re-run at `e1cd5696`/`8ce52ae3` after the subpackage move; e2e suites green from the previous round. Gaps: 1-point coverage margin; nightly acceptance never executed on GitHub. |
| Load / perf | 8.2 | 8.5 | `scripts/load-test.sh` samples `docker stats` per container about every 5 s and writes `docker-stats-summary.txt`; 3 runs at `8ce52ae3` (after the refactors) under `docker-compose.limits.yml` (2 CPU / 8 GiB) pass every SLO (medians: dashboard p95 71.8 ms, search 95.5, documents 59.8, chat TTFT 756, chat send 110, ingest lag 3272, queue age 2558 vs 5000) with a per-service resource table (`docs/performance-report.md`, "Re-run at 8ce52ae3"). api/worker/chat-worker/cron-worker briefly touch their CPU caps (avg 4–12%); memory far below limits. Gaps: not the target host; CI `load-smoke` never executed. |
| Maintainability | 9.1 | 9.1 | `core/model_gateway/client.py` 1791 → 1634 via `_text.py` helper extraction; file-size baseline 4 entries (max 1680, none > 2000); function-length baseline 99. Debt: 4 files > 1500 (`modules/connectors/routes.py` 1680, `core/model_gateway/client.py` 1634, `modules/connectors/service/github_hints.py` 1561, `modules/connectors/provider_specs.py` 1548); 99 long functions mostly lock/transaction ordering. |
| Conventions | 8.9 | 8.9 | `ruff format --check`, S rules, length/size ratchets, docstring convention in CI; web format gate portable; `.gitattributes` forces `*.sh eol=lf` so shell entrypoints run in Linux containers from Windows checkouts. |
| Folder structure | 8.7 | 9.1 | 17 private families moved from flat `_<family>_<name>.py` siblings into subpackages (`agents/_harness`, `agents/_public`, `automations/_execution`, `chat/_worker`/`_public`/`_routes`, `connectors/_collection`/`_worker`, `github/_routes`, `provisioning/_routes`, `ingestion/_worker`/`_native`, `documents/_worker`, `entities/_corrections`/`_worker`, `temporal/_worker`, `settings/_settings`), facades unchanged. Gap: 81 flat `_x_y.py` files remain in small (1–2 file) families; connectors and tools still have many top-level siblings. |
| Storage / data | 9.0 | 9.0 | `set_event_delivery` guards terminal statuses; RustFS with S3 tests passing; 0021 downgrade/upgrade and outbox gauges carry over. Gap: no restore/retention test for objects in RustFS. |
| FE perf | 8.7 | 9.0 | Authenticated Lighthouse route: `lighthouserc.json` audits `/` (dashboard) after `scripts/lighthouse-auth.cjs` creates the owner and signs in on a disposable compose stack in the nightly `lighthouse` job (`ci.yml:203`); local `/dashboard` perf 0.92 / a11y 1.0, public routes 1.0 / 1.0. `maxRouteFirstLoadKb` 841 → 800 (measured max 765). Gaps: Lighthouse never run on GitHub; route margin ~4%; total client JS 2103 of 2313 unchanged; performance assertion only warns. |
| Ops / data | 9.2 | 9.3 | `infrastructure/monitoring/alertmanager-entrypoint.sh` (wired in `docker-compose.monitoring.yml`) fails closed when the page webhook file is missing/empty or equals the ticket URL (`ALERT_ALLOW_SHARED_PAGE_URL=true` downgrades to a warning); `severity=page` → `owner-page`, amtool route tests for both receivers, promtool rule tests, synthetic delivery smoke in the push/PR monitoring job. Gaps: preflight not exercised in CI (`PREFLIGHT_ONLY` exists but no CI step uses it); nightly jobs unexecuted. |

## Top actions toward 9.5 (gain per effort)

1. Run the nightly `acceptance`, `load-smoke` and `lighthouse` jobs on GitHub (`gh workflow run ci.yml --ref feature/fix-ui -f run_docker_acceptance=true` after push), fix reds, keep run links. ≈ +0.10.
2. Test gates: raise measured coverage to ≥ 70% and the floor 67 → 69, targeting connectors/provisioning and agents worker paths. ≈ +0.04.
3. Maintainability: get all 4 files under 1500 (start with `modules/connectors/routes.py` 1680 and `core/model_gateway/client.py` 1634) and cut the function-length baseline 99 → ≤ 80 by extracting lock/capture phases. ≈ +0.04.
4. Load: run k6 3x on the target host (2 vCPU / 8 GiB) with `docker stats` and host CPU sampling. ≈ +0.03.
5. Ops: add a monitoring-job step that runs the entrypoint with `PREFLIGHT_ONLY=1` in `prom/alertmanager:v0.28.1` for the missing, identical and distinct URL cases. ≈ +0.01.
6. Storage: add a RustFS object restore/retention test. ≈ +0.02.
7. FE perf: cut total client JS toward ≤ 2000 KB, make Lighthouse performance `error` at 0.85, add `/chat` as a second authenticated route. ≈ +0.02.
8. Structure: group the remaining connectors/tools top-level siblings into subpackages where a family reaches 3 files. ≈ +0.01.

Actions 1–8 add up to about +0.27 (≈ 9.30). 9.5 also needs the long-function and coverage debt to keep falling.
