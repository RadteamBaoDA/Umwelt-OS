# Engineering scorecard

Re-scored 2026-11-06 at `628248cb` (branch `feature/fix-ui`) by a read-only review. Equal weights across 11 dimensions. Scores come from the repo plus locally reported runs; run logs are kept outside the repo.

**Overall: 8.8 / 10** (8.3 at `e70e51b7`, 8.7 at `150fa728`; target 9).

| Dimension | Prev | Now | Evidence / remaining gap |
|---|---|---|---|
| Architecture | 9.0 | 9.2 | Import-linter (3 contracts) in CI. ~20 modules split into private submodules behind unchanged facades (dashboard, timeline, memory, sources, temporal/entities/connectors workers, relationships, connectors collection/provisioning/github routes, entity corrections, ingestion worker, chat public, search indexing, settings, news stories, automations execution, agents public/harness, dashboard briefs); frozen W2 contract manifest repointed. Gap: GitNexus rates the big split merges CRITICAL (pure moves, verified by tests/mypy/import-linter). |
| Async | 9.0 | 9.2 | `test_event_loop_offload.py` deterministic (thread identity, not wall clock) and run as its own CI step without coverage tracing (`ci.yml:51`). Purge-replay race fixed; `set_event_delivery` compare-and-set. Gap: remaining long functions are mostly lock/transaction ordering. |
| Security | 8.7 | 9.0 | `test_auth_race` 3/3 on a fresh stack; npm audit 0; RustFS fails closed on empty/default keys and sits on a dedicated `storage` network; action SHAs verified upstream. Gap: integration security evidence is local only (no CI run yet). |
| Testing | 8.0 | 8.6 | Unit 6512 passed / 2 skipped; Vitest 1106/1106; integration 102/102 on a fresh stack at `084f0fc0`; e2e green (rest group, datasources, `sources-builtin-fetch` 47/47, FE re-check after bundle split); flaky storage orphan-cleanup test deterministic (20x). Branch coverage on. Gaps: floor 65 (measured 66); integration not re-run after the last two worker splits; nightly acceptance never executed on GitHub. |
| Load / perf | 7.0 | 8.2 | `docker-compose.limits.yml` (2 CPU / 8 GiB) committed and used by `LOAD_TEST_LIMITS=1` and CI `load-smoke`; 3 runs at `084f0fc0` pass every SLO (medians/spreads in `docs/performance-report.md`); `scripts/load-test.sh` portable on Windows Git Bash. Gaps: no `docker stats` sampling, not the target host, CI `load-smoke` never executed. |
| Maintainability | 8.0 | 8.9 | File-size baseline 28 → 7 entries (max 1855, none > 2000); function-length baseline 248 → 125; patch helpers (`tests/unit/split_patching.py` etc.) keep test patch targets correct. Debt: 6 files > 1500; long functions mostly lock/transaction ordering. |
| Conventions | 8.7 | 8.8 | `ruff format --check`, S rules, length/size ratchets, docstring convention in CI; web format gate portable (`endOfLine: auto`, `format-check.mjs` via `process.execPath`). |
| Folder structure | 8.3 | 8.7 | `provisioning_routes.py` moved into `provisioning/`; corrections split. Gap: splits are flat `_worker_*` / `_area_*` siblings rather than subpackages. |
| Storage / data | 8.5 | 9.0 | `set_event_delivery` guards terminal statuses; RustFS replaces MinIO with S3 tests passing; 0021 downgrade/upgrade and outbox gauges carry over. Gap: no restore/retention test for objects in RustFS. |
| FE perf | 7.0 | 8.3 | Bundle-size gate in CI; public routes 1030 → 222–340 KB gzip, workspace ~1030 → ~755, `/chat` 911; vi-vi catalog on demand; ChatDrawer via `next/dynamic`; budget max 1005 / total 2100; Lighthouse CI on public routes (local perf 0.97–1.0, a11y 1.0). Gaps: ~90 KB Recharts chunk on every workspace route; `/chat` ~9% under budget; no authenticated Lighthouse route; Lighthouse never run on GitHub. |
| Ops / data | 8.6 | 9.0 | `severity=page` → distinct `owner-page` receiver (shorter group/repeat); CI amtool tests both receivers and fails on mismatch; promtool rule tests. Gaps: page receiver defaults to the same webhook URL; no synthetic end-to-end delivery; nightly jobs unexecuted. |

## Top actions toward 9+ (gain per effort)

1. Run the nightly `acceptance`, `load-smoke` and `lighthouse` jobs on GitHub (`gh workflow run ci.yml --ref feature/fix-ui -f run_docker_acceptance=true` after push), fix reds, keep run links. ≈ +0.06.
2. Test gates: raise `--cov-fail-under` 65 → 68 with targeted tests; re-run integration at HEAD. ≈ +0.03.
3. FE perf: take Recharts out of the shared workspace chunk, add an authenticated Lighthouse route, lower `maxRouteFirstLoadKb` toward 850 then 700. ≈ +0.05.
4. Synthetic alert delivery: post a test alert to Alertmanager with a stub webhook receiver and assert it arrives on both routes. ≈ +0.02.
5. Structure ratchet: group flat `_worker_*` / `_area_*` siblings into subpackages, get under 5 files > 1500 (start with `observations/public.py` 1855), trim the function baseline below 100. ≈ +0.05.
6. Load evidence: add `docker stats` sampling to `scripts/load-test.sh`; repeat 3x on the target host. ≈ +0.03.

Actions 1–4 reach about 8.95; 9+ needs 1–5.
