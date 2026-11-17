# Service level objectives

Scope: the single-user, self-hosted deployment on the documented target host (2 vCPU / 8 GiB RAM, remote inference; see [performance report](performance-report.md) and [deployment](deployment.md)). The objectives are engineering targets for the owner's own experience, not contractual commitments.

Status: **proposed targets, not yet validated.** The load profile in `tests/load` encodes every number below as a k6 threshold, and `make load-test` evaluates them against a disposable Compose stack. Until a run on the target host is recorded in the [performance report](performance-report.md), a number here is a hypothesis; tighten or relax it from the first measured baseline and change the threshold in `tests/load/lib/thresholds.js` in the same commit.

## Measurement conditions

- Stack: the disposable test Compose project (`docker-compose.yml` + `docker-compose.test.yml`), started by `scripts/load-test.sh`, with `tests/integration/fake_model.py` as the only model provider. Provider latency is therefore zero by construction, which is what "excluding provider latency" means for the chat SLOs. Provider and network latency are separate budgets recorded in the performance report (`latency.provider_ms`).
- Load is bounded by the API's own per-minute budgets (`chat_send` 30, `ingest_upload` 20, `search` 60); the scenarios stay at one third to two thirds of each budget. A 429 counts as a failed request, so a run that exceeds a budget fails the availability SLO instead of hiding the problem.
- Dataset: 8 seed documents plus the documents the ingest scenario uploads. Dashboard/documents latency on a large corpus is a separate measurement (performance report, "Required workloads").
- Windows: a run is the 3.5 minute `all.js` profile; the SLOs apply per run, not per month, until a monthly production signal exists (see the error budget policy).

## SLIs and SLOs

| ID | SLI (what is measured) | SLO (target) | k6 metric / threshold |
|---|---|---|---|
| SLO-1 | **API availability**: share of requests answered 2xx/3xx across all scenarios (login, reads, chat, ingest). | at least 99.5 % successful | `http_req_failed` `rate<0.005` |
| SLO-2a | **Dashboard read latency**: `GET /api/v1/dashboards`. | p95 < 400 ms, p99 < 800 ms | `http_req_duration{endpoint:dashboard}` |
| SLO-2b | **Search latency**: `POST /api/v1/search`, lexical mode (hybrid adds the embedding provider, which is out of scope here). | p95 < 800 ms, p99 < 1.5 s | `http_req_duration{endpoint:search}` |
| SLO-2c | **Documents list latency**: `GET /api/v1/documents?limit=50`. | p95 < 300 ms, p99 < 600 ms | `http_req_duration{endpoint:documents}` |
| SLO-3 | **Chat time to first token (TTFT)**: from sending a message to the first token reaching the client, fake model, provider latency excluded. | p95 < 2 s, p99 < 4 s | `chat_ttft_ms` |
| SLO-3b | **Chat send latency**: `POST .../messages` accepted (202). | p95 < 500 ms | `chat_send_ms` |
| SLO-4 | **Ingest lag**: upload of a small Markdown document until a lexical search returns it. | p95 < 30 s, p99 < 60 s | `ingest_lag_ms` |
| SLO-5 | **SSE stream open** (proxy for SSE delivery latency; see notes): time for `GET /api/v1/responses/{id}/events` to open (response headers), which bounds the delay before any event can be delivered. | p95 < 500 ms | `chat_sse_open_ms` |
| SLO-6 | **Worker queue age**: time from the upload being accepted until its ingestion run leaves `queued`. | p95 < 5 s | `ingest_queue_age_ms` |
| SLO-7 | **Scenario correctness**: business checks in the scenarios (status codes, stream finished, document became searchable). | at least 99 % pass | `checks` `rate>0.99` |

### Notes on how each SLI is measured

- **TTFT.** k6's core HTTP module buffers a response until it ends, so individual SSE frames cannot be timestamped. The chat scenario therefore sends `[fake:tokens=1]`: the fake model streams one token, so the time to the end of the stream equals the time to the first token and is a slight upper bound (it includes the worker's 0.4 s stream flush and the `message.done` frame). One in five iterations streams 20 tokens and records `chat_completion_ms` (informational, no threshold). If a build with the `xk6-sse` extension is available, replace the measurement in `tests/load/chat.js` with a per-frame timestamp and keep the metric name.
- **SSE delivery.** `chat_sse_open_ms` is the time to response headers of the events request. Delivery of the realtime channel (`/api/v1/realtime/events`, reconnect with `Last-Event-ID`) is covered by the long-lived-tab scenario in the performance report's measurement plan and by `tests/e2e/realtime-status.spec.ts`; it is not part of the k6 profile. Follow-up: true per-event delivery latency (server emit to client receipt) needs per-frame timestamps, i.e. the `xk6-sse` extension or a server-side emitted-at field on `message.done`; until then SLO-5 only bounds stream open.
- **Worker queue age.** The platform exposes no queue-age gauge today. The ingest scenario polls the run once per second, so the metric has about 1 s resolution and includes one API round trip. A server-side gauge (oldest queued job age per arq queue) would remove that resolution limit and is the intended replacement.
- **Availability** is measured from the client side over the run; liveness (`/health`) and readiness (`/api/v1/system/ready`) are separate probes used by Compose healthchecks.

## Error budget policy

The availability SLO allows 0.5 % failed requests. For a single owner this is a per-run and, once production monitoring exists, a rolling 30-day budget (about 3.6 hours of full outage per 30 days at 99.5 %).

1. **Within budget**: ship normally. A k6 run that meets every threshold is the release gate for performance-sensitive changes (chat, search, ingestion, realtime, database schema).
2. **Budget below 50 % remaining** (measured over the last 30 days of owner-visible failures, or a failed threshold in two consecutive runs): feature work that touches the affected path pauses until the cause is identified; only fixes and instrumentation merge.
3. **Budget exhausted** (more than 0.5 % failures in the window, or a latency SLO missed by more than 25 % in a run): stop merging changes to the affected path, open a fix with a regression test or a documented budget change, and re-run `make load-test` before resuming.
4. **Changing an SLO** requires a measured baseline from the target host in the performance report and a matching threshold change in `tests/load/lib/thresholds.js`; never relax a threshold only to make a run pass. `SLO_LATENCY_SCALE` exists for slower development machines and results obtained with it are not SLO evidence.
5. **Excluded from the budget**: planned maintenance (migrations, restores), provider outages (the circuit breaker is expected to fail chat fast; see the architecture decisions), and traffic above the documented per-minute budgets.

## How to run

```bash
make load-test                              # full profile (all.js), writes test-results/load/<UTC timestamp>/
LOAD_DURATION_SCALE=0.2 make load-test      # smoke run (about 40 s of load)
LOAD_SCENARIO=chat.js make load-test        # one scenario: read_mix.js, chat.js, ingest.js
```

On Windows use `./scripts/dev.ps1 load-test` (same environment variables). The run needs Docker with Compose and the pinned `grafana/k6` image; it starts a uniquely named `bbd-os-test-*` project and removes its own volumes on exit. The output directory contains `summary.json` (k6 `--summary-export`), `environment.txt`, `compose-ps.txt` and `compose-logs.txt`. k6 exits with status 99 when a threshold is crossed. Validate the scripts without Docker using `node scripts/check-k6-scripts.mjs`.
