# Agents, specialists and bounded browser reads

Scope: Phase 7. Management UI is reached from full Chat and Settings > AI (advanced), not main navigation.

## Specialists

Supervisor, Knowledge, Research, Personal, Project, News and Planning share one LangGraph harness
(`modules/agents/harness.py`). Automation stays disabled until Phase 10. Each profile has an editable
prompt, model alias, exact registered tool permissions (name, version, fingerprint) and source scope;
saves are optimistic (`expected_revision`). Capabilities that depend on unregistered tools (Phase 8 tasks
and goals, browser) are shown as unavailable with a reason, never faked.

## Runs and approvals

`POST /api/v1/agents/{id}/runs` starts a durable run (20 steps, 10 tool calls, 300 s active by default).
Cancellation and permissions are re-checked before every tool. External-write and destructive tools wait
for an immutable, expiring approval; uncertain outcomes become `requires_review`. Full Chat shows
concise tool status (name and state only) and pending approvals. Hidden reasoning is never stored.

## browser.read

A READ_ONLY tool: `{source_id, max_pages 1-3}` for an owner-enabled web source. It is a bounded static
read (no actions) executed by the separate credential-isolated browser service, with page, byte, time and
request-count limits reserved against the run budget. Every network request is authorized by a callback
to the API against current owner session, run, profile and source grant; revoked or changed sources,
config or grants invalidate in-flight jobs. Page text is untrusted evidence. Results are erased when the
run, conversation or source is purged; job ids and digests remain as tombstones.

The tool is not registered (so specialists report browser reading unavailable) until browser network isolation and an
OmniRoute browser/tool capability probe are accepted; the control callback refuses `register` and
`authorize` while unverified. Static reads retain no session state. Remote-work guards expire shortly after the 45 s job bound, and ingestion defers (retries) while one is live.

Lock order across modules: Sources, then Agents, then Tools.

## Supervisor handoff

`agents.handoff` is a READ_ONLY tool with `{specialist, request}` that only the Supervisor profile may select
(saved profiles must add it explicitly; the default Supervisor includes it). It runs one specialist sub-run through
the same compiled workflow (`run_specialist_handoff` in `modules/agents/harness.py`) and returns only the specialist's
final answer (12,000 characters at most) and up to 20 citations; the specialist's source fences join the parent's, so
the final answer is revalidated against them.

- Depth is 1: the specialist never receives `agents.handoff`, and Automation and Supervisor are refused as targets
  (Automation belongs to Phase 10).
- At most once per run, judged from durable tool-call rows (a replay of the same slot is not a second use). The call
  must be the only tool call of its model turn, so the specialist's durable tool ordinals continue the parent's counter.
- It shares the parent's run row, claim, PostgreSQL lease and active-time clock. Every step, tool call and second the
  specialist uses counts against the parent's 20 steps, 10 tool calls and 300 s, and its token usage is added to the
  parent's. Parent cancel, session expiry, lease loss or limit stops it at the next fence.
- The specialist uses its own model alias, prompt, tools and source scope, intersected with the Supervisor's sources.
  A disabled, unconfigured or tool-less specialist is refused (`tool_unavailable`) with no fallback.
- Effects are not offered to the specialist: `webhook.send` and `browser.read` are excluded, so no approval can be
  bypassed or left pending inside a handoff. Run the effect-capable specialist directly to get the approval card.
- The specialist is not checkpointed on its own. If a worker segment is cut mid-handoff, replay re-runs the read-only
  specialist and its budget is charged again.
- No schema change: there is no child run row or `parent_run_id`.
