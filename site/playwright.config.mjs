// End-to-end tests of the site in a real browser.
//   npm run e2e                                    builds, serves locally on :4086, tests it
//   BASE_URL=https://stt-dicts-marketplace.vercel.app npm run e2e   tests a deployment
// Screenshots used as proof land in test-results/proof/.

import { defineConfig, devices } from "@playwright/test";

const external = process.env.BASE_URL;

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  reporter: "list",
  use: {
    baseURL: external ?? "http://localhost:4086",
    ...devices["Desktop Chrome"],
    viewport: { width: 1280, height: 900 },
    acceptDownloads: true,
  },
  webServer: external
    ? undefined
    : { command: "node build.mjs && node serve.mjs 4086", port: 4086, reuseExistingServer: true },
});
