import { test, expect } from './fixtures';
import { collectedRecordCount, connectWizardSource, deleteSources, findSources, hasLiveKeys, openWizard, WIZARD_CASES } from './sources-helpers';

/**
 * Walks the "Connect a source" wizard to its last step ("Save and test fetch") for every provider card it offers as
 * Available, then asserts the real outcome. Keyless providers must fetch records from the live public endpoint.
 * Keyed providers use a dummy key (no real keys exist in the disposable stack) and must fail with a readable
 * `Test fetch failed: <code>` message instead of crashing. Public network access is required for the `fetch` cases.
 */
// Live keyed providers (`E2E_KEY_*`) are typed into the wizard: keep traces/screenshots/videos off so the key is never recorded.
if (hasLiveKeys()) test.use({ trace: 'off', screenshot: 'off', video: 'off' });

/** Optional `E2E_ONLY=id1,id2` narrows the table while debugging a single provider. */
const only = process.env.E2E_ONLY?.split(",");

for (const c of WIZARD_CASES.filter((x) => !only || only.includes(x.id))) {
  test(`wizard ${c.id}: connect, activate and ${c.expect === 'fetch' ? 'fetch records' : 'fail cleanly without a real key'}`, async ({ page }) => {
    test.setTimeout(240_000);
    const name = `e2e ${c.id} ${Date.now()}`;
    // Surface the server's reason for any rejected connector call (the wizard shows only a generic code).
    page.on('response', async (r) => {
      if (r.status() >= 400 && r.url().includes('/api/v1/connectors/') && r.status() !== 401) {
        console.log(`HTTP ${r.status()} ${r.request().method()} ${new URL(r.url()).pathname} :: ${(await r.text().catch(() => '')).slice(0, 300)}`);
      }
    });
    try {
      const outcome = await connectWizardSource(page, c, name);
      test.info().annotations.push({ type: 'outcome', description: outcome.text });
      console.log(`RESULT ${c.id} :: ${outcome.text}`);

      const [source] = await findSources(page, name);
      if (c.expect === 'fetch') {
        expect(outcome.ok, outcome.text).toBe(true);
        const count = await collectedRecordCount(page, source.id);
        test.info().annotations.push({ type: 'records', description: String(count) });
        console.log(`RECORDS ${c.id} :: ${count}`);
        // arXiv's daily RSS is legitimately empty on weekends: only tolerate zero records when upstream itself has no entries.
        const upstreamEmpty = c.id === 'arxiv' && count === 0
          && !/<entry[ >]/.test(await (await page.request.get('https://rss.arxiv.org/atom/cs.AI')).text());
        if (!upstreamEmpty) expect(count, 'records collected by the test fetch').toBeGreaterThan(0);
      } else {
        expect(outcome.ok, 'dummy credential must not succeed').toBe(false);
        // The failure must come from the provider call itself, not from saving, encrypting or activating the credential.
        expect(outcome.text).toMatch(/Test fetch failed: (invalid_credential|provider_rejected|provider_response_invalid|provider_unavailable)\./);
      }
    } finally {
      await deleteSources(page, name);
    }
  });
}

/**
 * Providers that cannot reach a real fetch in a disposable stack without an owner account: Telegram needs a live bot
 * token and GitHub needs an interactive OAuth grant. The wizard must still accept the first steps and reject the dummy
 * Telegram token with a readable validation error rather than a server error.
 */
test('wizard telegram: dummy bot token is rejected cleanly at validation', async ({ page }) => {
  const name = `e2e telegram ${Date.now()}`;
  const statuses: number[] = [];
  page.on('response', (r) => { if (r.url().includes('/validate-draft')) statuses.push(r.status()); });
  try {
    const dialog = await openWizard(page);
    await dialog.getByRole('button', { name: /^Telegram channels/ }).click();
    await dialog.getByLabel('Source name').fill(name);
    const next = dialog.getByRole('button', { name: 'Next', exact: true });
    await next.click();
    await page.locator('#source-telegram-token').fill('123456:dummy-e2e-token');
    await expect(next).toBeEnabled();
    await next.click();
    await page.locator('#source-scope-telegram_chat_ids').fill('-1001234567890');
    await dialog.getByRole('button', { name: 'Validate draft' }).click();
    await expect(dialog.getByRole('alert').first()).toBeVisible({ timeout: 30_000 });
    expect(statuses.length, 'validate-draft was called').toBeGreaterThan(0);
    expect(statuses.every((s) => s < 500), `validate-draft statuses ${statuses}`).toBe(true);
  } finally {
    await deleteSources(page, name);
  }
});

test('wizard github: first steps accept a repository scope', async ({ page }) => {
  const name = `e2e github ${Date.now()}`;
  try {
    const dialog = await openWizard(page);
    await dialog.getByRole('button', { name: /^GitHub Credentials required/ }).click();
    await dialog.getByLabel('Source name').fill(name);
    const next = dialog.getByRole('button', { name: 'Next', exact: true });
    await next.click();
    await expect(dialog.getByRole('button', { name: /Connect GitHub/i })).toBeVisible();
  } finally {
    await deleteSources(page, name);
  }
});
