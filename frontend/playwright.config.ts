import { defineConfig, devices } from "@playwright/test";

// Browser tests (U15). They expect a running stack: the API (default http://localhost:8000, seeded with
// backend/scripts/seed_demo.py) and this frontend served on E2E_BASE_URL (default http://localhost:3000),
// for example `npm run build && PORT=3000 HOSTNAME=127.0.0.1 node .next/standalone/server.js`.
// See docs/evaluation/browser-tests.md.
export default defineConfig({
  testDir: "e2e",
  outputDir: "test-results/artifacts",
  // One worker: the tests share one seeded stack (office B uploads, worker queue), so they run in order.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    locale: "he-IL",
    timezoneId: "Asia/Jerusalem",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], locale: "he-IL" } }],
});
