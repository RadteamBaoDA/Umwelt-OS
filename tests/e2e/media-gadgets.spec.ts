import { spawnSync } from "node:child_process";
import { join } from "node:path";
import type { Locator, Page } from "@playwright/test";
import { test, expect, E2E_LOCALE } from "./fixtures";
import {
  collectedRecordCount,
  deleteSources,
  enableBuiltinViaSettings,
  escapeRe,
  findSources,
  openWizard,
  removeBuiltinSource,
  writeHeaders,
} from "./sources-helpers";
import { uiRe, uiText } from "./ui-text";

/**
 * Media datasource -> gadget end-to-end (spec new-datasource-v2 section 3.3 / 3.5 item 3): the keyless LiveChart built-in,
 * a scoped TVMaze source created through the Sources UI, the keyless-vs-key guidance and attribution badge, the YouTube
 * Data API scope panel, and `media_feed` / `media_calendar` gadgets bound to those sources. Live providers: a remote
 * outage (5xx, timeout, throttle) skips the dependent step with an explicit reason, it never passes silently. The gadget
 * tests do NOT depend on live data: they bind a manual source seeded by `seed_media_fixture.py` (contract-valid media
 * records, more than one projection page) so rendering, filters, chips, Load more and calendar buckets always run.
 *
 * UI labels come from the owner-locale catalogs (`uiText`), so the UI-only tests run under en-us and vi-vi. The built-in
 * enable helper is English-only, so that one test is skipped in other locales. Needs outbound public network access;
 * use the disposable stack only. Everything created is named with PREFIX; cleanup never touches other data.
 */
const PREFIX = "e2e-media";
const TVMAZE = `${PREFIX} tvmaze`;
const FIXTURE = `${PREFIX} fixture`;
const FIXTURE_ROWS = 130;
const DASHBOARD = `${PREFIX} dashboard`;
const SHELF = `${PREFIX} shelf`;
const CALENDAR = `${PREFIX} calendar`;
const LIVECHART = {
  id: "livechart_episodes",
  label: "LiveChart anime episodes",
};
/** Fetch failure codes that mean the remote provider (or the runner's network) is down, not that the app is broken. */
const OUTAGE = /provider_(unavailable|timeout|rate_limited|rejected)|network/;

test.describe.configure({ mode: "serial", timeout: 240_000 });

/** Clicks a Radix select trigger found by its (exact) label inside `scope`, then the option with the given name. */
async function pickOption(
  page: Page,
  scope: Locator,
  label: string,
  option: string | RegExp,
) {
  await scope.getByRole("combobox", { name: label, exact: true }).click();
  await page
    .getByRole("option", { name: option, exact: typeof option === "string" })
    .click();
}

/** Declares personal use and acknowledges the provider terms panel when the open wizard shows it (locale-aware). */
async function acknowledge(page: Page, dialog: Locator) {
  const panel = dialog.getByRole("region", {
    name: uiText("sources", "terms.title"),
  });
  if (!(await panel.count())) return;
  await pickOption(
    page,
    panel,
    uiText("sources", "terms.declaredUse"),
    uiText("sources", "terms.usePersonal"),
  );
  await panel.getByRole("checkbox").click();
  await panel
    .getByRole("button", { name: uiText("sources", "terms.save") })
    .click();
  await expect(panel.getByText(uiText("sources", "terms.saved"))).toBeVisible();
}

/** Waits for the wizard's test-fetch result; retries one transient provider failure. Returns the outcome text and, on failure, its code. */
async function fetchOutcome(
  dialog: Locator,
  retries = 1,
): Promise<{ ok: boolean; text: string; code: string }> {
  const prefix = uiText("sources", "verify.failed", { code: "@@" }).split(
    "@@",
  )[0]!;
  const success = dialog
    .getByRole("status")
    .filter({ hasText: uiText("sources", "verify.succeeded").split(".")[0]! });
  const failure = dialog.getByRole("alert").filter({ hasText: prefix.trim() });
  await expect(success.or(failure).first()).toBeVisible({ timeout: 120_000 });
  if (await success.count())
    return {
      ok: true,
      text: (await success.first().innerText()).trim(),
      code: "",
    };
  const text = (await failure.first().innerText()).trim();
  const code =
    text
      .slice(text.indexOf(prefix.trim()) + prefix.trim().length)
      .trim()
      .split(/[.\s]/)[0] ?? "";
  if (retries > 0 && OUTAGE.test(code)) {
    await dialog
      .getByRole("button", { name: uiText("sources", "verify.retry") })
      .click();
    await expect(failure).toHaveCount(0);
    return fetchOutcome(dialog, retries - 1);
  }
  return { ok: false, text, code };
}

/** Opens the "Connect an API" wizard on a provider card (a `provider.*` catalog key, or the server catalog's literal label, which keyed providers keep in every locale), names the source and advances to the Choose-data step (keyless). */
async function openChooseStep(
  page: Page,
  providerKey: string,
  name: string,
  apiKey?: string,
) {
  const dialog = await openWizard(page);
  await dialog
    .getByRole("button", {
      name: new RegExp(
        `^${escapeRe(providerKey.startsWith("provider.") ? uiText("sources", providerKey) : providerKey)}`,
      ),
    })
    .click();
  await dialog.getByLabel(uiText("sources", "sourceName")).fill(name);
  const next = dialog.getByRole("button", {
    name: uiText("sources", "next"),
    exact: true,
  });
  await next.click(); // provider -> connect (creates the draft source)
  if (apiKey) await page.locator("#source-provider-key").fill(apiKey); // dummy test value: the draft is never fetched or kept
  await expect(next).toBeEnabled();
  await next.click(); // connect -> choose data
  return { dialog, next };
}

/** Projection items (with typed media metadata) the bound sources currently expose to dashboards. */
async function projectionItems(page: Page, sourceIds: string[]) {
  const query = sourceIds.map((id) => `source_ids=${id}`).join("&");
  const res = await page.request.get(
    `/api/v1/documents/dashboard-projections?limit=100&${query}`,
  );
  return (
    (await res.json()) as { items: { media?: { genres: string[] } | null }[] }
  ).items;
}

/** Creates the manual fixture source and seeds its media documents (disposable stack DB via TEST_DATABASE_URL). */
async function seedFixtureSource(page: Page): Promise<string> {
  await page.goto("/dashboard"); // writeHeaders derives Origin from the current page URL
  const created = await page.request.post("/api/v1/sources", {
    data: { type: "manual", name: FIXTURE },
    headers: await writeHeaders(page),
  });
  expect(
    created.ok(),
    `create fixture source: ${created.status()} ${await created.text()}`,
  ).toBe(true);
  const id = ((await created.json()) as { id: string }).id;
  const run = spawnSync(
    "uv",
    [
      "run",
      "python",
      "tests/e2e/seed_media_fixture.py",
      id,
      String(FIXTURE_ROWS),
    ],
    { cwd: join(__dirname, "..", ".."), encoding: "utf8", env: process.env },
  );
  expect(run.status, `seed fixture: ${run.stdout}${run.stderr}`).toBe(0);
  await expect
    .poll(
      async () =>
        (await projectionItems(page, [id])).filter((i) => i.media).length,
      { timeout: 30_000 },
    )
    .toBeGreaterThan(0);
  return id;
}

/** Opens the e2e dashboard and waits for the edit toolbar. */
async function openDashboard(page: Page) {
  const list = (await (
    await page.request.get("/api/v1/dashboards")
  ).json()) as { id: string; name: string }[];
  const dash = list.find((d) => d.name === DASHBOARD);
  expect(
    dash,
    "e2e dashboard exists (run the dashboard creation test first)",
  ).toBeTruthy();
  await page.goto(`/dashboard/${dash!.id}`);
  await expect(
    page.getByRole("button", { name: uiText("dashboard", "toggleEdit") }),
  ).toBeEnabled({ timeout: 30_000 });
  await expect(page.locator("[data-route-loader]")).toBeHidden({
    timeout: 30_000,
  });
}

/** Adds one media gadget (optionally with Items shown / Exclude keywords, bound to the seeded fixture source) through Edit layout > Add gadget > template tile > bound sources > Name, then saves the layout. */
async function addMediaGadget(
  page: Page,
  tileKey: string,
  name: string,
  scope: { limit?: string; exclude?: string } = {},
) {
  await openDashboard(page);
  await page
    .getByRole("button", { name: uiText("dashboard", "toggleEdit") })
    .click();
  await page
    .getByRole("button", {
      name: uiText("dashboard", "addGadget.label"),
      exact: true,
    })
    .click();
  const dialog = page.getByRole("dialog", {
    name: uiText("dashboard", "addGadget.title"),
  });
  await dialog
    .getByRole("button", {
      name: new RegExp(`^${escapeRe(uiText("gadgetSettings", tileKey))}\\b`),
    })
    .click();
  await dialog
    .getByRole("checkbox", { name: new RegExp(`^${escapeRe(FIXTURE)}`) })
    .click();
  await dialog
    .getByLabel(uiText("gadgetSettings", "name"), { exact: true })
    .fill(name);
  if (scope.limit || scope.exclude)
    await dialog
      .getByText(uiText("gadgetSettings", "advanced"), { exact: true })
      .click(); // collapsed <details>
  if (scope.limit)
    await dialog
      .getByLabel(uiText("gadgetSettings", "limit"), { exact: true })
      .fill(scope.limit);
  if (scope.exclude)
    await dialog
      .getByLabel(uiText("gadgetSettings", "excludeKeywords"), { exact: true })
      .fill(scope.exclude);
  const add = dialog.getByRole("button", {
    name: uiText("dashboard", "addGadget.label"),
    exact: true,
  });
  await expect(add).toBeEnabled({ timeout: 60_000 });
  await add.click();
  await expect(dialog).toBeHidden({ timeout: 60_000 });
  const save = page.getByRole("button", {
    name: uiText("dashboard", "saveLayout"),
  });
  if (await save.isEnabled()) await save.click();
  await page
    .getByRole("button", { name: uiText("dashboard", "finishEditing") })
    .click();
}

test.describe("media datasources and gadgets", () => {
  test("clean slate", async ({ page }) => {
    await deleteSources(page, PREFIX);
    await removeBuiltinSource(page, LIVECHART.id);
  });

  test("livechart built-in: enable and fetch episodes", async ({ page }) => {
    test.skip(
      E2E_LOCALE !== "en-us",
      "the built-in enable helper drives English copy",
    );
    const out = await enableBuiltinViaSettings(
      page,
      LIVECHART.id,
      LIVECHART.label,
    );
    console.log(`RESULT livechart_episodes :: ${out.text}`);
    expect(out.sourceId, "built-in source created").not.toBeNull();
    if (!out.ok && OUTAGE.test(out.text)) {
      test
        .info()
        .annotations.push({ type: "remote-outage", description: out.text });
      test.skip(true, `LiveChart is unavailable from the runner: ${out.text}`);
    }
    expect(out.ok, out.text).toBe(true);
    expect(
      await collectedRecordCount(page, out.sourceId!),
      "LiveChart records collected",
    ).toBeGreaterThan(0);
  });

  test("tvmaze scoped source: mode switch toggles show IDs and country, save with schedule mode, fetch", async ({
    page,
  }) => {
    const { dialog, next } = await openChooseStep(
      page,
      "provider.tvmazeSchedule",
      TVMAZE,
    );
    const mode = uiText("sources", "optk.tvmazeMode");
    const country = uiText("sources", "optk.mediaCountry");
    const showIds = uiText("sources", "optk.tvmazeShowIds");
    await expect(
      dialog.getByRole("combobox", { name: country, exact: true }),
    ).toBeVisible();
    await expect(dialog.getByLabel(showIds)).toHaveCount(0);
    // Followed shows: the show-IDs field appears and the schedule country goes away.
    await pickOption(
      page,
      dialog,
      mode,
      uiText("sources", "optv.followedShows"),
    );
    await expect(dialog.getByLabel(showIds)).toBeVisible();
    await expect(
      dialog.getByRole("combobox", { name: country, exact: true }),
    ).toHaveCount(0);
    // Back to the country schedule with US.
    await pickOption(
      page,
      dialog,
      mode,
      uiText("sources", "optv.scheduleCountry"),
    );
    await expect(dialog.getByLabel(showIds)).toHaveCount(0);
    await pickOption(page, dialog, country, "US");
    await acknowledge(page, dialog);
    const validated = page.waitForResponse(
      (r) =>
        r.url().includes("/validate-draft") && r.request().method() === "POST",
    );
    await dialog
      .getByRole("button", { name: uiText("sources", "validateDraft") })
      .click();
    expect((await validated).status(), "validate-draft response").toBe(200);
    await expect(dialog.getByRole("alert")).toHaveCount(0);
    await next.click(); // choose -> schedule
    await pickOption(
      page,
      dialog,
      uiText("sources", "tracking.modeLabel"),
      uiText("sources", "tracking.modeNearRealtime"),
    );
    await next.click(); // schedule -> save and test
    await dialog
      .getByRole("button", { name: uiText("sources", "verify.run") })
      .click();
    const out = await fetchOutcome(dialog);
    console.log(`RESULT tvmaze_schedule :: ${out.text}`);
    if (!out.ok && OUTAGE.test(out.code)) {
      test
        .info()
        .annotations.push({ type: "remote-outage", description: out.text });
      test.skip(true, `TVMaze is unavailable from the runner: ${out.text}`);
    }
    expect(out.ok, out.text).toBe(true);
    const [source] = await findSources(page, TVMAZE);
    expect(source, "TVMaze source created").toBeTruthy();
    expect(
      await collectedRecordCount(page, source!.id),
      "TVMaze records collected",
    ).toBeGreaterThan(0);
  });

  test("guidance: keyless vs API-key rows for Wikidata and TMDB, TVMaze CC BY-SA badge", async ({
    page,
  }) => {
    const wikidata = await openChooseStep(
      page,
      "provider.wikidataMovies",
      `${PREFIX} wikidata`,
    );
    await expect(
      wikidata.dialog.getByText(
        uiText("sources", "guideAccess.wikidata.keyless"),
      ),
    ).toBeVisible();
    await expect(
      wikidata.dialog.getByText(
        uiText("sources", "guideAccess.wikidata.keyed"),
      ),
    ).toBeVisible();
    const tmdb = await openChooseStep(
      page,
      "provider.tmdbMovies",
      `${PREFIX} tmdb`,
      "dummy-e2e-key",
    );
    await expect(
      tmdb.dialog.getByText(
        uiText("sources", "guideAccess.tmdbMovies.keyless"),
      ),
    ).toBeVisible();
    await expect(
      tmdb.dialog.getByText(uiText("sources", "guideAccess.tmdbMovies.keyed")),
    ).toBeVisible();
    await expect(
      tmdb.dialog.getByText(uiText("sources", "tmdb.attributionNotice")),
    ).toBeVisible();
    const tvmaze = await openChooseStep(
      page,
      "provider.tvmazeSchedule",
      `${PREFIX} tvmaze-guide`,
    );
    await expect(tvmaze.dialog.getByTestId("attribution-badge")).toContainText(
      "CC BY-SA",
    );
    await deleteSources(page, `${PREFIX} wikidata`);
    await deleteSources(page, `${PREFIX} tmdb`);
    await deleteSources(page, `${PREFIX} tvmaze-guide`);
  });

  test("youtube api scope: Most popular mode hides channel targets (UI only)", async ({
    page,
  }) => {
    const { dialog } = await openChooseStep(
      page,
      "provider.youtubeApi",
      `${PREFIX} youtube`,
      "dummy-e2e-key",
    );
    const targets = uiText("sources", "youtube.targetMode");
    await expect(
      dialog.getByRole("combobox", { name: targets, exact: true }),
    ).toBeVisible();
    await pickOption(
      page,
      dialog,
      uiText("sources", "youtube.apiMode"),
      uiText("sources", "youtube.modePopular"),
    );
    await expect(
      dialog.getByRole("combobox", { name: targets, exact: true }),
    ).toHaveCount(0);
    await expect(
      dialog.getByLabel(uiText("sources", "youtube.channels")),
    ).toHaveCount(0);
    await expect(
      dialog.getByLabel(uiText("sources", "youtube.categoryId")),
    ).toBeVisible();
    // Nothing is validated, fetched or kept: the draft with a dummy key is removed.
    await deleteSources(page, `${PREFIX} youtube`);
  });

  test("create the dashboard", async ({ page }) => {
    await page.goto("/dashboard");
    const first = page.getByRole("button", {
      name: uiText("dashboard", "createFirstDashboard"),
    });
    if (
      await first.waitFor({ timeout: 15_000 }).then(
        () => true,
        () => false,
      )
    )
      await first.click();
    else {
      await page
        .getByRole("button", { name: uiText("dashboard", "switchDashboard") })
        .click();
      await page
        .getByRole("menuitem", { name: uiText("dashboard", "newDashboard") })
        .click();
    }
    await page.getByLabel(uiText("dashboard", "dashboardName")).fill(DASHBOARD);
    await page
      .getByRole("dialog")
      .getByRole("button", { name: uiText("dashboard", "create"), exact: true })
      .click();
    await expect(page).toHaveURL(/\/dashboard\/[0-9a-f-]{36}/);
  });

  test("seed the fixture media source", async ({ page }) => {
    await seedFixtureSource(page);
  });

  test("media_feed gadget: cards, filter popover, active chip, clear, load more", async ({
    page,
  }) => {
    await // Items shown = 100 equals the projection page, so the Load more button (shown only while fewer than that many
    // items survive the gadget's own keyword filters) needs an exclusion: it drops fixtures 000-009 from page one.
    await addMediaGadget(page, "templateTypes.media_shelf.name", SHELF, {
      limit: "100",
      exclude: "fixture 00",
    });
    const shelf = page.getByRole("region", { name: SHELF, exact: true });
    const cards = shelf.getByRole("article");
    await expect(cards.first()).toBeVisible({ timeout: 60_000 });
    const before = await cards.count();
    expect(before).toBeGreaterThan(0);

    // Country filter: a country with no releases empties the shelf, shows an active chip; Clear all restores it.
    const filter = shelf.getByRole("button", {
      name: new RegExp(`^${escapeRe(uiText("dashboard", "feed.filter"))}`),
    });
    await filter.click();
    const countries = page.getByRole("combobox", {
      name: uiText("gadgetSettings", "media.countries"),
      exact: true,
    });
    await countries.click();
    await page
      .getByRole("textbox", { name: uiText("gadgetSettings", "media.search") })
      .fill("AQ");
    await page.keyboard.press("Enter");
    const chip = shelf.getByText(
      new RegExp(
        `${escapeRe(uiText("gadgetSettings", "media.countries"))}: AQ`,
      ),
    );
    await expect(chip).toBeVisible({ timeout: 15_000 });
    await expect(cards).toHaveCount(0, { timeout: 30_000 });
    await expect(
      shelf.getByText(uiText("dashboard", "media.empty")),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await shelf
      .getByRole("button", { name: uiText("dashboard", "media.clear") })
      .click();
    await expect(chip).toHaveCount(0);
    await expect(cards).toHaveCount(before, { timeout: 30_000 });

    // Genre filter: use a canonical genre present in the collected data; every kept card must still render, chip shows.
    const [fixture] = await findSources(page, FIXTURE);
    const items = await projectionItems(page, [fixture!.id]);
    const genre = items.flatMap((i) => i.media?.genres ?? []).find((g) => g);
    expect(genre, "fixture carries a canonical genre").toBeTruthy();
    {
      const genreLabel = uiText("gadgetSettings", `media.values.${genre}`);
      await filter.click();
      await page
        .getByRole("combobox", {
          name: uiText("gadgetSettings", "media.genres"),
          exact: true,
        })
        .click();
      await page.getByRole("option", { name: genreLabel, exact: true }).click();
      await expect(
        shelf.getByText(
          new RegExp(
            `${escapeRe(uiText("gadgetSettings", "media.genres"))}: ${escapeRe(genreLabel)}`,
          ),
        ),
      ).toBeVisible({ timeout: 15_000 });
      await expect
        .poll(() => cards.count(), { timeout: 30_000 })
        .toBeLessThanOrEqual(before);
      await page.keyboard.press("Escape");
      await shelf
        .getByRole("button", { name: uiText("dashboard", "media.clear") })
        .click();
      await expect(cards).toHaveCount(before, { timeout: 30_000 });
    }

    const more = shelf.getByRole("button", {
      name: uiText("dashboard", "feed.loadMore"),
    });
    await expect(
      more,
      `${FIXTURE_ROWS} fixture rows exceed one page`,
    ).toBeVisible();
    await more.click();
    await expect
      .poll(() => cards.count(), { timeout: 30_000 })
      .toBeGreaterThan(before);
  });

  test("media_calendar gadget: recent and upcoming buckets", async ({
    page,
  }) => {
    await // Default Items shown is 25: the earliest releases would fill it before the upcoming bucket, so show the maximum.
    await addMediaGadget(
      page,
      "templateTypes.release_calendar.name",
      CALENDAR,
      { limit: "100" },
    );
    const calendar = page.getByRole("region", { name: CALENDAR, exact: true });
    // Fixture releases sit 1-6 days back and 1-29 days ahead: the recent and upcoming buckets always exist.
    await expect(
      calendar.getByRole("heading", {
        name: uiRe("dashboard", "media.recent"),
      }),
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      calendar.getByRole("heading", {
        name: uiRe("dashboard", "media.upcoming"),
      }),
    ).toBeVisible();
    await expect(
      calendar.getByText(uiText("dashboard", "media.empty")),
    ).toHaveCount(0);
    await expect(calendar.getByRole("alert")).toHaveCount(0);
  });

  test("cleanup", async ({ page }) => {
    await page.goto("/dashboard");
    const headers = await writeHeaders(page);
    const dashboards = (await (
      await page.request.get("/api/v1/dashboards")
    ).json()) as { id: string; name: string; revision: number }[];
    for (const d of dashboards.filter((x) => x.name === DASHBOARD)) {
      await page.request.delete(
        `/api/v1/dashboards/${d.id}?expected_revision=${d.revision}`,
        { headers },
      );
    }
    const defs = (await (
      await page.request.get("/api/v1/gadget-definitions")
    ).json()) as { id: string; name: string; revision: number }[];
    for (const d of defs.filter((x) => x.name.startsWith(PREFIX))) {
      await page.request.delete(
        `/api/v1/gadget-definitions/${d.id}?expected_revision=${d.revision}`,
        { headers: await writeHeaders(page) },
      );
    }
    await deleteSources(page, PREFIX);
    await removeBuiltinSource(page, LIVECHART.id);
  });
});
