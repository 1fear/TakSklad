import { expect, test } from "@playwright/test";
import axe from "axe-core";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { installSyntheticApi } from "./synthetic-api";
import budgets from "../performance-budgets.json" with { type: "json" };

const enabled = process.env.TAKSKLAD_PERF === "1";
const artifactRoot = resolve(
  process.env.TAKSKLAD_PERF_ARTIFACT_DIR ?? resolve(import.meta.dirname, "../../.release-state/frontend-performance"),
);
const screenshotRoot = resolve(artifactRoot, "screenshots");
const performancePartPath = resolve(artifactRoot, "performance-part.json");
const keyboardPartPath = resolve(artifactRoot, "keyboard-part.json");
const viewportMatrix = [
  { width: 1440, height: 900 },
  { width: 1280, height: 720 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
] as const;

type BrowserEvidence = Record<string, unknown>;

let performanceEvidence: BrowserEvidence | null = null;
let keyboardEvidence: BrowserEvidence | null = null;

test.skip(!enabled, "Run through npm run perf so the pinned synthetic profile and artifact directory are explicit.");

test.beforeAll(() => mkdirSync(screenshotRoot, { recursive: true }));

test.afterAll(() => {
  const storedPerformance = existsSync(performancePartPath)
    ? JSON.parse(readFileSync(performancePartPath, "utf8")) as BrowserEvidence
    : performanceEvidence;
  const storedKeyboard = existsSync(keyboardPartPath)
    ? JSON.parse(readFileSync(keyboardPartPath, "utf8")) as BrowserEvidence
    : keyboardEvidence;
  const pass = storedPerformance?.pass === true && storedKeyboard?.pass === true;
  writeFileSync(resolve(artifactRoot, "browser-summary.json"), `${JSON.stringify({
    schema_version: 1,
    pass,
    performance: storedPerformance,
    keyboard: storedKeyboard,
    incomplete_parts: [
      ...(!storedPerformance ? ["performance"] : []),
      ...(!storedKeyboard ? ["keyboard"] : []),
    ],
  }, null, 2)}\n`);
});

test("@performance pinned synthetic Web Vitals, axe, screenshots and network budgets", async ({ browser, page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const api = await installSyntheticApi(page);
  const consoleCounts = { error: 0, warning: 0, pageerror: 0 };
  const networkCounts: Record<string, number> = {};
  let externalRequests = 0;
  let failedRequests = 0;
  let httpErrorResponses = 0;

  page.on("console", (message) => {
    if (message.type() === "error") consoleCounts.error += 1;
    if (message.type() === "warning") consoleCounts.warning += 1;
  });
  page.on("pageerror", () => { consoleCounts.pageerror += 1; });
  page.on("requestfailed", () => { failedRequests += 1; });
  page.on("response", (response) => {
    if (response.url().startsWith("http://127.0.0.1:4180/") && response.status() >= 400) httpErrorResponses += 1;
  });
  page.on("request", (request) => {
    if (request.url().startsWith("data:") || request.url().startsWith("blob:")) return;
    const parsed = new URL(request.url());
    if (parsed.origin !== "http://127.0.0.1:4180") {
      externalRequests += 1;
      return;
    }
    const key = `${request.method()} ${parsed.pathname}`;
    networkCounts[key] = (networkCounts[key] ?? 0) + 1;
  });

  await page.addInitScript(() => {
    const metrics = {
      cls: 0,
      eventDurations: [] as number[],
      eventTimingSupported: false,
      lcp: 0,
    };
    Object.defineProperty(globalThis, "__takskladPerformanceMetrics", { value: metrics });
    try {
      new PerformanceObserver((entries) => {
        for (const entry of entries.getEntries()) metrics.lcp = Math.max(metrics.lcp, entry.startTime);
      }).observe({ type: "largest-contentful-paint", buffered: true });
    } catch { /* The support flag remains observable in the final evidence. */ }
    try {
      new PerformanceObserver((entries) => {
        for (const entry of entries.getEntries()) {
          const shift = entry as PerformanceEntry & { hadRecentInput?: boolean; value?: number };
          if (!shift.hadRecentInput) metrics.cls += shift.value ?? 0;
        }
      }).observe({ type: "layout-shift", buffered: true });
    } catch { /* The metric stays zero only when the API provides no entries. */ }
    try {
      metrics.eventTimingSupported = PerformanceObserver.supportedEntryTypes.includes("event");
      new PerformanceObserver((entries) => {
        for (const entry of entries.getEntries()) {
          const event = entry as PerformanceEntry & { duration?: number; interactionId?: number };
          if ((event.interactionId ?? 0) > 0) metrics.eventDurations.push(event.duration ?? 0);
        }
      }).observe({ type: "event", buffered: true, durationThreshold: 16 } as PerformanceObserverInit);
    } catch { metrics.eventTimingSupported = false; }
  });

  await page.goto("/", { waitUntil: "networkidle" });
  await expect(page.getByRole("heading", { name: "Позиции заказов" })).toBeVisible();

  const selector = page.getByLabel("Выбрать заказ Альфа Тест");
  await selector.focus();
  await page.keyboard.press("Space");
  await page.keyboard.press("Space");
  await page.waitForTimeout(100);

  const metrics = await page.evaluate(() => {
    const value = (globalThis as typeof globalThis & {
      __takskladPerformanceMetrics: {
        cls: number;
        eventDurations: number[];
        eventTimingSupported: boolean;
        lcp: number;
      };
    }).__takskladPerformanceMetrics;
    return value;
  });
  const observedInp = metrics.eventDurations.length > 0 ? Math.max(...metrics.eventDurations) : null;
  const inpWithinBudget = observedInp !== null && observedInp <= budgets.interactionToNextPaintMs;

  await page.addScriptTag({ content: axe.source });
  const axeResults = await page.evaluate(async () => {
    const axeApi = (globalThis as typeof globalThis & {
      axe: { run: (root: Document) => Promise<{ violations: Array<{ impact: string | null; id: string }> }> };
    }).axe;
    return axeApi.run(document);
  });
  const seriousOrCritical = axeResults.violations.filter((violation) => ["serious", "critical"].includes(violation.impact ?? ""));

  const screenshots = [];
  const reducedMotion = await page.evaluate(() => {
    const toMilliseconds = (value: string) => value.split(",").reduce((maximum, entry) => {
      const duration = entry.trim();
      const milliseconds = duration.endsWith("ms")
        ? Number.parseFloat(duration)
        : Number.parseFloat(duration) * 1000;
      return Math.max(maximum, Number.isFinite(milliseconds) ? milliseconds : 0);
    }, 0);
    let maximumAnimationMs = 0;
    let maximumTransitionMs = 0;
    for (const element of document.querySelectorAll("*")) {
      const style = getComputedStyle(element);
      maximumAnimationMs = Math.max(maximumAnimationMs, toMilliseconds(style.animationDuration));
      maximumTransitionMs = Math.max(maximumTransitionMs, toMilliseconds(style.transitionDuration));
    }
    return {
      media_matches: matchMedia("(prefers-reduced-motion: reduce)").matches,
      maximum_animation_ms: maximumAnimationMs,
      maximum_transition_ms: maximumTransitionMs,
      pass: matchMedia("(prefers-reduced-motion: reduce)").matches
        && maximumAnimationMs <= 10
        && maximumTransitionMs <= 10,
    };
  });
  for (const viewport of viewportMatrix) {
    await page.setViewportSize(viewport);
    await page.waitForTimeout(50);
    const overflow = await page.evaluate(() => ({
      document_scroll_width: document.documentElement.scrollWidth,
      viewport_width: document.documentElement.clientWidth,
      pass: document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
    }));
    const screenshotPath = resolve(screenshotRoot, `${viewport.width}x${viewport.height}.png`);
    await page.screenshot({ path: screenshotPath, fullPage: false, animations: "disabled", caret: "hide" });
    screenshots.push({ ...viewport, absolute_path: screenshotPath, overflow });
  }

  const allowedInitialApiRequests = [
    /^GET \/api\/v1\/auth\/session(?:\?|$)/,
    /^GET \/api\/v1\/admin\/table(?:\?|$)/,
    /^GET \/api\/v1\/admin\/dashboard\/day-summary(?:\?|$)/,
  ];
  const unexpectedInitialApiRequests = api.requests.filter((request) => (
    request.includes("/api/") && !allowedInitialApiRequests.some((pattern) => pattern.test(request))
  ));
  const criticalRequestCount = api.requests.filter((request) => (
    /^GET \/api\/v1\/admin\/table(?:\?|$)/.test(request)
    || /^GET \/api\/v1\/admin\/dashboard\/day-summary(?:\?|$)/.test(request)
  )).length;
  const expectedBrowserVersion = JSON.parse(
    await (await import("node:fs/promises")).readFile(resolve(import.meta.dirname, "../node_modules/playwright-core/browsers.json"), "utf8"),
  ).browsers.find((entry: { name: string }) => entry.name === "chromium").browserVersion;
  const actualBrowserVersion = browser.version();

  performanceEvidence = {
    pass: actualBrowserVersion === expectedBrowserVersion
      && metrics.lcp > 0
      && metrics.lcp <= budgets.largestContentfulPaintMs
      && metrics.cls <= budgets.cumulativeLayoutShift
      && metrics.eventTimingSupported
      && inpWithinBudget
      && axeResults.violations.length === 0
      && consoleCounts.error === 0
      && consoleCounts.warning === 0
      && consoleCounts.pageerror === 0
      && externalRequests === 0
      && failedRequests === 0
      && httpErrorResponses === 0
      && unexpectedInitialApiRequests.length === 0
      && criticalRequestCount === 2
      && reducedMotion.pass
      && screenshots.every((entry) => entry.overflow.pass),
    browser: {
      engine: "Playwright-bundled Chrome for Testing",
      expected_version: expectedBrowserVersion,
      actual_version: actualBrowserVersion,
      executable: "playwright.chromium.executablePath()",
    },
    web_vitals: {
      lcp_ms: metrics.lcp,
      lcp_budget_ms: budgets.largestContentfulPaintMs,
      inp_ms: observedInp,
      inp_budget_evidence_ms: observedInp,
      inp_budget_ms: budgets.interactionToNextPaintMs,
      inp_precision: observedInp === null ? "missing PerformanceEventTiming interaction sample; gate fails closed" : "PerformanceEventTiming interaction duration",
      cls: metrics.cls,
      cls_budget: budgets.cumulativeLayoutShift,
    },
    browser_gates: {
      performance: metrics.lcp > 0
        && metrics.lcp <= budgets.largestContentfulPaintMs
        && metrics.cls <= budgets.cumulativeLayoutShift
        && inpWithinBudget,
      accessibility: axeResults.violations.length === 0,
      best_practices: consoleCounts.error + consoleCounts.warning + consoleCounts.pageerror + externalRequests + failedRequests + httpErrorResponses === 0,
    },
    axe: {
      engine: "axe-core 4.12.1",
      total_violations: axeResults.violations.length,
      serious_or_critical: seriousOrCritical.length,
      violation_ids: axeResults.violations.map((violation) => violation.id).sort(),
    },
    console: consoleCounts,
    network: {
      local_request_counts: Object.fromEntries(Object.entries(networkCounts).sort(([left], [right]) => left.localeCompare(right))),
      external_requests: externalRequests,
      failed_requests: failedRequests,
      http_error_responses: httpErrorResponses,
      hidden_panel_requests: unexpectedInitialApiRequests.length,
      unexpected_initial_api_requests: unexpectedInitialApiRequests,
      initial_critical_requests: criticalRequestCount,
    },
    reduced_motion: reducedMotion,
    screenshots,
  };
  writeFileSync(performancePartPath, `${JSON.stringify(performanceEvidence, null, 2)}\n`);

  expect(actualBrowserVersion).toBe(expectedBrowserVersion);
  expect(metrics.lcp).toBeGreaterThan(0);
  expect(metrics.lcp).toBeLessThanOrEqual(budgets.largestContentfulPaintMs);
  expect(observedInp, "A real PerformanceEventTiming interaction sample is required").not.toBeNull();
  expect(observedInp ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(budgets.interactionToNextPaintMs);
  expect(metrics.cls).toBeLessThanOrEqual(budgets.cumulativeLayoutShift);
  expect(axeResults.violations.length, `axe violation ids: ${axeResults.violations.map((violation) => violation.id).sort().join(", ")}`).toBe(0);
  expect(consoleCounts).toEqual({ error: 0, warning: 0, pageerror: 0 });
  expect({ externalRequests, failedRequests, unexpectedInitialApiRequestCount: unexpectedInitialApiRequests.length, httpErrorResponses }).toEqual({
    externalRequests: 0,
    failedRequests: 0,
    unexpectedInitialApiRequestCount: 0,
    httpErrorResponses: 0,
  });
  expect(criticalRequestCount).toBe(2);
  expect(reducedMotion.pass).toBe(true);
  expect(screenshots.every((entry) => entry.overflow.pass)).toBe(true);
});

test("@performance keyboard-only login, navigation, selection, action, dropdown and logout", async ({ page }) => {
  const api = await installSyntheticApi(page, { authenticated: false });
  page.on("dialog", async (dialog) => dialog.accept(dialog.type() === "prompt" ? "Synthetic keyboard reason" : undefined));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Вход в панель" })).toBeVisible();

  const matrix: Array<{ step: string; focus_visible: boolean; pass: boolean }> = [];
  keyboardEvidence = {
    pass: false,
    matrix,
    controlled_dropdown_keys: ["ArrowDown", "Home", "End", "Enter", "Escape"],
    action_requests: { resyncs: 0 },
  };
  writeFileSync(keyboardPartPath, `${JSON.stringify(keyboardEvidence, null, 2)}\n`);
  const focusByTab = async (locator: ReturnType<typeof page.locator>, step: string) => {
    let focused = false;
    for (let index = 0; index < 40; index += 1) {
      await page.keyboard.press("Tab");
      focused = await locator.evaluate((element) => document.activeElement === element).catch(() => false);
      if (focused) break;
    }
    const focusVisible = focused && await locator.evaluate((element) => {
      const style = getComputedStyle(element);
      return style.boxShadow !== "none" || (style.outlineStyle !== "none" && style.outlineWidth !== "0px");
    });
    matrix.push({ step, focus_visible: focusVisible, pass: focused && focusVisible });
    expect(focused, `${step} must be reachable with Tab`).toBe(true);
    expect(focusVisible, `${step} must expose a visible focus indicator`).toBe(true);
  };

  const phone = page.locator('input[inputmode="tel"]');
  await focusByTab(phone, "login phone");
  await page.keyboard.type("+998 90 000 00 01");
  const password = page.locator('input[type="password"]');
  await focusByTab(password, "login password");
  await page.keyboard.type("synthetic-password");
  const login = page.getByRole("button", { name: "Войти" });
  await focusByTab(login, "login submit");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Позиции заказов" })).toBeVisible();

  const clients = page.getByRole("button", { name: "Клиенты" });
  await focusByTab(clients, "navigation");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Клиенты и таймслоты" })).toBeVisible();

  const table = page.getByRole("button", { name: "Таблица" });
  await focusByTab(table, "return to table");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Позиции заказов" })).toBeVisible();

  const statusFilter = page.getByLabel("Фильтр статуса заказа");
  await focusByTab(statusFilter, "controlled dropdown");
  await page.keyboard.press("End");
  await expect(statusFilter).toHaveValue("removed_from_google");
  await page.keyboard.press("Home");
  await expect(statusFilter).toHaveValue("all");
  await page.keyboard.press("ArrowDown");
  await expect(statusFilter).toHaveValue("active");
  await page.keyboard.press("Enter");
  await expect(statusFilter).toHaveValue("active");
  await expect(statusFilter).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(statusFilter).toHaveValue("active");
  await expect(statusFilter).toBeFocused();

  const orderSelector = page.getByLabel("Выбрать заказ Альфа Тест");
  await focusByTab(orderSelector, "order detail selection");
  await page.keyboard.press("Space");
  await expect(orderSelector).toBeChecked();

  const action = page.getByRole("button", { name: "Ресинк Google" });
  await focusByTab(action, "order action");
  await page.keyboard.press("Enter");
  await expect.poll(() => api.resyncs).toBe(1);

  const logout = page.getByRole("button", { name: "Выйти" });
  await focusByTab(logout, "logout");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Вход в панель" })).toBeVisible();

  keyboardEvidence = {
    pass: matrix.every((entry) => entry.pass)
      && api.resyncs === 1
      && api.requests.includes("POST /api/v1/auth/login")
      && api.requests.includes("POST /api/v1/auth/logout"),
    matrix,
    controlled_dropdown_keys: ["ArrowDown", "Home", "End", "Enter", "Escape"],
    action_requests: { resyncs: api.resyncs },
  };
  writeFileSync(keyboardPartPath, `${JSON.stringify(keyboardEvidence, null, 2)}\n`);
  expect(keyboardEvidence.pass).toBe(true);
});
