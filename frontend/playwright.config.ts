import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";

const ci = Boolean(process.env.CI);
const installedChrome = [
  process.env.PLAYWRIGHT_CHROME_EXECUTABLE,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
].find((candidate): candidate is string => Boolean(candidate && existsSync(candidate)));

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: ci,
  retries: ci ? 2 : 0,
  workers: 1,
  reporter: ci ? "line" : "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    launchOptions: installedChrome ? { executablePath: installedChrome } : undefined,
  },
  projects: [
    {
      name: "synthetic-chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !ci,
    timeout: 120_000,
    env: {
      VITE_TAKSKLAD_DEV_API_URL: "",
    },
  },
});
