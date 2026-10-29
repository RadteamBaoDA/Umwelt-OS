import { defineConfig, devices } from '@playwright/test';

import { OWNER_STATE, REPORT_DIR, RESULTS_DIR } from './tests/e2e/run-paths';

/**
 * The live-provider datasource specs alone take ~55 min, so a full run is split into two groups (scripts/test.sh,
 * scripts/dev.ps1) that each get the global timeout and a fresh database. Unset E2E_GROUP runs everything.
 */
const DATASOURCE_SPECS = ['**/dashboard-datasources.spec.ts', '**/sources-builtin-fetch.spec.ts'];
const group = process.env.E2E_GROUP;

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line'], ['html', { open: 'never', outputFolder: REPORT_DIR }]],
  outputDir: RESULTS_DIR,
  globalTimeout: 60 * 60_000,
  maxFailures: 10,
  expect: { timeout: 10_000 },
  use: {
    ...devices['Desktop Chrome'],
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost:3000',
    trace: 'retain-on-failure',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    { name: 'bootstrap', testMatch: 'auth.spec.ts' },
    {
      name: 'system',
      testMatch: group === 'datasources' ? DATASOURCE_SPECS : '**/*.spec.ts',
      testIgnore: ['**/auth.spec.ts', ...(group === 'rest' ? DATASOURCE_SPECS : [])],
      dependencies: ['bootstrap'],
      // Per-project: a test.use in a shared helper module only reaches the first spec file a worker loads.
      use: { storageState: OWNER_STATE },
    },
  ],
  webServer: process.env.PLAYWRIGHT_EXTERNAL_SERVER ? undefined : {
    command: 'npm run dev --workspace apps/web',
    url: 'http://127.0.0.1:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { API_INTERNAL_URL: 'http://127.0.0.1:8000' },
  },
});
