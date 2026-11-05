# Umwelt-OS remediation — trạng thái (state)

- Cập nhật: 2026-11-05 (lần 2: đã push nhánh WIP + lưu patch trong `docs/wip/`)
- Plan gốc: `docs/superpowers/plans/2026-11-04-code-quality-remediation-plan.md`
- Đã merge: [PR #1](https://github.com/RadteamBaoDA/Umwelt-OS/pull/1) → `feature/fix-ui` (merge commit `cc5045b2`, head `186e8120` của `feature/fix-ui-quality`)
- Dừng theo yêu cầu owner ("Stop toàn bộ") — goal đã clear.
- Head hiện tại `feature/fix-ui`: `6364deea` (PR #1 + `state.md` + patch WIP). Mọi nhánh (`feature/fix-ui`, `feature/fix-ui-quality`, `wp/boundaries`, `wp/outbox-events`) đã push, local = remote; không còn thay đổi chưa commit.

## 1. Đã hoàn thành (đã merge vào `feature/fix-ui`)

| Wave | Nội dung | Review |
|---|---|---|
| 1 | Vá SQL tool (pglast allowlist + role `umwelt_chat_sql_reader` + read-only, migration 0013, đã kiểm trên Postgres thật); `max_tokens` theo provider (ẩn với anthropic/openai/groq); sửa capability evidence; fail-closed client IP; widget xác nhận link/prompt; pin RSS/crawl; offload event-loop | Opus approve |
| 2 | `DomainError`, admission helpers, cursor helpers, `ModuleDescriptor` + router/erasure registry, `StorageBackend`/`StorageFence`, `WireProtocol` gateway; web platform (cn nội bộ, format, `apiRequestRaw`, route groups, OpenAPI types) | Opus approve |
| 3 | Tách toàn bộ hàm >100 dòng (169 → 0), test đặc tả trước khi tách; registry ProviderCollector / NativeProofVerifier / tool handler / gadget; tách god modules; nâng cấp dependency (next, dompurify, zod) | Opus approve (web-rest còn 1 major về test hook — đã bù bằng guardrails) |
| 4b | Migration safety + index (0014), tsvector, HNSW concurrent, retention outbox, bulk entity API; lock privacy theo workspace + SSE notify + pool HTTP client; Prometheus/OTel/alerts; cron jitter + single-flight + cron-worker + backoff + DLQ (0015); PgBouncer, Sentinel, per-role timeout; S3/MinIO backend + Postgres fence (0016); SLO + k6 assets | Opus approve |
| 4a (một phần) | dedupe helpers; guardrails (ruff strict, function-length gate, ESLint error, prettier, CI workflow, hook tests) | Opus approve |

Kết quả kiểm tra cuối (local, `186e8120`): pytest **6491 passed / 0 failed**, mypy 0, ruff gate sạch, function-length 0, vitest **1094/1094**, tsc 0, ESLint + prettier sạch, `next build` OK, `npm audit` 0 high/critical.

## 2. Chưa hoàn thành

### 2.1 Work package chưa merge (đã lưu để làm sau)
| Mục | Trạng thái | Nơi lưu |
|---|---|---|
| **wp/boundaries** (plan §3.7: bỏ import vào nội bộ module khác, read projection có tài liệu, descriptor `dependencies` đúng thực tế, import-linter) | Đang implement dở, **chưa review**. Thử merge vào `feature/fix-ui` (2026-11-05): 8 file xung đột (giải quyết được) nhưng code còn vỡ ruff gate (hàng trăm E402 do khối re-export đặt trước import) ⇒ không merge để giữ `fix-ui` xanh | Nhánh remote `wp/boundaries` (`b7fcb69b`); patch `docs/wip/wp-boundaries.patch` (diff từ `a28b5da3`, 196 file, +1570/−508) |
| **wp/outbox-events** (chuyển outbox + dispatcher vào `core/events`, registry consumer qua descriptor, validate emitted/consumed events, replay CLI) | Đã Opus approve nhưng **không merge**: xung đột sâu với event-driven wake (`notify_on_commit` trong `publish_event`) và dispatcher của worker-cron | Nhánh remote `wp/outbox-events` (`593eeb4a`); patch `docs/wip/wp-outbox-events.patch` (diff từ `a28b5da3`, 26 file, +1510/−527) |

**Cách tiếp tục:**
```bash
git switch -c wp/boundaries-v2 origin/feature/fix-ui
git merge origin/wp/boundaries   # hoặc: git apply --3way docs/wip/wp-boundaries.patch
```
- boundaries: giải quyết 8 xung đột (`Makefile`, `scripts/dev.ps1` giữ bản HEAD; `news/backfill_attribution.py` gộp import; các `public.py` lấy khối re-export), rồi sửa E402 (đưa khối re-export xuống sau import hoặc dùng `__getattr__` lazy), ratchet `tests/unit/core/test_descriptor_dependencies.py` về 0, thêm `.importlinter`, chạy `lint-imports`.
- outbox-events: port lên base mới, giữ `notify_on_commit(session, "dispatch_pending_work")` và `index_pending_chunks` trong `publish_event` ở `core/events`, giữ `_valid_cleanup_envelope`/`_envelope_uuid_fields` của dispatcher hiện tại.
- Cả hai cần Opus review lại + full suite + integration trước khi merge.

### 2.2 Kiểm thử chưa chạy / chưa đạt
| Mục | Trạng thái |
|---|---|
| E2E cuối trên code hoàn chỉnh | **Chưa chạy** (bị dừng khi đang build image) |
| Integration (Docker) | Lần gần nhất (trước các merge 4b cuối) **70/72**. 2 lỗi: `test_connector_backend_transition::test_late_webhook_from_old_token_only_template_is_rejected` (pass 5/5 khi chạy riêng ở mọi commit bisect, fail khi chạy cả bộ lúc tải cao — nghi state leak/timing, chưa xác định gốc); `test_p14_chat_worker::test_stopped_chat_worker...` trễ deadline 30 s 0,25 s do tải máy |
| Playwright `rest` group | **Chưa chạy xong** sau các merge cuối |
| Playwright `datasources` group | Không chạy được trong sandbox (cần internet thật từ container) — cần chạy ở môi trường có mạng |
| Integration mới của Wave 4b (`test_db_schema_perf.py`, `test_storage_s3_minio.py` với MinIO, HNSW concurrent end-to-end) | **Chưa chạy** |
| Load test k6 (`scripts/load-test.sh`, `make load-test`) | **Chưa chạy**; `docs/performance-report.md` vẫn chưa có số đo thực |
| Chấm điểm lại scorecard (mục tiêu ≥ 9/10) | **Chưa làm** |

### 2.3 Review findings mức minor còn để lại
- Bulk entity contracts (`list_entity_evidence_bulk`, `get_neighbors_bulk`) chỉ được kiểm bằng compile SQL — cần integration test so sánh với bản singular.
- HNSW concurrent build đặt `statement_timeout=0`: có thể chờ vô hạn nếu có transaction idle — nên đặt timeout hữu hạn hoặc ghi rõ trong `docs/operations.md`.
- Comment cấu hình retention outbox trong `modules/ingestion/_purge.py` trỏ sai tên biến (`OUTBOX_RETENTION_DAYS` mới đúng).
- Wake trễ của dispatcher (worker-cron) giữ cờ debounce 5 s, có thể làm ingestion mới chờ ~5 s; cần test hành vi thật cho 4 continuation path; ghi tài liệu việc cron job không vào DLQ.
- CSP vẫn `script-src 'unsafe-inline'` (đã ghi lý do + lộ trình trong `docs/security.md`).
- Tầng 2 của SQL tool: `SET ROLE` không độc lập với `session_user` (đã ghi trong `docs/security.md`); hướng hardening: kết nối LOGIN riêng cho reader.
- Một số `_actor`/`_admit` có hành vi khác biệt vẫn giữ bản riêng (đã ghi trong `docs/architecture/workspace-access-helpers.md`).
- 104 import chéo module chưa khai báo trong descriptor (allow-list ratchet) — thuộc wp/boundaries.

### 2.4 Việc nhỏ / ngoài phạm vi CI
- `ruff check .` toàn repo còn 30 lỗi, nằm ngoài phạm vi CI: `scripts/dev_workspace.py`, `scripts/check-workspace-contracts.py`, `scripts/check_genui_parity.py`, và skill vendored `.agents/` `.claude/`.
- `npm audit`: 8 advisory mức moderate trong dev tooling (minimatch qua typescript-eslint).
- GitNexus impact/detect-changes không được chạy đầy đủ ở mọi package (một số dùng grep thay thế; các merge lớn được đánh giá critical: worker-cron 37 symbols/16 flows, catch-widening auth/publication 19 flows) — đã có unit test bao phủ, cần e2e xác nhận.
- `origin/main` là lịch sử tách biệt (30 commit, không có merge-base với `feature/fix-ui`) — chưa xử lý, ngoài phạm vi.
- Lớp `.ccr-docker/` (CA proxy + tiktoken cache để build Docker trong sandbox) là cấu hình cục bộ, không commit.

## 3. Thứ tự đề xuất khi tiếp tục
1. Hoàn thành outbox-events (port lên base mới) → boundaries + import-linter.
2. Chạy integration đầy đủ + Playwright `rest`; điều tra triệt để lỗi `test_connector_backend_transition` khi chạy cả bộ.
3. Chạy integration Wave 4b (MinIO, HNSW, bulk contracts).
4. Chạy load test k6, ghi số liệu vào `docs/performance-report.md`.
5. Xử lý các minor ở mục 2.3, rồi chấm lại scorecard (mục tiêu ≥ 9/10).
6. Chạy `datasources` ở môi trường có internet.
