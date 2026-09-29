// The #86 acceptance walk in a browser: browse, filter by tag, search terms, open a
// dictionary, copy its install command, download its file and check the hash against the index.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

const PROOF = "test-results/proof";
const COMMITTED_INDEX = new URL("../../stt_cli/dictionaries/index.json", import.meta.url);
const COMMITTED_TAGS = new URL("../../stt_cli/dictionaries/tags.json", import.meta.url);

async function catalogIndex(request) {
  const response = await request.get("/api/dictionaries");
  expect(response.status()).toBe(200);
  return { bytes: await response.body(), index: await response.json() };
}

function byId(index, id) {
  const entry = index.dictionaries.find((row) => row.id === id);
  expect(entry, `${id} is in the catalog`).toBeDefined();
  return entry;
}

test("the API serves this checkout's committed index byte for byte", async ({ request }) => {
  const { bytes } = await catalogIndex(request);
  expect(bytes.equals(readFileSync(COMMITTED_INDEX))).toBe(true);
});

test("the catalog lists every dictionary, filters by tag and searches term text", async ({ page, request }) => {
  const { index } = await catalogIndex(request);
  await page.goto("/");
  const cards = page.locator("#cards .card:visible");
  await expect(cards).toHaveCount(index.dictionaries.length);

  const tagged = index.dictionaries.filter((row) => row.tags.includes("medical-general"));
  await page.locator('button[data-tag="medical-general"]').click();
  await expect(page).toHaveURL(/\?tag=medical-general$/);
  await expect(cards).toHaveCount(tagged.length);
  await expect(page.locator("#status")).toHaveText(`${tagged.length} of ${index.dictionaries.length} dictionaries`);
  await page.screenshot({ path: `${PROOF}/catalog-filtered-by-tag.png`, fullPage: true });

  // A term that is in neither the title nor the tags (slugs or labels) of its dictionary:
  // search reaches terms.
  await page.locator('button[data-tag="medical-general"]').click();
  const target = byId(index, "devops-cloud");
  const file = await (await request.get(`/api/dictionaries/${target.id}`)).json();
  const tags = JSON.parse(readFileSync(COMMITTED_TAGS, "utf8"));
  const elsewhere = [target.id, target.title, ...target.tags, ...target.tags.map((tag) => tags[tag].label)]
    .join("\n")
    .toLowerCase();
  const term = file.terms.map((t) => t.term).find((t) => !elsewhere.includes(t.toLowerCase()));
  expect(term, "devops-cloud has a term found only in its term list").toBeDefined();
  await page.locator("#search").fill(term);
  await expect(page.locator(`#cards .card[data-id="${target.id}"]`)).toBeVisible();
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe(term);
  await page.screenshot({ path: `${PROOF}/catalog-searched-by-term.png`, fullPage: true });

  await page.locator("#search").fill("zzzz-no-such-word");
  await expect(cards).toHaveCount(0);
  await expect(page.locator("#empty")).toBeVisible();
});

test("a dictionary page shows its facts and copies its real install command", async ({ page, context, request }) => {
  const { index } = await catalogIndex(request);
  const entry = byId(index, "russian-it-anglicisms");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  await page.locator(`#cards .card[data-id="${entry.id}"] .card-title`).click();
  await expect(page).toHaveURL(new RegExp(`/d/${entry.id}$`));

  await expect(page.locator("h1")).toHaveText(entry.title);
  const facts = page.locator(".facts");
  await expect(facts.locator("dt:has-text('Id') + dd")).toHaveText(entry.id);
  await expect(facts.locator("dt:has-text('Version') + dd")).toHaveText(String(entry.version));
  await expect(page.locator("h1 + .tags .tag")).toHaveCount(entry.tags.length);
  await expect(page.locator("table.terms tbody tr")).toHaveCount(entry.term_count);

  const copy = page.locator(".install .copy");
  await copy.locator("button").click();
  await expect(copy.locator("button")).toHaveText("Copied");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`stt dict install ${entry.id}`);
  await page.locator(".install").screenshot({ path: `${PROOF}/detail-copy-install.png` });
  await page.screenshot({ path: `${PROOF}/detail-page.png` });
});

test("the last of several copies keeps its confirmation for the full time", async ({ page, context, request }) => {
  const { index } = await catalogIndex(request);
  const entry = byId(index, "russian-it-anglicisms");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  // A paused clock: only runFor moves time, so a slow runner cannot fire a timer early.
  await page.clock.pauseAt(new Date("2026-01-01T00:00:00Z"));
  await page.goto(`/d/${entry.id}`);
  const button = page.locator(".install .copy button");
  // A clipboard read queues behind the click's write, so once it answers the handler has run.
  const settled = () => page.evaluate(() => navigator.clipboard.readText());
  await button.click();
  await settled(); // t=0: the first click's timer is armed, due at 1600
  await page.clock.runFor(1000);

  // From here on a clipboard write waits until the test releases it, oldest first.
  await page.evaluate(() => {
    const write = navigator.clipboard.writeText.bind(navigator.clipboard);
    const held = [];
    navigator.clipboard.writeText = (text) => new Promise((resolve) => held.push(() => resolve(write(text))));
    window.releaseWrite = () => held.shift()();
  });
  const release = async () => {
    await page.evaluate(() => window.releaseWrite());
    await settled();
  };
  await button.click(); // t=1000: two more copies, both writes pending
  await button.click();
  await page.clock.runFor(1000); // t=2000: the first timer was due at 1600
  await expect(button).toHaveText("Copied");
  await release(); // the second write lands and arms a timer due at 3600
  await page.clock.runFor(1000);
  await release(); // t=3000: the third lands and replaces it with one due at 4600
  await page.clock.runFor(1000); // t=4000
  await expect(button).toHaveText("Copied");
  await page.clock.runFor(700); // t=4700
  await expect(button).toHaveText("Copy");
});

test("the downloaded dictionary file hashes to its catalog index entry", async ({ page, request }) => {
  const { index } = await catalogIndex(request);
  const entry = byId(index, "pharmacology");
  await page.goto(`/d/${entry.id}`);
  const [download] = await Promise.all([page.waitForEvent("download"), page.locator("a.download").click()]);
  expect(download.suggestedFilename()).toBe(`${entry.id}.json`);
  const bytes = readFileSync(await download.path());
  expect(createHash("sha256").update(bytes).digest("hex")).toBe(entry.sha256);
  const api = await (await request.get(`/api/dictionaries/${entry.id}`)).body();
  expect(api.equals(bytes)).toBe(true);
});

test("an unknown dictionary is a 404 page and a 404 API response", async ({ page, request }) => {
  expect((await page.goto("/d/no-such-dictionary")).status()).toBe(404);
  await expect(page.locator("h1")).toHaveText("Not found");
  expect((await request.get("/api/dictionaries/no-such-dictionary")).status()).toBe(404);
});
