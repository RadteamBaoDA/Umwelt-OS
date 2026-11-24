import { expect, test } from "./fixtures";
import {
  BUILTINS,
  WIZARD_CASES,
  connectWizardSource,
  enableBuiltinViaSettings,
} from "./sources-helpers";

/**
 * One-off bootstrap for the real-data chat eval collection stack (scripts/eval-collect.ps1 bootstrap).
 * Enables the public keyless built-ins and the wizard providers with default schedules so the stack keeps collecting
 * for days; sources are never deleted. Keyed providers join only when an owner key is present (`E2E_KEY_*`).
 * Skipped unless EVAL_COLLECT is set, so normal e2e runs never touch it.
 */
test.skip(!process.env.EVAL_COLLECT, "EVAL_COLLECT is not set");
// Live keys may be typed in the wizard: keep every artifact that could capture them off, unconditionally.
test.use({ trace: "off", screenshot: "off", video: "off" });
test.setTimeout(300_000);

/** Excluded built-in: job listings are personal-scope data the eval does not use. */
const SKIP_BUILTINS = new Set(["itviec_jobs"]);
/** Keyless (or demo-key) wizard providers. */
const KEYLESS_WIZARD = new Set([
  "rest",
  "google_news",
  "openalex",
  "arxiv",
  "huggingface",
  "github_releases",
  "youtube",
  "open_meteo",
  "coingecko",
]);
/** Keyed wizard providers, enabled only with a live owner key. */
const KEYED_WIZARD = new Set([
  "fred",
  "finnhub_news",
  "newsapi",
  "openweathermap",
  "openaq",
  "nasa_firms",
  "alpha_vantage",
]);

for (const [id, label] of BUILTINS.filter(([id]) => !SKIP_BUILTINS.has(id))) {
  test(`builtin ${id}`, async ({ page }) => {
    const outcome = await enableBuiltinViaSettings(page, id, label);
    expect.soft(outcome.ok, `${id}: ${outcome.text}`).toBe(true);
  });
}

for (const c of WIZARD_CASES.filter(
  (w) => KEYLESS_WIZARD.has(w.id) || (KEYED_WIZARD.has(w.id) && w.live),
)) {
  test(`wizard ${c.id}`, async ({ page }) => {
    const outcome = await connectWizardSource(page, c, `evalc-${c.id}`);
    // The message never includes the key; only the outcome text is reported.
    expect.soft(outcome.ok, `${c.id}: ${outcome.text}`).toBe(true);
  });
}
