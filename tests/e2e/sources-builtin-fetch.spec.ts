import type { Page } from '@playwright/test';
import { test, expect } from './fixtures';
import { BUILTINS, collectedRecordCount, deleteSources, enableBuiltinViaSettings, findSources } from './sources-helpers';

/**
 * Adds every keyless fixed-scope built-in through "Add built-in" (pick, terms, enable, test fetch) and asserts that the
 * live public endpoint produced records. Mirrors `BUILTIN_PROVIDERS` in modules/connectors/provider_specs.py; a drift
 * check at the end of the file fails when the server offers a built-in missing from this table.
 * Requires outbound public network access from the disposable stack.
 */

/**
 * Providers whose endpoint refuses this network from outside the app (evidence recorded 2026-10-24, Vietnam egress):
 * cisa.gov answers HTTP 403 to every client (curl, browser UA, python) from the host and from the container, while the
 * official mirror raw.githubusercontent.com/cisagov/kev-data answers 200. The app must then fail with the readable code
 * below; on a network where the endpoint is reachable the normal records assertion applies instead.
 */
const EXTERNAL_BLOCK: Record<string, string> = { cisa_kev: 'provider_rejected' };

/**
 * Providers throttled by the remote side: GDELT answers 429 ("one request every 5 seconds") for minutes after a burst.
 * Waiting the full retry ladder cost 8 min and pushed the suite past its global timeout (2026-10-28), so a source still
 * parked on `provider_rate_limited` after this wait counts as a remote throttle the app handled, not a failure.
 */
const SLOW_MS: Record<string, number> = { gdelt_economy: 180_000 };

/** True when the built-in source is parked on a retryable remote 429. */
async function remoteThrottled(page: Page, id: string): Promise<boolean> {
  const builtins = await (await page.request.get('/api/v1/connectors/builtins')).json() as { id: string; source_id: string | null }[];
  const sourceId = builtins.find((b) => b.id === id)?.source_id;
  if (!SLOW_MS[id] || !sourceId) return false;
  const source = (await findSources(page, '')).find((s) => s.id === sourceId);
  return [source?.collection_error_code, source?.last_error_code].includes('provider_rate_limited');
}

/** Optional `E2E_ONLY=id1,id2` narrows the table while debugging a single provider. */
const only = process.env.E2E_ONLY?.split(",");

for (const [id, label] of BUILTINS.filter(([key]) => !only || only.includes(key))) {
  test(`built-in ${id}: enable and fetch records`, async ({ page }) => {
    test.setTimeout(240_000 + (SLOW_MS[id] ?? 0));
    const enabled = await enableBuiltinViaSettings(page, id, label, SLOW_MS[id] ?? 120_000).catch(async (error: unknown) => {
      if (!(await remoteThrottled(page, id))) throw error;
      return null;
    });
    if (!enabled) {
      test.info().annotations.push({ type: 'remote-throttle', description: 'provider_rate_limited' });
      console.log(`RESULT ${id} :: remote throttle: provider_rate_limited`);
      const builtins = await (await page.request.get('/api/v1/connectors/builtins')).json() as { id: string; source_id: string | null }[];
      const created = (await findSources(page, '')).find((s) => s.id === builtins.find((b) => b.id === id)?.source_id);
      if (created) await deleteSources(page, created.name);
      return;
    }
    const { sourceId, ...outcome } = enabled;
    test.info().annotations.push({ type: 'outcome', description: outcome.text });
    console.log(`RESULT ${id} :: ${outcome.text}`);
    expect(sourceId, 'built-in source created').not.toBeNull();
    try {
      if (!outcome.ok && EXTERNAL_BLOCK[id]) {
        test.info().annotations.push({ type: 'external-block', description: outcome.text });
        expect(outcome.text).toContain(`Test fetch failed: ${EXTERNAL_BLOCK[id]}.`);
        return;
      }
      if (!outcome.ok && id === 'noaa_tsunami') {
        // The NOAA tsunami Atom feed has no entries unless a bulletin is active; the app reports that as collection_incomplete.
        test.info().annotations.push({ type: 'empty-feed', description: outcome.text });
        expect(outcome.text).toContain('Test fetch failed: collection_incomplete.');
        return;
      }
      expect(outcome.ok, outcome.text).toBe(true);
      const count = await collectedRecordCount(page, sourceId!);
      test.info().annotations.push({ type: 'records', description: String(count) });
      console.log(`RECORDS ${id} :: ${count}`);
      expect(count, 'records collected by the test fetch').toBeGreaterThan(0);
    } finally {
      const all = await (await page.request.get('/api/v1/sources?limit=100')).json() as { items: { id: string; name: string }[] };
      const created = all.items.find((s) => s.id === sourceId);
      if (created) await deleteSources(page, created.name);
    }
  });
}

test('built-in table matches the server offering', async ({ page }) => {
  const builtins = await (await page.request.get('/api/v1/connectors/builtins')).json() as { id: string }[];
  expect(builtins.map((b) => b.id).sort()).toEqual(BUILTINS.map(([id]) => id).sort());
});
