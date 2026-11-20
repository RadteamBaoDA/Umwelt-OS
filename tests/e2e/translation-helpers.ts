import type { Page } from "@playwright/test";
import { expect } from "./fixtures";
import { collectedRecordCount, escapeRe, findSources } from "./sources-helpers";
import { uiText } from "./ui-text";

/** Shared helpers for the translation e2e specs (BBC World dashboard with a Feed gadget bound to it). */
export const BBC = "BBC World News";
export const FEED_NAME = "e2e-translation feed";
export const BATCHES = "/api/v1/translations/batches";

/** Per-item Translate button: named `Translate “<title>”` when the row has a title, plain `Translate` otherwise. */
export const itemButton = (scope: Page | ReturnType<Page["getByRole"]>) =>
  scope.getByRole("button", {
    name: new RegExp(
      `^${escapeRe(uiText("translations", "translate"))}( “.*”)?$`,
    ),
  });

/** Records POST /translations/batches (item counts) and GET /translations/batches/{id} calls made by `page`. */
export function watchBatches(page: Page) {
  const posts: number[] = [];
  const gets: string[] = [];
  page.on("request", (req) => {
    const url = new URL(req.url());
    if (url.pathname === BATCHES && req.method() === "POST")
      posts.push((req.postDataJSON() as { items: unknown[] }).items.length);
    else if (url.pathname.startsWith(`${BATCHES}/`) && req.method() === "GET")
      gets.push(url.pathname);
  });
  return { posts, gets };
}

/** Owner PATCH of the workspace translation setting; `target` keeps the current language when omitted. */
export async function setTranslation(
  page: Page,
  enabled: boolean,
  target?: "vi" | "en",
) {
  const session = (await (
    await page.request.get("/api/v1/auth/session")
  ).json()) as { csrfToken: string };
  const origin = new URL((await page.request.get("/api/v1/auth/session")).url())
    .origin;
  const cur = (await (
    await page.request.get("/api/v1/settings/translation")
  ).json()) as {
    configuration_revision: number;
    target_language: string;
  };
  const res = await page.request.patch("/api/v1/settings/translation", {
    headers: { "X-CSRF-Token": session.csrfToken, Origin: origin },
    data: {
      enabled,
      target_language: target ?? cur.target_language,
      expected_revision: cur.configuration_revision,
    },
  });
  expect(res.ok(), `set translation enabled=${enabled}`).toBe(true);
}

/** Opens a dashboard path and waits for the shell to settle. */
export async function openDashboard(page: Page, url: string) {
  expect(url, "setup test created a dashboard").toBeTruthy();
  await page.goto(url);
  await expect(
    page.getByRole("button", { name: uiText("dashboard", "toggleEdit") }),
  ).toBeEnabled({ timeout: 30_000 });
  await expect(page.locator("[data-route-loader]")).toBeHidden({
    timeout: 30_000,
  });
}

/**
 * Creates the BBC World starter source, a dashboard with a Feed gadget bound to it, waits for clustered stories and
 * enables workspace translation. Returns the dashboard path. Needs outbound public network.
 */
export async function setupBbcDashboard(
  page: Page,
  home: string,
): Promise<string> {
  await page.goto("/dashboard");
  const create = page.getByRole("button", {
    name: uiText("dashboard", "createFirstDashboard"),
  });
  if (
    await create.waitFor({ timeout: 20_000 }).then(
      () => true,
      () => false,
    )
  ) {
    await create.click();
    await page.getByLabel(uiText("dashboard", "dashboardName")).fill(home);
    await page
      .getByRole("dialog")
      .getByRole("button", {
        name: uiText("dashboard", "create"),
        exact: true,
      })
      .click();
  }
  await page
    .getByRole("button", { name: uiText("dashboard", "setup.connect") })
    .click();
  const wizard = page.getByRole("dialog", {
    name: uiText("sources", "starter.title"),
  });
  await expect(wizard).toBeVisible();
  const boxes = wizard.getByRole("checkbox");
  await expect(boxes.first()).toBeVisible({ timeout: 30_000 });
  for (const box of await boxes.all()) {
    const label = (await box.getAttribute("aria-label")) ?? "";
    const keep =
      label === uiText("sources", "starter.itemSelect", { name: BBC });
    if ((await box.isChecked()) !== keep) await box.setChecked(keep);
  }
  await wizard
    .getByRole("button", { name: uiText("sources", "next"), exact: true })
    .click();
  const rowDone = new RegExp(
    `${escapeRe(uiText("sources", "starter.statusDone"))}|${escapeRe(uiText("sources", "starter.statusExists"))}`,
    "i",
  );
  await expect(wizard.getByTestId("starter-row-bbc_world")).toContainText(
    rowDone,
    { timeout: 120_000 },
  );
  await wizard
    .getByRole("button", { name: uiText("sources", "buildDashboard") })
    .click();
  const picker = page
    .getByRole("dialog")
    .filter({ hasText: uiText("dashboard", "applyPreset") });
  await expect(picker).toBeVisible();
  await picker
    .getByRole("button", { name: uiText("dashboard", "applyPreset") })
    .click();
  await expect(picker).toBeHidden({ timeout: 60_000 });
  await expect(
    page.getByRole("button", { name: uiText("dashboard", "toggleEdit") }),
  ).toBeEnabled({ timeout: 30_000 });
  const dashboardUrl = new URL(page.url()).pathname;

  // The preset's Overview tab has no feed; add one bound to BBC so item Translate buttons exist.
  await page
    .getByRole("button", { name: uiText("dashboard", "toggleEdit") })
    .click();
  await page
    .getByRole("button", {
      name: uiText("dashboard", "addGadget.label"),
      exact: true,
    })
    .click();
  const gadget = page.getByRole("dialog", {
    name: uiText("dashboard", "addGadget.title"),
  });
  await gadget
    .getByRole("button", {
      name: new RegExp(
        `^${escapeRe(uiText("gadgetSettings", "templateTypes.feed.name"))}\\b`,
      ),
    })
    .click();
  await gadget
    .getByRole("checkbox", { name: new RegExp(`^${escapeRe(BBC)}`) })
    .click();
  await gadget
    .getByLabel(uiText("gadgetSettings", "name"), { exact: true })
    .fill(FEED_NAME);
  const add = gadget.getByRole("button", {
    name: uiText("dashboard", "addGadget.label"),
    exact: true,
  });
  await expect(add).toBeEnabled({ timeout: 120_000 });
  await add.click();
  await expect(gadget).toBeHidden({ timeout: 60_000 });
  const save = page.getByRole("button", {
    name: uiText("dashboard", "saveLayout"),
  });
  if (await save.isEnabled()) await save.click();
  await page
    .getByRole("button", { name: uiText("dashboard", "finishEditing") })
    .click();

  const [source] = await findSources(page, "BBC");
  expect(source, "BBC source exists").toBeTruthy();
  expect(
    await collectedRecordCount(page, source!.id, 120_000),
    "BBC records",
  ).toBeGreaterThan(0);
  await expect
    .poll(
      async () =>
        (
          (await (
            await page.request.get(
              `/api/v1/stories?limit=50&source_ids=${source!.id}`,
            )
          ).json()) as { items: unknown[] }
        ).items.length,
      { timeout: 180_000, message: "stories clustered" },
    )
    .toBeGreaterThan(0);
  await setTranslation(page, true);
  return dashboardUrl;
}
