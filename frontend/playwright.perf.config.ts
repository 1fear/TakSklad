import { chromium, defineConfig } from "@playwright/test";
import { resolve } from "node:path";

const repositoryRoot = resolve(import.meta.dirname, "..");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "performance.spec.ts",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: "line",
  outputDir: resolve(repositoryRoot, ".release-state/frontend-performance/playwright"),
  use: {
    baseURL: "http://127.0.0.1:4180",
    browserName: "chromium",
    colorScheme: "light",
    deviceScaleFactor: 1,
    locale: "ru-RU",
    reducedMotion: "reduce",
    serviceWorkers: "block",
    timezoneId: "Asia/Tashkent",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    launchOptions: {
      executablePath: chromium.executablePath(),
    },
  },
  webServer: {
    command: "node scripts/performance-server.mjs",
    url: "http://127.0.0.1:4180",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
