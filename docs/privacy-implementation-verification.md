# Privacy and account deletion verification

Reviewed 21 October 2026 on `feature/fix-ui`, HEAD `b6040e4f7ddb2e475e7cb920769e96d469d20b92`. Changes remain uncommitted. The dirty checkout also contains separate Models/UI work; these results do not approve all worktree changes.

| Plan area | Current evidence | Remaining acceptance |
| --- | --- | --- |
| Service inventory | 61 recipient rows with repository evidence, activation gates, inbound/outbound data and local retention anchors; gateway downstream and SearXNG upstream recipients separated | Actual deployment recipients, configuration and provider practices require inspection |
| Policies | Six canonical English/Vietnamese draft documents; public `/privacy`, `/terms`, `/refunds`; repository references stay readable without broken app URLs; all inventory fragments resolve | Operator identity/contact/jurisdiction and legal review remain required |
| Account API and receipt | Auth, Origin/CSRF, password throttling, exact DELETE, locked member/invitation blockers, atomic disable/session revocation, lost-response receipt and safe status tests | Live PostgreSQL transaction/concurrency acceptance |
| Durable erasure | [152-table ownership map](account-erasure-coverage.md); required graph/filesystem/remote failures cannot advance to success; identity/workspace finalization last | Real database/Neo4j/n8n erasure and worker-crash acceptance |
| Late writes | Neo4j uniqueness/schema proof and transaction fence; independent OS file lock plus permanent deletion flag; queued/run admission fences | Neo4j serialization and remote-filesystem locking need target deployment verification |
| Remote cleanup | Persist original n8n endpoint before dispatch; compare endpoint plus remote ID for peers; background clients resolved from original workspace; unsettled sends block; confirmed GitHub revoke checkpointed before cursor advance | Legacy unknown endpoints or ambiguous provider revocation require operator reconciliation; no unsupported absence guarantee |
| Identity and browser | Explicit peer-settings updater anonymization, including account 1; host-only bootstrap recovery; cross-tab cache/draft cleanup; keyboard/focus tests | Real multi-tab browser E2E and host recovery drill |
| Credentials | Fresh heuristic scan: 419 frontend source files and 986 built/static artifacts, zero candidate files; credential-read settings tests pass | Exact deployed assets, logs and live network traffic unverified |

## Local checks

- Consolidated backend suites: **458 passed, 2 skipped**. The two skipped real-symlink tests require Windows symlink privilege; portable guard tests and real Windows junction/interprocess-lock tests passed. Alembic/Graphiti emitted existing deprecation warnings.
- Focused frontend: **25 passed**; TypeScript and scoped ESLint passed. Ruff passed; mypy reported no issues in 46 production files. The final Redis key-format repair separately passed its tests/Ruff/mypy.
- Offline migration tests passed. Deletion adds `r27_account_deletion` and `r28_n8n_endpoint_lineage`; the current checkout's unrelated Models work extends the single head to `r30_drop_model_alias_columns`. No live migration was applied. A stale migration test was changed to validate r26 ancestry instead of hardcoding it as today's head.
- Next standalone build passed. All six policies plus inventory are packaged. Temporary local standalone HTTP smoke returned 200 for policies/status and 307 to `/login` for anonymous `/app`; anchors and the final technical-marker disclosure were present.
- GitNexus force-refresh: 20,599 symbols, 53,570 edges, 300 flows. A temporary independent Git index included 61 task files, including all 39 new production files; every new production file appeared in `detect_changes`. The actual Git index hash remained unchanged. Whole dirty-worktree analysis reported **CRITICAL**, 198 changed files and 117 flows; it includes substantial separate Models/UI changes. GitNexus has per-file symbol caps and incomplete dynamic edges, so direct caller/source review and focused tests supplement this heuristic report. Existing Auth critical/high admission contracts were preserved or reviewed under the documented operation-only authority.
- Whole-worktree whitespace checking reports trailing whitespace in the separate AI settings message edits; those unrelated edits were preserved. Task source checks are scoped rather than misreported as a clean-worktree audit.

## Unresolved gates and limits

Docker integration/E2E and live provider calls remain owner-gated. No real account deletion, bootstrap recovery, live migration, commit, deployment or key rotation was executed. Provider backups/logs, host environment secrets and exported/third-party copies require separate handling. Technical graph/filesystem deletion markers intentionally survive to fence delayed writers; their narrow retained data are disclosed in both privacy drafts.

The compatible Next16/ESLint9 development lint dependency currently has five HIGH npm advisory entries through the braces/micromatch/fast-glob chain. The offered Next lint14 downgrade is incompatible with ESLint9/the current flat config, so it was not kept as a false fix. This tooling dependency issue remains documented in [the credential review](frontend-credential-audit.md); no claim is made that all dependency vulnerabilities are resolved.
