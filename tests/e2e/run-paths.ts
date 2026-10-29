/**
 * Per-run artifact locations. Concurrent runs against different disposable stacks must not share the owner session file
 * (each stack has its own owner) or the result folders, so `PLAYWRIGHT_RUN_ID` gives each run its own set.
 */
const runId = process.env.PLAYWRIGHT_RUN_ID;
export const OWNER_STATE = runId ? `playwright/.auth/owner-${runId}.json` : 'playwright/.auth/owner.json';
export const REPORT_DIR = runId ? `playwright-report-${runId}` : 'playwright-report';
export const RESULTS_DIR = runId ? `test-results-${runId}` : 'test-results';
