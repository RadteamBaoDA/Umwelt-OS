// Full load profile: read-mix, search, chat and ingest together (what `make load-test` runs).
// Per-scenario files can be run alone for debugging: read_mix.js, chat.js, ingest.js.
import { sloThresholds } from './lib/thresholds.js';
import { setupRun } from './lib/run.js';
import { readMixScenarios, readMixExec, searchExec } from './read_mix.js';
import { chatScenarios, chatExec } from './chat.js';
import { ingestScenarios, ingestExec } from './ingest.js';

export const options = {
  scenarios: { ...readMixScenarios, ...chatScenarios, ...ingestScenarios },
  thresholds: sloThresholds,
  noCookiesReset: true, // keep the per-VU session cookies installed by ensureVuSession across iterations
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

/** k6 setup: owner bootstrap, single login, fake model configuration. */
export function setup() {
  return setupRun({ fakeModel: true, seed: true });
}

export { readMixExec, searchExec, chatExec, ingestExec };
