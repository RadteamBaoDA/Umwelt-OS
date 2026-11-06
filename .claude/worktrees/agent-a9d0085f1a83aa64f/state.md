# Umwelt-OS remediation — trạng thái (state)

- Cập nhật: 2026-11-07. Nhánh `feature/fix-ui`, head local `e70e51b7` (+ commit cập nhật state này). **Chưa push** các commit W7.
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

## Còn lại

- `test_auth_race` chưa chạy lại trên stack sạch; datasources e2e + MinIO chưa chạy.
- Nightly jobs chưa từng chạy (schedule chỉ chạy trên default branch); SHA `actions/upload-artifact` chưa xác minh.
- `set_event_delivery` cập nhật vô điều kiện.
- npm audit 8 moderate (dev-only).
- File override giới hạn CPU của k6 chưa commit.
- Top-5 việc lên 9+: xem `docs/scorecard.md`.
- Session song song đã merge `42d5b08c` (compose `!reset`) và `9c83afb8` (docs Langfuse); test compose hiện đã pass.
