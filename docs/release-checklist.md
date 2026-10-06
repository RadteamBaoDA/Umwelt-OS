# Umwelt-OS release checklist

Status: preparation in progress, 2026-10-06. This checklist records release gates; it does not certify deployment or product acceptance.

## Current integration boundary

Local develop `84c25df` includes P01–P11, Collector/MCP, R07, R12, R14 and R15, plus bounded owner-export contracts (`3215939`) and serialized Chat history privacy (`bd79c06`). These integrated tasks passed their prescribed builds and independent source reviews. Additional accepted milestones include Entities/Relationships export (`781c0eb`), onboarding API/UI (`de132d8`) Chat/Memory deletion (`94c7c1b`), retained Timeline/Observations export (`72d72c6`) bounded Chat expiry (`32a6ccc`), demo/named workspace source (`655331b`), visible branding/EN–VI labels (`44cf45a`) and individual-Document raw cleanup (`3d2c51e`). Backup/admission/recovery and the bounded portable-export aggregator are integrated at `5212def`; complete saved GadgetDefinition/eligible retained DailyBrief/schedule coverage is integrated at `bce670b`, and bounded exact Chat copied-evidence cleanup at `f6da2e2`. Document deletion receipt UI (`4416843`), Source canonical/linked-child cleanup (`fe601bc`) and mobile/full Dashboard reconciliation (`98365a1`) are integrated. Document-derived Memory cleanup is integrated at `84c25df` from accepted `adf52e1`; Agent/other copied-owner stages, historical and Source-local coverage, final release work and runtime mobile/accessibility acceptance remain incomplete. Demo/named workspace seed/reset source is integrated at `655331b`; runtime isolation acceptance remains deferred. Recheck the execution ledger and actual Git revision before producing a release artifact.

## Required evidence

| Gate | Evidence required | Current state |
| --- | --- | --- |
| Production scope | Every original phase and R01–R16 task has an accepted code/build/review receipt and integrated revision | Incomplete |
| Final composed build | Prescribed `./scripts/dev.ps1 build` or `make build`, exact source revision, output and exit code | Pending final composition |
| Clean install | Disposable clean database migrations, owner bootstrap/login, approved seed, authenticated dashboard/chat/settings | Deferred test stage |
| Upgrade | Preserved Phase0 deployment data and secrets, migration chain, upgrade and recovery evidence | Deferred test stage |
| Realtime/proxy | Authenticated SSE replay/reset, long-lived proxy response and client status | Runtime pending |
| Privacy/deletion | Consent, immediate access revocation, exact evidence cleanup and durable retry receipts | P12 implementation incomplete |
| Backup/restore | Encrypted archive, protected keys, isolated restore, component readiness and explicit recovery | Source integrated; restore acceptance deferred |
| Provider integration | Explicit live Google, connector, OmniRoute, graph/n8n/browser receipts for enabled capabilities | Pending |
| Mobile/accessibility | Viewport, keyboard/focus, screen-reader labels, edit-grid and drawer/full-chat behavior | Deferred test stage |
| Capacity | Measured 2-core/8GiB workload, configuration, concurrency, memory/CPU/latency and OOM/disk evidence | Target host unverified |
| OSS | Exact dependencies/copied source revisions, licenses, modifications and notices | Final inventory reconciliation pending |

## Operator preparation

- Follow [deployment.md](deployment.md) and [README.md](../README.md) for the supported installation commands. Validate them against the final integrated source before release.
- Keep PostgreSQL, Redis, API internals and Docker socket private; expose the web origin through HTTPS with secure cookies and exact origin configuration.
- Apply the authenticated realtime proxy settings documented in deployment.md. Check Chat streaming paths too during final proxy acceptance.
- Bootstrap the local owner before linking Google sign-in. Login consent and Gmail collection consent are separate operations.
- Configure approved collectors, MCP servers and remote AI through the native Settings UI. Account, appearance and language remain in the user menu.
- Use explicit fictional seeding only; preserve stable demo identities and owner edits/deletions. Named development workspace reset is implemented and source-reviewed at `655331b`; actual isolation/reset acceptance remains deferred, and it must not be executed against owner data during code work.
- Use the implemented commands in [backup-recovery.md](backup-recovery.md) for encrypted backup, isolated restore, same-operation recovery and retained-project cleanup. Their source is integrated at `5212def`; container builds and isolated validation do not establish successful production recovery.

## Acceptance record

```json
{
  "functional_acceptance": "pending",
  "live_integrations": "pending",
  "target_capacity": "pending",
  "restore_verified": false
}
```

Run tests only after all original and supplemental production code/build/review closes. Record actual command, source revision, environment, outcome and unresolved gate for each result. Historical Phase0 evidence does not certify current auth, UI, provider or recovery behavior. Release, push and deployment require their own authorized action.


