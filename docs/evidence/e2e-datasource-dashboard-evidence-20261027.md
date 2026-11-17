# E2E datasource and dashboard evidence — 2026-10-27

Repository: D:/Project/Umwelt-OS, HEAD 0da1e5b5a58899122a4d77a4f4871e770cd2fcb2; working tree contained pre-existing modifications and untracked tests. Results apply to that working tree, not the commit alone.

## Runs

- `./scripts/dev.ps1 test -E2eOnly -E2eTarget 'dashboard-datasources|sources-.*\.spec'`: 179 passed, 10 failed, 2 skipped, 14 did not run; 53.0 minutes. Stopped at configured maxFailures=10. See `playwright-report-datasource-dashboard-20261027/index.html` and `test-results-datasource-dashboard-20261027/`.
- `./scripts/dev.ps1 test -E2eOnly -E2eTarget 'sources-wizard.spec.ts'`: 22 passed, 1 failed (arxiv); 6.5 minutes. This reran the wizard file containing the 14 cases not run above. See `playwright-report-datasource-wizard-followup-20261027/index.html` and `test-results-datasource-wizard-followup-20261027/`.
- Each run used a unique disposable Docker Compose project and database, Playwright one worker, and COMPOSE_PARALLEL_LIMIT=1. Compose cleanup removed each test project's containers, network and volumes. Development containers remained running.

## Failures and limitations

- Wikipedia Most-read fetch returned provider_response_invalid twice; its dashboard gadget could not render a row. Wikipedia Top Views did render and fetch successfully. Cause not established.
- arXiv wizard fetch returned provider_response_invalid in both runs. Its gadget was skipped by the existing suite. Cause not established.
- Fake MCP connection was healthy, but collection returned HTTP 503: Scoped MCP collector contract is pending.
- Fake Home Assistant and FHIR drafts validated, but activation returned HTTP 409: Source is inactive or private; it cannot be activated. Their dashboard gadgets did not render rows.
- GDELT was rate-limited in the first dashboard sequence, then a separate builtin test collected 5 records. CISA KEV returned provider_rejected and its dashboard test is excluded by the existing suite. NOAA tsunami gadget is skipped by the suite even though the separate fetch obtained a record.
- Social/import dashboard tests rendered rows for Bluesky, Mastodon, Markdown notes, finance CSV and manual upload. Credentialed tests use dummy credentials or absent OAuth clients; they do not prove live authorized connections. Local-service tests use fake services.
- The HTML reports contain per-test status, output and error contexts. No screenshot/video/trace capture was configured, so these reports are not visual recordings of every passing case. The PowerShell transcript files contain only transcript metadata and should not be treated as full execution logs.
- Existing test selection is not proof of every possible datasource, dashboard operation, browser, permission boundary, production environment or real external account. No product code was changed.
