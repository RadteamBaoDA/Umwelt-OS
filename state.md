# Umwelt-OS remediation — trạng thái (state)

- Cập nhật: 2026-11-07. Nhánh `feature/fix-ui`, head local `3863d100`. Owner tự push.
- Plan gốc: `docs/superpowers/plans/2026-11-04-code-quality-remediation-plan.md`. Scorecard: `docs/scorecard.md` (8.3/10, mục tiêu 9).

## Đã xong (merge vào `feature/fix-ui`)

- Wave 1–4 + PR #1 (xem lịch sử git), A1 outbox-events, A2 boundaries (import-linter 3 contract), P0 `pin_default_queue`, CSP nonce + `/genui-frame`, egress `core/net_policy`, SQL reader (0017–0019), SSE terminal close (TTFT p95 0.71 s).
- **W7**:
  - Ops/CI: Alertmanager, promtool rule tests, nightly acceptance + k6 smoke, coverage floor (68% Py / 60% FE lines), test cron→dispatcher.
  - Tách `entities/public.py`, `chat/worker.py`, `connectors/provisioning`.
  - 165 assert → raise.
  - `ruff format` toàn repo + `--check` trong CI.
  - Ratchet độ dài hàm / kích thước file (baseline 248 hàm, 28 file; `.git-blame-ignore-revs`).
  - Outbox gauges + alert `UmweltOutboxPurgeBacklog`; không làm processed_events / partition (lý do trong `docs/operations.md`).
  - Migration `0021` siết role `umwelt_app`; `0020` giữ nguyên bản đã push.
  - Review Opus gộp: ship-with-fixes, đã sửa hết.
- Kiểm tra:
  - unit 6523 pass / 2 fail đã biết (Windows launch-path, flake `test_run_web_parses_big_pages_off_the_loop`);
  - Docker: integration 68/68, 0021 downgrade/upgrade OK, promtool/amtool OK;
  - k6 2 CPU / 8 GiB: pass 10/10 SLO với split cron-worker 0.3 (1 lần chạy, `docs/performance-report.md`).

## Đã xong sau W7 (2026-11-06)

- MinIO → RustFS (profile `s3`, fail-closed keys, network `storage` riêng cho api/worker/cron-worker).
- Spec `BUILTINS` thêm 5 built-in; `test.sh`/`dev.ps1` export `CHAT_SQL_READER_DATABASE_URL`; sửa test Windows launch-path, test offload deterministic, race replay trong `test_p12_source_purge`.
- `set_event_delivery` compare-and-set theo status; `docker-compose.limits.yml` + `LOAD_TEST_LIMITS=1`; npm audit 0.
- Kiểm tra local: integration (`test_auth_race` 3/3, reader 20/20, S3 RustFS 3/3, purge 3×4/4); e2e rest 12/13 spec + screens-capture nhóm dark pass riêng, datasources pass, `sources-builtin-fetch` 47/47 (crash screens-capture cũ do harness timeout).

## Còn lại

- Nightly jobs chưa từng chạy (schedule chỉ chạy trên default branch); SHA `actions/upload-artifact` chưa xác minh.
- k6 với `docker-compose.limits.yml` mới chạy 1 lần; cần lặp ≥3.
- Top-5 việc lên 9+: xem `docs/scorecard.md`.
