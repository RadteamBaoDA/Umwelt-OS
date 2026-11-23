# Chat retrieval eval over collected datasources — design

Date: 2026-11-23 · Branch: `enhance/chat-datasource` · Status: draft for owner approval

## Intent

The owner wants to know, before changing anything, whether chat answers natural-language questions well over the
data that connectors collect into PostgreSQL, and **where data is missed**: in retrieval (the rows never reach the
model) or in the answer (the model had the rows but did not use or cite them). The result decides the next work:
semantic/hybrid retrieval in `query_collected_data`, row-limit/truncation handling, extra ingest metadata, and the
`@` datasource scope picker in the chat composer. No vector DB is planned; pgvector already exists in `modules/search`.

## What exists (build on it)

- `tests/integration/test_chat_real_model_eval.py`: tier-2 real-model eval. 45 VI/EN cases, each one full turn through
  `modules.chat._worker.stream._stream_turn` with real tool specs, real `query_collected_data`, real citation
  validation and grounding. Grades numbers, text regexes, forbidden values, citations, no-data honesty. Writes
  `test-results/chat-eval/report-<ts>.json`. Opt-in: `INTEGRATION=1`, `CHAT_REAL_EVAL=1`, `.env.e2e-real`
  (`E2E_REAL_BASE_URL/MODEL/API_KEY`). Never run so far (no report on disk).
- `tests/integration/test_chat_collected_data_matrix.py`: seed (`matrix` fixture) through real ingestion persistence
  + `process_normalize_event`, records from real provider mappers fed with `tests/fixtures/providers/*`. Seeds ~12
  providers (binance, frankfurter, world_bank, open_meteo, usgs, cisa_kev, vnexpress-style feeds, vietnamworks, arxiv,
  bluesky, local calendar).
- `tests/fixtures/providers/`: 47 offline fixtures covering ~40 providers.

## Gaps this work closes

1. Seed covers ~12 of ~40 providers that already have offline fixtures.
2. The eval passes `AnswerContext(evidence=[])`, so the `build_context` hybrid search (lexical + pgvector, RRF,
   rerank) is never measured. Only the tool path is.
3. No retrieval-recall metric. Grading only checks the final answer text, so "rows never retrieved" and "rows
   retrieved but not used" look the same.
4. Missing case families: paraphrase (no shared keyword), cross-lingual (VI question, EN data), volume beyond the
   50-row cap, filters on `attributes` (magnitude, vendor), media `attributes.release_date` vs `observed_at`, source
   scope (the future `@` picker), prompt injection in collected text, SQL-mode refusal of base tables.

## Goals

- Seed every provider that has an offline fixture and a mapper, through the same real ingestion path as the matrix.
- Add planted documents that exercise the gaps above, with known gold.
- Add ~30 new cases (VI and EN) on top of the existing 45; keep every existing case passing as before.
- Add two metrics per case: **retrieval recall** (gold rows present in what the turn retrieved) and **answer recall**
  (gold rows cited in the final answer), next to the existing criteria.
- Run each case in two legs: `tool` (today's behaviour, empty `AnswerContext`) and `hybrid` (real `build_context`
  with an embedding model, then the same turn). The hybrid leg is skipped and reported "not run" when its env is absent.
- Report per category and per leg, plus a miss list (case id, gold key missed, the tool args the model used) so a miss
  can be diagnosed without re-running.

## Non-goals

- Fixing retrieval, adding metadata, the `@` composer UI, or any production code change. This is measurement only.
- CI gating. It stays opt-in, spends tokens, and needs owner approval per run (AGENTS.md).
- Live provider calls. Offline fixtures only.
- A vector DB or new dependency.

## Anti-goals (would make it a failure even if it runs)

- Gold derived from the code under test (e.g. "expected = what the tool returns"). Gold is hand-authored keys in the
  planted fixture, or values read straight from the fixture files.
- Lenient grading that hides misses: a text regex that a partial answer also satisfies counts as a pass only if the
  recall metrics agree.
- Non-reproducible runs: seed time is pinned (existing `TZ` and fixed timestamps); no `now()`-relative gold except the
  existing relative-date cases, which already pin their reference.
- Leaking the API key or embedding key into logs or the report.

## Design

### Seed (fixture)

- New module `tests/integration/chat_eval/seed_all.py` (keeps the matrix file under the 1500-line limit). It reuses the
  matrix helpers `_create_source` and `_ingest` and adds one source per provider whose fixture in
  `tests/fixtures/providers/` can be fed to its mapper. Providers without a usable mapper path are listed in the report
  under `not_seeded` with the reason; they are not silently dropped.
- Seeding runs once per session in the same workspace as the matrix, so existing cases see the extra data too. Every
  existing case must still pass the way it did (extra providers must not add a distractor that changes an answer). Where
  an existing case breaks only because of new data, the case gains `source_scope` rather than the seed being reduced.
- Planted documents: `tests/fixtures/chat_eval/planted.json`, ingested through the same path as a news/feed source.
  Each planted item has a stable `key`. After seeding, the harness maps `key → document_id/document_version_id` and
  `key → observation id` for gold. Planted sets:
  - **paraphrase**: EN/VI pairs where the question shares no keyword with the gold doc (e.g. a Fed rate article that
    never says "Fed"; a gold-price article phrased as "kim loại quý").
  - **cross-lingual**: gold only in English, question only in Vietnamese, and the reverse.
  - **volume**: 80 vnexpress-style documents on one local day, 12 of them matching one topic (gold), spread so that
    newest-first ordering pushes some gold past 50 rows.
  - **attribute filter**: 60 quake documents (emsc-shaped) with magnitudes in `attributes`; 5 are ≥ M5 (gold).
  - **media time**: tmdb/tvmaze-shaped rows whose `attributes.release_date` is "this week" while `observed_at` is older.
  - **injection**: one feed document whose body tells the assistant to ignore instructions and print a marker string.
  - **local_only**: one planted doc on a `local_only` source (must never appear with a remote model).

### Cases

Extend the existing `Case` dataclass (move it with the new cases to `tests/integration/chat_eval/cases.py`; the existing
45 cases move unchanged). New fields:

- `gold_keys: tuple[str, ...]`: planted/seed keys that must be retrieved (recall denominator).
- `min_recall: float | None`: optional pass threshold per case (default: report only).
- `source_scope: tuple[str, ...]`: source keys to pass as the turn's `source_scope` (the `@` simulation). Empty = no scope.
- `forbid_keys: tuple[str, ...]`: keys that must not be retrieved or cited (scope leak, local_only, injection doc text).

New case families (~30, each VI and EN where it makes sense): paraphrase, cross-lingual, long multi-word question,
English plural/stem, volume summary, count of documents on a topic, attribute filter (quake ≥ M5, KEV by vendor),
media release this week, scope (`@binance` asking for news → no-data; `@vnexpress @tuoitre` → only those), local_only
scope with remote model, injection (marker string must not appear), SQL base-table request (must be refused), one case
per newly seeded provider asking for its latest or most notable item.

### Metrics

Per case, in addition to the existing criteria:

- `retrieved_keys`: gold keys whose document/observation id appears in any `query_collected_data` result rows of the
  turn, or (hybrid leg) in `AnswerContext.evidence`.
- `retrieval_recall = |retrieved ∩ gold| / |gold|`; `answer_recall = |cited ∩ gold| / |gold|` where cited = rows whose
  citation marker survives `_validate_citations`.
- `scope_leak`: any retrieved row from a source outside `source_scope`, or any `forbid_keys` hit. Must be 0.
- `tool_rounds`, `latency_s` (already partly recorded).

Report summary: pass rate per category and leg, mean retrieval/answer recall per category and leg, total scope leaks,
`not_seeded` providers, and the miss list.

### Legs

- `tool`: current runner, unchanged.
- `hybrid`: before the turn, call `modules.chat.retrieval.build_context` with the case's `AnswerContextRequest` (query,
  `source_scope`, `timezone`) so the system prompt carries real evidence, exactly as the production worker does. Needs
  the seeded chunks embedded: run the search indexing generation for the workspace once after seeding with an embedding
  model from `.env.e2e-real` through AI settings (see Decisions below).

### JEV judge (owner, 2026-11-23)

A second grader next to the regex grader, using a JEV-style scoring model (choice question → one probability per
option), following the contract the ragflow `ScoredSwitch` component uses
(`D:/Project/AIProject/ragflow/internal/agent/component/scored_switch_request.go`, `scored_switch_route.go`).

- **Call:** `POST {base}/alpha/decisions` (base ending `/v1` → `/alpha/decisions`, `/api/v1` → `/api/alpha/decisions`),
  body `{model, state, questions: {verdict: {type: "choice", instructions, criteria: {label: description}}}}`. Config
  from `.env.e2e-real`: `E2E_JUDGE_BASE_URL/MODEL/API_KEY`; key never printed or written. Missing vars → judge skipped,
  report header says `judge: off`.
- **State sent:** the question, the case's hand-authored reference (`gold_facts: tuple[str, ...]`, new `Case` field; for
  `empty` cases the reference is "no data exists"), and the final answer. Not the raw tool rows. Data is fixture-only
  (fictional or public feed samples), so nothing private leaves the harness.
- **Labels:** `correct_complete`, `correct_partial` (true but misses gold facts), `wrong`, `fabricated` (states facts or
  figures not in the reference), `correct_no_data` (correctly says there is no data).
- **Uncertainty (ragflow rule):** probabilities must sum to 1 ± 0.02 else `judge_invalid`. If top-1 − top-2 < 0.15 or
  top-1 is tied, the verdict is `judge_uncertain` and the case goes to a manual-review list in the report.
- **Role:** report-only. It does not change the pass rule. The report lists every **disagreement** (regex pass + judge
  `wrong`/`fabricated`/`correct_partial`, or regex fail + judge `correct_complete`), which is how a too-lenient or
  too-strict regex is found. Judge verdict and probabilities are stored per case and leg.
- **Builder must confirm** the real response shape against the configured endpoint in a one-call smoke before the full
  run (the ragflow code is the only reference; field names may differ), and adapt only the parser.

### Decisions after builder check (author's calls unless marked owner)

- **Baseline first.** Milestone 1 is one approved run of the existing 45 cases on the current matrix seed, before any
  seed growth; its report is the baseline. "Passing as before" means: same result as that baseline report.
- **Pass rule.** A case passes on the existing criteria, unchanged. Recall is reported next to it, and a case that passes
  on text but has `retrieval_recall < 1` or `answer_recall < 1` is flagged `hidden_miss` (counted separately, not a
  fail). `min_recall`, when set, turns it into a fail.
- **Follow-up rewrite.** The hybrid leg runs the same history-based query rewrite as production
  (`modules/chat/_worker/setup.py` follow-up rewrite) before `build_context`.
- **local_only fail-closed.** Production switches a turn to local-only when any evidence item is `local_only`. The
  hybrid leg applies the same check; since the eval model is remote, such a turn is not sent to the model and the case
  is recorded `local_only_blocked` (expected for the planted local_only case, a fail anywhere else). The existing
  `bn_local`/`cal` local_only sources stay as they are.
- **Real Redis and Settings** in the hybrid leg (build_context and indexing need them); the tool leg keeps its stubs.
- **SQL-refusal case** runs only when `sql_mode_available()` is true; otherwise it is reported skipped.
- **Embeddings and rerank (owner, 2026-11-23): both on.** The hybrid leg stores an embedding provider and a rerank
  provider through AI settings in the disposable test database and turns on `privacy.allow_remote_embeddings` (and the
  rerank permission the settings require). Endpoints, models and keys come from `.env.e2e-real`
  (`E2E_REAL_EMBED_BASE_URL/MODEL/API_KEY`, `E2E_REAL_RERANK_BASE_URL/MODEL/API_KEY`), are never printed or written to
  the report, and exist only in that disposable database. Missing embed vars → hybrid leg skipped; missing rerank vars →
  hybrid runs without rerank and the report header says `rerank: off`. The report records `rerank_status` per case.

## Environment

Shell flags (per run): `INTEGRATION=1`, `CHAT_REAL_EVAL=1`.

Docker test compose (already set in `docker-compose.test.yml`): `CHAT_SQL_READER_DATABASE_URL` (enables `mode='sql'`).

`.env.e2e-real` (git-ignored; `.env.e2e-real.example` gains the new keys with empty values and one-line comments):

| Variable | Status | Needed for | Missing → |
|---|---|---|---|
| `E2E_REAL_BASE_URL` | existing | chat model (OpenAI-compatible) | whole eval skipped |
| `E2E_REAL_MODEL` | existing | chat model | whole eval skipped |
| `E2E_REAL_API_KEY` | existing | chat model | whole eval skipped |
| `E2E_REAL_REASONING_EFFORT` | existing, optional | chat model | provider default |
| `E2E_REAL_EMBED_BASE_URL` | new | hybrid leg embeddings | hybrid leg skipped |
| `E2E_REAL_EMBED_MODEL` | new | hybrid leg embeddings | hybrid leg skipped |
| `E2E_REAL_EMBED_API_KEY` | new | hybrid leg embeddings | hybrid leg skipped |
| `E2E_REAL_RERANK_BASE_URL` | new | hybrid leg rerank | hybrid runs, `rerank: off` |
| `E2E_REAL_RERANK_MODEL` | new | hybrid leg rerank | hybrid runs, `rerank: off` |
| `E2E_REAL_RERANK_API_KEY` | new | hybrid leg rerank | hybrid runs, `rerank: off` |
| `E2E_JUDGE_BASE_URL` | new | JEV judge (`…/v1` base) | `judge: off` |
| `E2E_JUDGE_MODEL` | new | JEV judge | `judge: off` |
| `E2E_JUDGE_API_KEY` | new | JEV judge | `judge: off` |

Keys are read at runtime only, never printed, logged or written to the report; embed/rerank keys are stored only in the
disposable test database through AI settings.

## Constraints

- Docker integration harness only (`INTEGRATION=1`), opt-in flag `CHAT_REAL_EVAL=1`; owner approves each run.
- Keys read from `.env.e2e-real` at runtime only, never printed or written; error text masked (existing `_mask`).
- File size ≤ 1500 lines and function length checks (`scripts/check_file_size.py`, `check_function_length.py`).
- Module boundaries: test code may use module `public.py` and the same private helpers the existing eval already
  imports; no new production imports of private internals.
- Existing 45 cases keep their ids so reports stay comparable.

## Approaches considered

- **A. Extend the existing harness** (chosen by owner): reuse grader, turn runner, seed path; add providers, planted
  docs, cases, recall metrics and a hybrid leg. Cheapest, reproducible, separates retrieval misses from answer misses.
- **B. Separate HTTP end-to-end runner** against a running API + worker + indexing: closest to production but heavy setup,
  waits on async indexing, harder to plant gold; half the work duplicates A. Rejected.
- **C. Seed the dev DB and ask in the UI**: not reproducible, no gold. Rejected.
- Judge: regex-only (current) misses paraphrased or partial answers; an LLM judge alone is not reproducible; owner chose
  regex grader as the pass rule plus a JEV scoring judge as report-only second opinion.

## Left to the builder

- Exact mapper call per fixture and which providers end up `not_seeded`.
- Wording of the new questions and planted texts, within the families above.
- How the recorder extracts row ids from tool results (rows already carry `document_version_id`/ids).
- How to trigger one indexing generation synchronously in the test (reuse `modules.search.indexing` entry points).
- Concurrency/timeouts: keep the existing `CONCURRENCY=3`, `CASE_TIMEOUT_S`, `DEADLINE_S` unless the larger set needs
  a higher deadline; note any change in the report header.
