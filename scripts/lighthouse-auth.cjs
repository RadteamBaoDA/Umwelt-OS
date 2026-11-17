/**
 * LHCI `collect.puppeteerScript`: signs the disposable-stack owner in before an authenticated route is audited.
 *
 * LHCI calls this once per URL, in the same Chrome profile Lighthouse then audits (lighthouserc.json sets
 * `disableStorageReset`, otherwise Lighthouse would clear the session cookie). Public routes are skipped. If the stack
 * has no owner yet, the first-run setup form is submitted with LH_SETUP_TOKEN first (same flow as tests/e2e/auth.spec.ts).
 *
 * Env: LH_BASE_URL (default http://localhost:3300), LH_SETUP_TOKEN, LH_OWNER_PASSWORD. Throws on any failed step so the
 * LHCI job fails instead of auditing the login redirect.
 *
 * @param {import('puppeteer-core').Browser} browser Browser LHCI launched for this run.
 * @param {{url: string}} context URL about to be audited.
 * @returns {Promise<void>}
 */
const PUBLIC_PATHS = new Set(['/', '/login', '/terms', '/privacy']);

module.exports = async (browser, context) => {
  const target = new URL(context.url);
  if (PUBLIC_PATHS.has(target.pathname)) return;
  const base = process.env.LH_BASE_URL || target.origin;
  const token = process.env.LH_SETUP_TOKEN;
  const password = process.env.LH_OWNER_PASSWORD;
  if (!token || !password) throw new Error('LH_SETUP_TOKEN and LH_OWNER_PASSWORD must be set');

  const page = await browser.newPage();
  try {
    await page.goto(`${base}/login`, { waitUntil: 'networkidle0' });
    await page.waitForFunction(() => document.querySelector('#setupToken') || document.querySelector('#password'));
    // First-run setup now lives at /setup; login redirects here after reading setup status.
    if (await page.$('#setupToken')) {
      await page.type('#setupToken', token);
      await page.type('#password', password);
      await page.type('#confirmPassword', password);
      await page.click('button[type=submit]');
      await page.waitForFunction(() => location.pathname.startsWith('/login'));
    }
    await page.waitForSelector('#password');
    await page.type('#password', password);
    await page.click('button[type=submit]');
    // Client-side router push, so there is no document navigation to wait for.
    await page.waitForFunction(() => location.pathname === '/dashboard' || location.pathname.startsWith('/dashboard/'));
  } finally {
    await page.close();
  }
};
