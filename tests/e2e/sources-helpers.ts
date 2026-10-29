import type { Locator, Page } from '@playwright/test';
import { expect } from './fixtures';

type Outcome = { ok: boolean; text: string };

/** Headers an owner write needs: Playwright's request context sends no Origin (browsers do). */
export async function writeHeaders(page: Page) {
  const session = await (await page.request.get('/api/v1/auth/session')).json();
  return { 'X-CSRF-Token': session.csrfToken as string, Origin: new URL(page.url()).origin };
}

/**
 * Waits for the wizard's final "Save and test" result. Resolves with the visible status or alert text:
 * `ok` is true for "Test fetch succeeded" (new data or reachable-without-new-items), false for "Test fetch failed: <code>".
 * A transient network code (DNS/timeout/5xx) is retried once with the dialog's "Try again", as an owner would.
 */
export async function waitForFetchOutcome(dialog: Locator, timeout = 120_000, retries = 1, slowRetryCodes: string[] = []): Promise<Outcome> {
  const success = dialog.getByRole('status').filter({ hasText: /Test fetch succeeded/ });
  const failure = dialog.getByRole('alert').filter({ hasText: /Test fetch failed/ });
  await expect(success.or(failure).first()).toBeVisible({ timeout });
  if (await success.count()) return { ok: true, text: (await success.first().innerText()).trim() };
  const text = (await failure.first().innerText()).trim();
  // `slowRetryCodes` are provider rejections that clear on their own (remote throttle): retried once after a pause.
  const slow = slowRetryCodes.some((code) => text.includes(`Test fetch failed: ${code}.`));
  if (retries > 0 && (slow || /Test fetch failed: (provider_timeout|provider_unavailable)\./.test(text))) {
    console.log(`RETRY after transient failure :: ${text}`);
    if (slow) await dialog.page().waitForTimeout(15_000);
    await dialog.getByRole('button', { name: 'Try again' }).click();
    await expect(failure).toHaveCount(0);
    return waitForFetchOutcome(dialog, timeout, retries - 1, slowRetryCodes);
  }
  return { ok: false, text };
}

/**
 * Number of records the source's collection produced: the largest per-stage `result_count` / `normalized_count` seen on
 * its ingestion runs. Normalization runs asynchronously after the fetch settles, so poll until a run reports records
 * (or `timeout` passes) and return what was last seen.
 */
export async function collectedRecordCount(page: Page, sourceId: string, timeout = 60_000): Promise<number> {
  let last = 0;
  await expect.poll(async () => {
    const res = await page.request.get(`/api/v1/ingestion/sources/${sourceId}/runs`);
    if (!res.ok()) return -1;
    const body = await res.json() as { current_run: Run | null; items: Run[] };
    const runs = [...(body.current_run ? [body.current_run] : []), ...body.items];
    last = Math.max(0, ...runs.flatMap((run) => run.stages.map((st) => Math.max(st.result_count ?? 0, st.normalized_count ?? 0))));
    return last;
  }, { timeout, message: 'ingestion run reports records' }).toBeGreaterThan(0).catch(() => undefined);
  return last;
}

type Run = { stages: { result_count: number | null; normalized_count: number }[] };

/** Finds sources whose name starts with `prefix` (names are unique per test). */
export async function findSources(page: Page, prefix: string) {
  const res = await page.request.get('/api/v1/sources?limit=100');
  const body = await res.json() as { items: { id: string; name: string; status: string; last_error_code: string | null; collection_error_code: string | null }[] };
  return body.items.filter((s) => s.name.startsWith(prefix));
}

/** Deletes every source with the given name prefix so the disposable app is left clean (best effort). */
export async function deleteSources(page: Page, prefix: string) {
  const headers = await writeHeaders(page);
  for (const source of await findSources(page, prefix)) {
    await page.request.delete(`/api/v1/sources/${source.id}`, { headers });
  }
}

/** Acknowledges the provider terms panel (declares personal use) if it is shown in the open dialog. */
export async function acknowledgeTerms(dialog: Locator) {
  const panel = dialog.getByRole('region', { name: 'Provider terms' });
  if (!(await panel.count())) return;
  await panel.getByRole('combobox', { name: 'Deployment use' }).click();
  await dialog.page().getByRole('option', { name: 'Personal' }).click();
  await panel.getByRole('checkbox').click();
  await panel.getByRole('button', { name: 'Acknowledge terms' }).click();
  await expect(panel.getByText('Terms acknowledged.')).toBeVisible();
}

/** One wizard-connectable provider scenario shared by the wizard, fetch and dashboard specs. */
export type WizardCase = {
  id: string;
  /** Provider card label as rendered by the add-source wizard. */
  card: string;
  /** `fetch`: live keyless endpoint must return records. `rejects`: dummy credential must fail cleanly. */
  expect: 'fetch' | 'rejects';
  /** Credential for the Connect step (dummy), if the provider needs one. */
  key?: string;
  /** Scope/URL inputs for the Choose-data step: element id -> value. */
  fields?: Record<string, string>;
};

export const WIZARD_CASES: WizardCase[] = [
  { id: 'rest', card: 'REST API', expect: 'fetch', fields: { 'source-config-url': 'https://hn.algolia.com/api/v1/search?query=oil&tags=story', 'source-items-path': 'hits', 'source-id-field': 'objectID', 'source-content-field': 'title', 'source-title-field': 'title' } },
  { id: 'rss', card: 'RSS / Atom', expect: 'fetch', fields: { 'source-feed-url': 'https://feeds.bbci.co.uk/news/world/rss.xml' } },
  { id: 'web', card: 'Web page', expect: 'fetch', fields: { 'source-config-url': 'https://example.com/' } },
  { id: 'google_news', card: 'Google News RSS', expect: 'fetch', fields: { 'source-scope-news_query': 'oil price' } },
  { id: 'sec_edgar', card: 'SEC EDGAR filings', expect: 'fetch', fields: { 'source-scope-sec_cik': '0000320193', 'source-scope-sec_contact_email': 'owner@example.com' } },
  { id: 'openalex', card: 'OpenAlex', expect: 'fetch', fields: { 'source-scope-scholarly_query': 'inflation' } },
  { id: 'arxiv', card: 'arXiv', expect: 'fetch', fields: { 'source-scope-arxiv_category': 'cs.AI' } },
  { id: 'huggingface', card: 'Hugging Face models', expect: 'fetch', fields: { 'source-scope-huggingface_author': 'google' } },
  { id: 'github_releases', card: 'GitHub Releases', expect: 'fetch', fields: { 'source-scope-github_owner': 'vercel', 'source-scope-github_repository': 'next.js' } },
  { id: 'youtube', card: 'YouTube', expect: 'fetch', fields: { 'source-scope-youtube_channel_id': 'UC_x5XG1OV2P6uZZ5FSM9Ttw' } },
  { id: 'open_meteo', card: 'Open-Meteo forecast', expect: 'fetch', fields: { 'source-scope-weather_latitude': '21.03', 'source-scope-weather_longitude': '105.85', 'source-scope-weather_timezone': 'Asia/Ho_Chi_Minh', 'source-scope-weather_metrics': 'temperature_2m' } },
  { id: 'coingecko', card: 'CoinGecko', expect: 'fetch', key: 'dummy-e2e-key' }, // Demo tier answers the keyless simple/price call even with an unrecognised key,
  { id: 'finnhub_news', card: 'Finnhub market news', expect: 'rejects', key: 'dummy-e2e-key' },
  { id: 'newsapi', card: 'NewsAPI', expect: 'rejects', key: 'dummy-e2e-key' },
  { id: 'fred', card: 'FRED series', expect: 'rejects', key: 'dummy-e2e-key', fields: { 'source-scope-fred_series_id': 'DGS10' } },
  { id: 'openweathermap', card: 'OpenWeather', expect: 'rejects', key: 'dummy-e2e-key', fields: { 'source-scope-owm_latitude': '21.03', 'source-scope-owm_longitude': '105.85' } },
  { id: 'openaq', card: 'OpenAQ', expect: 'rejects', key: 'dummy-e2e-key', fields: { 'source-scope-openaq_location_id': '8118' } },
  { id: 'nasa_firms', card: 'NASA FIRMS', expect: 'rejects', key: 'dummy-e2e-key', fields: { 'source-scope-firms_country': 'VNM' } },
  { id: 'alpha_vantage', card: 'Alpha Vantage', expect: 'fetch', // IBM daily data is served for any key string (verified by curl)
     key: 'dummy-e2e-key', fields: { 'source-scope-market_symbols': 'IBM', 'source-scope-market_currency': 'USD', 'source-scope-market_exchange_timezone': 'America/New_York' } },
];


/** Escapes a label for use inside a RegExp. */
export const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Keyless fixed-scope built-ins `[id, label]`; mirrors `BUILTIN_PROVIDERS` in modules/connectors/provider_specs.py. */
export const BUILTINS: [id: string, label: string][] = [
  ['bbc_world', 'BBC World News'],
  ['vnexpress_business', 'VnExpress Business'],
  ['tuoitre_news', 'Tuổi Trẻ News'],
  ['thanhnien_news', 'Báo Thanh Niên'],
  ['dantri_news', 'Dân trí News'],
  ['hn_top', 'Hacker News top stories'],
  ['gdelt_economy', 'GDELT economy articles'],
  ['world_bank', 'World Bank annual GDP (Vietnam)'],
  ['frankfurter', 'Frankfurter USD/VND'],
  ['ecb', 'ECB euro reference rates'],
  ['binance', 'Binance BTCUSDT price'],
  ['alternative_me', 'Alternative.me Fear and Greed'],
  ['usgs', 'USGS M4.5+ earthquakes (day)'],
  ['nasa_eonet', 'NASA EONET open natural events'],
  ['cisa_kev', 'CISA Known Exploited Vulnerabilities'],
  ['coinpaprika', 'CoinPaprika BTC ticker'],
  ['aljazeera_world', 'Al Jazeera World News'],
  ['npr_news', 'NPR News'],
  ['fed_press', 'Federal Reserve press releases'],
  ['gdacs_rss', 'GDACS disaster alerts (RSS)'],
  ['lobsters', 'Lobsters'],
  ['us_travel_advisories', 'U.S. travel advisories'],
  ['noaa_tsunami', 'NOAA tsunami bulletins'],
  ['jma_eqvol', 'JMA earthquake and volcano bulletins'],
  ['gdacs_events', 'GDACS Orange/Red disaster events'],
  ['nws_alerts', 'NWS severe alerts (US)'],
  ['emsc_quakes', 'EMSC earthquakes (M4+)'],
  ['launch_library', 'Launch Library upcoming launches'],
  ['wikipedia_mostread', 'Wikipedia most-read articles'],
  ['wikipedia_top_views', 'Wikipedia top page views'],
  ['bmkg_quakes', 'BMKG recent earthquakes (Indonesia)'],
  ['satnogs_observations', 'SatNOGS recent observations'],
  ['noaa_kp', 'NOAA planetary Kp index'],
  ['open_er_api', 'Open ER API daily USD rates'],
  ['oecd_cli', 'OECD composite leading indicators'],
  ['iss_position', 'ISS position'],
  ['disease_sh', 'disease.sh global COVID-19 totals'],
  ['ioda', 'IODA internet outage events'],
  ['nvd', 'NVD recent CVEs'],
];

/** Opens the "Connect a source" wizard dialog from the sources settings page. */
export async function openWizard(page: Page) {
  await page.goto('/settings/sources');
  await page.getByRole('button', { name: 'Connect a source' }).click();
  return page.getByRole('dialog');
}

/**
 * Walks the wizard for one case up to and including "Save and test fetch" and returns the visible fetch outcome.
 * The source is created under `name`; the caller owns its cleanup (`deleteSources`).
 */
export async function connectWizardSource(page: Page, c: WizardCase, name: string, timeout = 120_000): Promise<Outcome> {
  const dialog = await openWizard(page);
  await dialog.getByRole('button', { name: new RegExp(`^${escapeRe(c.card)}`) }).click();
  await dialog.getByLabel('Source name').fill(name);
  const next = dialog.getByRole('button', { name: 'Next', exact: true });
  await next.click(); // provider -> connect (creates the draft source)
  if (c.key) await page.locator('#source-provider-key').fill(c.key);
  await expect(next).toBeEnabled();
  await next.click(); // connect -> choose data
  for (const [id, value] of Object.entries(c.fields ?? {})) await page.locator(`#${id}`).fill(value);
  await acknowledgeTerms(dialog);
  const validated = page.waitForResponse((r) => r.url().includes('/validate-draft') && r.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Validate draft' }).click();
  expect((await validated).status(), 'validate-draft response').toBe(200);
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  await next.click(); // choose -> schedule
  await next.click(); // schedule -> save and test
  await dialog.getByRole('button', { name: 'Save and test fetch' }).click();
  return waitForFetchOutcome(dialog, timeout);
}

/**
 * Enables one built-in through "Add built-in" (pick, terms, enable, test fetch) and returns the fetch outcome with the
 * created source id (null when the server did not create one). The caller owns cleanup.
 */
export async function enableBuiltinViaSettings(page: Page, id: string, label: string, timeout = 120_000): Promise<Outcome & { sourceId: string | null }> {
  await page.goto('/settings/sources');
  await page.getByRole('button', { name: 'Add built-in' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: new RegExp(`^${escapeRe(label)}`) }).click();
  const next = dialog.getByRole('button', { name: 'Next', exact: true });
  await next.click(); // pick -> terms (personal use is the default declaration)
  await next.click(); // terms -> enable + test fetch
  // fed_press-style keyless feeds can answer one transient provider_rejected; a second one still fails as before.
  const outcome = await waitForFetchOutcome(dialog, timeout, 1, ['provider_rejected']);
  const builtins = await (await page.request.get('/api/v1/connectors/builtins')).json() as { id: string; source_id: string | null }[];
  return { ...outcome, sourceId: builtins.find((x) => x.id === id)?.source_id ?? null };
}
