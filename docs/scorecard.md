# Engineering scorecard

Re-scored 2026-11-07 at `e70e51b7` (branch `feature/fix-ui`) by a read-only review. Equal weights across 11 dimensions.

**Overall: 8.3 / 10** (previous ≈7.9 under the same weighting; target 9).

| Dimension | Prev | Now | Evidence / remaining gap |
|---|---|---|---|
| Architecture | 9 | 9.0 | Import-linter (3 contracts) in CI; giant modules split behind `public.py` facades. |
| Async | 9 | 9.0 | Cron→dispatcher regression test added; `test_event_loop_offload.py` timing test still flaky. |
| Security | 8.5 | 8.7 | Zero production `assert`; migration `0021_harden_umwelt_app_group_role`; SHA-pinned actions. Gaps: `test_auth_race` not re-verified on a fresh stack; npm audit 8 moderate (dev-only). |
| Testing | 7.5 | 8.0 | Coverage gated (Python 68% floor / 70% measured, Vitest lines 60). Gaps: 2 known unit failures; datasources e2e group and MinIO tests not run; nightly acceptance never executed. |
| Load / perf | 6 | 7.0 | 2 CPU / 8 GiB k6 pass (`docs/performance-report.md`), but single run, uncommitted override, several shares changed together, not the target host; CI smoke never executed. |
| Maintainability | 7.5 | 8.0 | Formatter, S101, function-length and file-size ratchets. Debt: 28 files > 1500 lines (7 > 3000), 248 functions > 100 lines (baselined). |
| Conventions | 8 | 8.7 | `ruff format --check`, S rules, length/size gates, docstring convention enforced in CI. |
| Folder structure | 8 | 8.3 | Entities, provisioning, chat-worker splits. Chat split is flat `_worker_*.py`; `provisioning_routes.py` outside its package. |
| Storage / data | 8 | 8.5 | 0021 downgrade/upgrade proven; outbox gauges + purge-backlog alert. `set_event_delivery` still an unconditional update. |
| FE perf | 7 | 7.0 | No bundle or Lighthouse budget; `connector-editor.tsx` 1357 lines. |
| Ops / data | 8.1 | 8.6 | Alertmanager routing + promtool rule tests in CI. Delivery never exercised end to end; nightly jobs unexecuted. |

## Top actions toward 9+ (gain per effort)

1. Run the nightly `acceptance` and `load-smoke` jobs (manual dispatch or after merge to the default branch), fix reds, keep run links. ≈ +0.25.
2. Make perf evidence reproducible: commit a `docker-compose.limits.yml` selectable from `scripts/load-test.sh`, repeat the final split ≥3 times (median/spread), isolate the cron-worker share, point CI `load-smoke` at it.
3. Clear known reds: deterministic offload test, Windows launch-path test, guard `set_event_delivery` on status/row count, run datasources e2e + MinIO + `test_auth_race` on a fresh stack.
4. Shrink the 7 files over 3000 lines into private-submodule packages, finish `entities/corrections.py`, move `provisioning_routes.py`; lower baselines with `--update` toward < 15 files / < 150 functions.
5. FE perf gate: bundle-size budget and Lighthouse CI on 2–3 routes; split `connector-editor.tsx`. Extras: branch coverage via `[tool.coverage]`; fire one synthetic alert through Alertmanager in CI.

Actions 1–3 reach about 8.6; 9+ needs 1–4.
