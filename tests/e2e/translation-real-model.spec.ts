import { existsSync } from "node:fs";
import path from "node:path";
import type { Locator, Page } from "@playwright/test";
import { test, expect } from "./fixtures";
import { escapeRe } from "./sources-helpers";
import { uiText } from "./ui-text";
import {
  itemButton,
  openDashboard,
  setTranslation,
  setupBbcDashboard,
  watchBatches,
} from "./translation-helpers";

// Opt-in on-demand translation e2e against a REAL OpenAI-compatible model (role "background"). Configure
// `.env.e2e-real`; the whole file is skipped while the key, base URL or model is empty.
// SECURITY: the key goes only to the disposable stack's settings API. Traces/screenshots/video are off, assertion
// messages go through mask(), no settings page is captured, and the key is checked absent from the DOM after each test.
const ENV_FILE = path.resolve(__dirname, "../../.env.e2e-real");
if (existsSync(ENV_FILE)) process.loadEnvFile(ENV_FILE);

const KEY = process.env.E2E_REAL_API_KEY ?? "";
const BASE_URL = (process.env.E2E_REAL_BASE_URL ?? "").replace(/\/+$/, "");
const MODEL = process.env.E2E_REAL_MODEL ?? "";
const REAL_TIMEOUT = 120_000;
const VI = /[ăâđêôơưàáạảãèéẹẻẽìíịỉĩòóọỏõùúụủũỳýỵỷỹ]/i;

test.use({ trace: "off", screenshot: "off", video: "off" });

/** Redacts the configured key from any text that may reach an assertion message or report. */
const mask = (text: string) => (KEY ? text.split(KEY).join("***") : text);

/** Owner write headers: Playwright's request context sends no Origin, which owner writes require. */
async function writeHeaders(page: Page) {
  const session = await (await page.request.get("/api/v1/auth/session")).json();
  return {
    "X-CSRF-Token": session.csrfToken as string,
    Origin: new URL(
      page.url() === "about:blank" ? "http://localhost:3000" : page.url(),
    ).origin,
  };
}

/** Fails if the key appears anywhere in the rendered page (run after every test). */
async function expectNoKeyLeak(page: Page) {
  expect(
    (await page.content()).includes(KEY),
    "API key must never be rendered",
  ).toBe(false);
}

/** Configures the real provider for chat, brief and background, grants consent and probes the background role. */
async function configureRealModel(page: Page) {
  await page.goto("/dashboard");
  const headers = await writeHeaders(page);
  const current = await (await page.request.get("/api/v1/settings/ai")).json();
  const mapping = { model: MODEL, destination: "remote", provider_id: "real" };
  const common = {
    defaults: { chat: mapping, brief: mapping, background: mapping },
    request_timeout_seconds: 120,
  };
  const created = await page.request.put("/api/v1/settings/ai", {
    headers,
    data: {
      ...common,
      providers: [
        {
          id: "real",
          kind: "custom",
          label: "real",
          base_url: BASE_URL,
          credential_action: "replaced",
          api_key: KEY,
          models: [{ id: MODEL, type: "llm" }],
        },
      ],
      expected_revision: current.configuration_revision,
    },
  });
  expect(
    created.ok(),
    mask(`create provider: ${created.status()} ${await created.text()}`),
  ).toBeTruthy();
  const body = await created.json();
  const destination = body.providers.find(
    (p: { id: string }) => p.id === "real",
  ).destination_id;
  const saved = await page.request.put("/api/v1/settings/ai", {
    headers,
    data: {
      ...common,
      privacy: {
        allow_remote_reasoning: true,
        reasoning_destinations: [destination],
        allow_remote_embeddings: false,
        embedding_destinations: [],
      },
      expected_revision: body.configuration_revision,
    },
  });
  expect(
    saved.ok(),
    mask(`grant consent: ${saved.status()} ${await saved.text()}`),
  ).toBeTruthy();
  for (const capability of ["chat", "structured"]) {
    const probe = await page.request.post(
      "/api/v1/settings/models/background/test",
      {
        headers,
        data: { capability },
        timeout: REAL_TIMEOUT,
      },
    );
    expect(
      probe.ok(),
      mask(`probe ${capability}: ${probe.status()} ${await probe.text()}`),
    ).toBeTruthy();
    expect((await probe.json()).result, `probe ${capability}`).toBe(
      "supported",
    );
  }
}

/** Removes the provider: system.spec.ts asserts a fresh stack has no configured model gateway. */
async function removeProvider(page: Page) {
  await page.goto("/dashboard");
  const headers = await writeHeaders(page);
  const current = await (await page.request.get("/api/v1/settings/ai")).json();
  const res = await page.request.put("/api/v1/settings/ai", {
    headers,
    data: {
      providers: [],
      defaults: {},
      privacy: {
        allow_remote_reasoning: false,
        reasoning_destinations: [],
        allow_remote_embeddings: false,
        embedding_destinations: [],
      },
      request_timeout_seconds: 30,
      expected_revision: current.configuration_revision,
    },
  });
  expect(res.ok(), mask(`remove provider: ${res.status()}`)).toBeTruthy();
}

/** Original/Translation flip button inside `scope`. */
const flipNamed = (scope: Locator, key: "originalShort" | "translationShort") =>
  scope.getByRole("button", { name: uiText("translations", key), exact: true });

const translateName = (title: string) =>
  `${uiText("translations", "translate")} “${title}”`;

/** First Translate button for `title`: several gadgets on the dashboard may render the same story. */
const storyButton = (page: Page, title: string) =>
  page.getByRole("button", { name: translateName(title), exact: true }).first();

let dashboardUrl = "";
// Story translated by test 2 and reused by the later tests (same row after reload, matched by title).
let storyTitle = "";
let translatedTitle = "";

/** Title text of a row: its selection button's first line. */
async function rowTitle(row: Locator) {
  return ((await row.getByRole("button").first().innerText()) ?? "")
    .split("\n")[0]
    .trim();
}

/** Expands "Show N more" until the live feed shows `title`'s Translate button (feed order drifts between loads). */
async function revealStory(page: Page, title: string) {
  const btn = storyButton(page, title);
  const more = page.getByRole("button", {
    name: new RegExp(
      `^${escapeRe(uiText("dashboard", "showMore", { count: "__N__" })).replace("__N__", "\\d+")}$`,
    ),
  });
  for (let i = 0; i < 6 && !(await btn.count()) && (await more.count()); i += 1)
    await more.first().click();
  await expect(btn).toBeAttached({ timeout: 60_000 });
}

/**
 * Marks the list row that owns the Translate button named for `title` and returns a locator for it. The Translate
 * button disappears once translated, so a locator derived from it would stop resolving; the marker survives.
 */
async function markRow(page: Page, title: string) {
  await storyButton(page, title).evaluate((el) =>
    el.closest("li")?.setAttribute("data-e2e-row", "1"),
  );
  return page.locator('li[data-e2e-row="1"]');
}

test.describe.configure({ mode: "serial", timeout: 600_000 });

test.describe("translation (real model)", () => {
  test.skip(
    !KEY || !BASE_URL || !MODEL,
    "set E2E_REAL_API_KEY / E2E_REAL_BASE_URL / E2E_REAL_MODEL in .env.e2e-real",
  );

  test.afterEach(async ({ page }) => {
    await expectNoKeyLeak(page);
  });
  test.afterAll(async ({ browser }, testInfo) => {
    const context = await browser.newContext({
      storageState: testInfo.project.use.storageState as string,
      baseURL: testInfo.project.use.baseURL,
    });
    await removeProvider(await context.newPage());
    await context.close();
  });

  test("1: setup real background provider, BBC feed, translation enabled (vi)", async ({
    page,
  }) => {
    await configureRealModel(page);
    dashboardUrl = await setupBbcDashboard(page, "e2e-translation-real home");
    await setTranslation(page, true, "vi");
  });

  test("2+3: Translate one story to Vietnamese, flip works, detail dialog reuses it without a new POST", async ({
    page,
  }) => {
    const { posts } = watchBatches(page);
    await openDashboard(page, dashboardUrl);
    // A story inside a list row (the featured hero has no row to scope flip assertions to).
    const btn = itemButton(page.locator("li")).first();
    await expect(btn).toBeAttached({ timeout: 60_000 });
    const label = (await btn.getAttribute("aria-label")) ?? "";
    storyTitle = /“(.*)”/.exec(label)?.[1] ?? "";
    expect(
      storyTitle,
      "per-item Translate button names its story",
    ).toBeTruthy();
    const row = await markRow(page, storyTitle);
    await btn.scrollIntoViewIfNeeded();
    const started = Date.now();
    await btn.click();
    await expect(
      flipNamed(row, "originalShort"),
      "row never reached translated state",
    ).toBeVisible({ timeout: REAL_TIMEOUT });
    console.log(`case2 translate latency ms=${Date.now() - started}`);
    expect(posts, "one POST with one item").toEqual([1]);
    translatedTitle = await rowTitle(row);
    console.log(
      `case2 original="${storyTitle}" translated="${translatedTitle}"`,
    );
    expect(translatedTitle).not.toBe(storyTitle);
    expect(
      translatedTitle,
      "Vietnamese diacritics in translated title",
    ).toMatch(VI);
    for (const num of storyTitle.match(/\d[\d.,]*/g) ?? [])
      expect(translatedTitle, `number ${num} preserved`).toContain(num);

    // Flip to the original, then back.
    await flipNamed(row, "originalShort").click();
    await expect(row.getByRole("button").first()).toContainText(storyTitle);
    await flipNamed(row, "translationShort").click();
    await expect(row.getByRole("button").first()).toContainText(
      translatedTitle,
    );

    // Same session: the story dialog shows the translation immediately and sends nothing.
    await row.getByRole("button").first().click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible({ timeout: 15_000 });
    await expect(dialog).toContainText(translatedTitle, { timeout: 5_000 });
    await page.waitForTimeout(3_000);
    expect(posts, "no new POST when opening detail").toEqual([1]);
  });

  test("4: reload translates nothing; repeat click resolves from the server cache quickly", async ({
    page,
  }) => {
    const { posts, gets } = watchBatches(page);
    await openDashboard(page, dashboardUrl);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(3_000);
    expect(posts, "POST on load").toEqual([]);
    await revealStory(page, storyTitle);
    const btn = storyButton(page, storyTitle);
    const row = await markRow(page, storyTitle);
    const started = Date.now();
    const response = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === "/api/v1/translations/batches" &&
        r.request().method() === "POST",
    );
    await btn.click();
    const accepted = (await (await response).json()) as {
      items: Array<{ status: string }>;
    };
    await expect(flipNamed(row, "originalShort")).toBeVisible({
      timeout: 15_000,
    });
    const elapsed = Date.now() - started;
    console.log(
      `case4 cached latency ms=${elapsed} postItemStatus=${accepted.items[0]?.status} gets=${gets.length}`,
    );
    expect(elapsed, "cache hit under 15 s").toBeLessThan(15_000);
    expect(posts).toEqual([1]);
    expect(await rowTitle(row)).toMatch(VI);
  });

  test("5: Translate all sends batches of at most 25 and nothing stays Translating", async ({
    page,
  }) => {
    const { posts } = watchBatches(page);
    await openDashboard(page, dashboardUrl);
    const all = page
      .getByRole("button", {
        name: new RegExp(
          `^${escapeRe(uiText("translations", "translateAll"))}$`,
        ),
      })
      .first();
    await expect(all).toBeVisible({ timeout: 60_000 });
    await all.click();
    await expect
      .poll(() => posts.length, { timeout: 30_000 })
      .toBeGreaterThan(0);
    await expect(
      page.getByRole("button", {
        name: new RegExp(escapeRe(uiText("translations", "translating"))),
      }),
    ).toHaveCount(0, { timeout: 300_000 });
    for (const size of posts) expect(size).toBeLessThanOrEqual(25);
    const flips = flipNamed(page.locator("body"), "originalShort");
    const translated = await flips.count();
    console.log(
      `case5 batches=${JSON.stringify(posts)} translatedRows=${translated} remainingTranslateButtons=${await itemButton(page).count()}`,
    );
    expect(translated, "at least some rows translated").toBeGreaterThan(0);
    const samples: string[] = [];
    for (const b of (await flips.all()).slice(0, 5))
      samples.push(
        await b.evaluate((el) =>
          (el.closest("li")?.querySelector("button")?.textContent ?? "").slice(
            0,
            160,
          ),
        ),
      );
    console.log(`case5 samples=${JSON.stringify(samples)}`);
  });

  test("6: switching the target to English re-keys results; translating again yields English", async ({
    page,
  }) => {
    await openDashboard(page, dashboardUrl);
    await revealStory(page, storyTitle);
    const row = await markRow(page, storyTitle);
    await storyButton(page, storyTitle).click();
    await expect(flipNamed(row, "originalShort")).toBeVisible({
      timeout: REAL_TIMEOUT,
    });
    await expect(row.getByRole("button").first()).toContainText(
      translatedTitle,
    );

    const control = (lang: string) =>
      page.getByRole("button", {
        name: new RegExp(
          `^${escapeRe(uiText("translations", "control.label"))}: ${lang}`,
        ),
      });
    await control("VI").click();
    await page
      .getByRole("combobox", { name: uiText("translations", "targetLanguage") })
      .click();
    await page
      .getByRole("option", { name: uiText("translations", "languageEn") })
      .click();
    await expect(control("EN")).toBeVisible({ timeout: 15_000 });
    await page.keyboard.press("Escape");
    // The Vietnamese result must not be current for the English target.
    await expect(flipNamed(row, "originalShort")).toHaveCount(0, {
      timeout: 15_000,
    });
    await expect(row.getByRole("button").first()).not.toContainText(
      translatedTitle,
    );
    const started = Date.now();
    await storyButton(page, storyTitle).click();
    // English source -> English target is either "unchanged" (note) or a translated row; never Vietnamese.
    await expect(
      flipNamed(row, "originalShort").or(
        row.getByText(uiText("translations", "note.unchanged")),
      ),
    ).toBeVisible({ timeout: REAL_TIMEOUT });
    const title = await rowTitle(row);
    console.log(`case6 en latency ms=${Date.now() - started} title="${title}"`);
    expect(title).not.toMatch(VI);
    await setTranslation(page, true, "vi");
  });
});
