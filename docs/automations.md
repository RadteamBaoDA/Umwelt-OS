# Automations

Owner-configured rules: one **trigger**, optional **conditions** (AND, no scripting), and 1-10 ordered **actions**. Rules live in Settings > AI & Ommi Router (advanced). Every save appends an immutable revision; a run always cites the revision that fired it. Four disabled fictional examples are installed by the explicit demo seed (`python -m modules.knowledge.documents.seed`, never at startup, once per owner via the `bbd-os.demo.phase-10` receipt, so edited or deleted examples never return): new document alert, task-due reminder, weekday brief, and a weekly `run_agent` check.

## Triggers and declared fields

Conditions may only reference the fields a trigger declares (`modules/automations/conditions.py`, `TRIGGER_FIELDS`). Operators: `eq ne in gt gte lt lte`; ordered operators need numeric fields. A field the producer could not supply evaluates as `missing_field` (not matched).

| Trigger | Declared fields | Notes |
|---|---|---|
| `schedule` | `weekday` (0=Sunday), `hour` | 5-field cron + IANA timezone |
| `new_event` | `event_type`, `source_id`, `importance` | timeline events |
| `new_document` | `source_id`, `source_type`, `mime_type`, `title` | metadata only, never content |
| `entity_changed` | `entity_id`, `entity_type`, `change` | |
| `task_due` | `status`, `goal_id`, `hours_until_due`, `created_by_automation` | `lead_minutes` window |
| `goal_deadline` | `goal_id`, `status`, `days_until_deadline`, `progress` | `lead_days` window |
| `connector_sync_result` | `source_id`, `status`, `new_items` | |
| `webhook` | `event` | Authenticated source alias, durable key dedupe |

## Webhook triggers

Webhook triggers use an owner-created alias; rules carry an alias, never a URL. Enter the alias in the rule editor, then
issue its inbound bearer there. Outbound `call_webhook` actions remain limited to `WEBHOOK_PROFILES`. The owner-only
`POST /api/v1/automations/webhook-credentials/{alias}` response shows the random token once and expires it after 90 days;
issue again to rotate. `DELETE` on the same path revokes it. Tokens are stored only as hashes. Keep the token in the sending
system's secret store; it is separate from owner cookies and outbound `call_webhook` credentials.

Send `POST /api/v1/automations/inbound/{alias}` with `X-Umwelt-Webhook-Token`, a unique `Idempotency-Key` (printable ASCII,
up to 128 characters), `Content-Type: application/json`, and body `{"event":"..."}`. The body is capped at 64 KiB, but only
the declared `event` field (up to 2,000 characters) is stored. Duplicate delivery with the same key and event returns
`accepted: false`; reusing a key for a different event returns 409. The trigger inbox commits before 202, so retries are
durably deduped. The automation worker still checks the current enabled rule revision and module fences before actions.

## Schedule ownership (single owner per logical job)

The daily brief is a logical job (`daily_brief`) with exactly one scheduler owner, recorded on the P08 `brief_schedules` row:

```json
{"logical_job":"daily_brief","schedule_owner":"internal_brief","automation_id":null}
```

`GET /api/v1/dashboard/briefs/schedule/ownership` returns this record. The default is `internal_brief` (the ARQ cron, 07:00 Asia/Ho_Chi_Minh unless edited).

- A rule "wants" the slot when it is enabled, has a `schedule` trigger and a `generate_brief` action.
- **Enabling** such a rule (PATCH `enabled=true`, or creating it enabled) explicitly transfers ownership to it, in the same transaction as the enable. The internal cron (`run_due_brief`) then skips.
- **Disabling or deleting** the owning rule, or editing it so it no longer wants the slot, returns ownership to `internal_brief` in the same transaction.
- If another automation already owns the slot, enabling a second one fails with 409 `brief_slot_owned`; disable the first.
- Editing an already-enabled rule into a brief schedule is refused (422): pause it, edit, then enable. Transfer is never a side effect of an edit.
- Writes go through the existing owner-auth + CSRF PATCH/DELETE routes. The owner can still edit the internal schedule time (`PUT /briefs/schedule`); it is simply inactive while an automation owns the slot.
- Note: a brief rule that was enabled before this feature does not hold the slot until it is paused and re-enabled.

Collection/source polling schedules remain owned by n8n; automations never duplicate them.

## Missed-run behavior

The worker job `reconcile_automation_runs` runs every 5 s: schedule tick, trigger producer sweeps, inbox fan-out, dispatch. For schedule rules (`scheduler.tick`), policy is `coalesce`:

- Slots missed while the worker was down collapse into **one** run, keyed by the first missed slot; the next slot then jumps past now. No unbounded replay.
- A new revision (any edit, pause/resume) resets the schedule from now with no catch-up.
- DST-gap local times are skipped; slot state advances in the same transaction that inserts the run, and the run identity `(automation, revision, trigger_key)` is unique, so a slot cannot fire twice.
- Event producers (documents, events, entities, sync results) use durable cursors that start at "now" and read with a 30 s overlap (deduped); `task_due`/`goal_deadline` sweep a due window. They do not backfill history from before a rule existed.

## Permission and approval model

- All routes require the owner session; writes also require CSRF.
- `create_notification`, `create_task`, `generate_brief` run without approval, inside the rule's own transaction ledger.
- `run_agent` and `call_webhook` always stop at `awaiting_approval`, even when scheduled. The owner approves or denies per action (`POST /automations/runs/{run_id}/actions/{n}/decision`).
- Approval is **bound** to the exact action definition of the run's revision (hash). For `call_webhook` it is also bound to the destination: the webhook profile revision is part of the hash and is re-checked at decision time and just before sending; a changed destination drops the action (`stale_destination`). Approvals expire after `approval_expiry_hours`; waiting never consumes the run's attempt budget.
- `run_agent` runs the chosen agent profile in a per-rule Chat conversation with the approving owner's session; the P07 tool gates inside that run still apply. Agents can only propose rules (`automations.create`, owner-approved, always created disabled); they never enable them.
- Every action re-checks the **fence**: rule live, enabled and still at the run's revision. Pausing, editing or deleting drops queued work (`dropped`) and keeps history.
- Uncertain outcomes (webhook `in_flight` found after a restart) become `requires_review` and are never resent.

## Action limits

| Limit | Value |
|---|---|
| Actions per rule | 10 (conditions: 20) |
| Cooldown (non-scheduled) | 60 s: later runs are queued behind the previous one |
| Rate cap | 30 runs per rule per hour (excess recorded as `skipped: rate_limited`) |
| Cron floor | 120 s between slots (300 s if the rule has `run_agent`); enforced at save |
| Chain depth | 5; deeper runs are recorded `skipped: depth_exceeded` |
| Retries | 4 worker passes per run; 3 tries per retry-safe action, exponential backoff |
| Loop guards | a rule never reacts to its own emission (`self_origin`); known self-triggering pairs rejected at save and skipped at dispatch |

Actions run in strict order and stop on the first non-success.

## Recovery after restart

PostgreSQL is the queue authority. Queued runs are (re)enqueued each pass with a generation-scoped job id; a run stuck `running` with no heartbeat for 200 s is requeued. Completed actions are not repeated (per-action ledger; notification and task writes commit with their ledger row). Retry-safe actions resume; an action found `in_flight` (webhook) goes to `requires_review`. Preview and "Run now" never bypass any of the above; Run now is idempotent per client request id.
