import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import process from "node:process";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath, URL } from "node:url";
import { gzipSync } from "node:zlib";
import { chromium } from "playwright";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(frontendRoot, "..");
const artifactRoot = resolve(repositoryRoot, ".release-state/frontend-performance");
const distRoot = resolve(frontendRoot, "dist");
const budgets = JSON.parse(readFileSync(resolve(frontendRoot, "performance-budgets.json"), "utf8"));

rmSync(artifactRoot, { recursive: true, force: true });
mkdirSync(artifactRoot, { recursive: true });

const build = spawnSync("npm", ["run", "build"], {
  cwd: frontendRoot,
  env: process.env,
  stdio: "inherit",
});
if (build.status !== 0) process.exit(build.status ?? 1);

const indexHtml = readFileSync(resolve(distRoot, "index.html"), "utf8");
const initialAssetPaths = [...indexHtml.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)]
  .map((match) => match[1])
  .filter((value, index, values) => values.indexOf(value) === index);
const initialAssetNames = new Set(initialAssetPaths.map((publicPath) => basename(publicPath)));

const assetRows = readdirSync(resolve(distRoot, "assets"))
  .filter((asset) => asset.endsWith(".js") || asset.endsWith(".css"))
  .sort()
  .map((asset) => {
    const absolutePath = resolve(distRoot, "assets", asset);
    const content = readFileSync(absolutePath);
    const type = asset.endsWith(".js") ? "javascript" : "css";
    const gzipBytes = gzipSync(content, { level: 9 }).byteLength;
    const budgetBytes = type === "javascript"
      ? budgets.javaScriptChunkGzipBytes
      : type === "css"
        ? budgets.cssChunkGzipBytes
        : null;
    return {
      asset,
      type,
      initial: initialAssetNames.has(asset),
      raw_bytes: content.byteLength,
      gzip_bytes: gzipBytes,
      budget_bytes: budgetBytes,
      pass: budgetBytes === null || gzipBytes <= budgetBytes,
    };
  });

const initialJavaScriptGzipBytes = assetRows
  .filter((row) => row.type === "javascript" && row.initial)
  .reduce((total, row) => total + row.gzip_bytes, 0);
const bundleSummary = {
  schema_version: 1,
  source: "frontend/dist/index.html",
  compression: "gzip level 9",
  budgets: {
    initial_javascript_gzip_bytes: budgets.initialJavaScriptGzipBytes,
    javascript_chunk_gzip_bytes: budgets.javaScriptChunkGzipBytes,
    css_chunk_gzip_bytes: budgets.cssChunkGzipBytes,
  },
  initial_javascript_gzip_bytes: initialJavaScriptGzipBytes,
  assets: assetRows,
  pass: initialJavaScriptGzipBytes <= budgets.initialJavaScriptGzipBytes && assetRows.every((row) => row.pass),
};
writeFileSync(resolve(artifactRoot, "bundle-summary.json"), `${JSON.stringify(bundleSummary, null, 2)}\n`);

process.stdout.write("Bundle/chunk budget (gzip bytes)\n");
process.stdout.write("asset | type | gzip | budget | result\n");
for (const row of assetRows) {
  process.stdout.write(`${row.asset} | ${row.type} | ${row.gzip_bytes} | ${row.budget_bytes ?? "n/a"} | ${row.pass ? "PASS" : "FAIL"}\n`);
}
process.stdout.write(`initial-js | javascript | ${initialJavaScriptGzipBytes} | ${budgets.initialJavaScriptGzipBytes} | ${initialJavaScriptGzipBytes <= budgets.initialJavaScriptGzipBytes ? "PASS" : "FAIL"}\n`);
if (!bundleSummary.pass) process.exit(1);

const commandEnvironment = {
  ...process.env,
  TAKSKLAD_PERF: "1",
  TAKSKLAD_PERF_ARTIFACT_DIR: artifactRoot,
};
delete commandEnvironment.NO_COLOR;

const server = spawn(process.execPath, [resolve(frontendRoot, "scripts/performance-server.mjs")], {
  cwd: frontendRoot,
  env: commandEnvironment,
  stdio: ["ignore", "pipe", "pipe"],
});
server.stdout.on("data", (chunk) => process.stdout.write(chunk));
server.stderr.on("data", (chunk) => process.stderr.write(chunk));

async function waitForPerformanceServer() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (server.exitCode !== null) throw new Error(`Synthetic performance server exited with ${server.exitCode}`);
    try {
      const response = await globalThis.fetch("http://127.0.0.1:4180/");
      if (response.ok) return;
    } catch { /* Retry only the bounded local readiness probe. */ }
    await delay(100);
  }
  throw new Error("Synthetic performance server did not become ready in 5 seconds");
}

await waitForPerformanceServer();

const lighthouseRawPath = resolve(artifactRoot, "lighthouse-raw.json");
const lighthouseEnvironment = {
  ...commandEnvironment,
  CHROME_PATH: chromium.executablePath(),
};
const lighthouse = spawnSync(
  resolve(frontendRoot, "node_modules/.bin/lighthouse"),
  [
    "http://127.0.0.1:4180/",
    "--output=json",
    `--output-path=${lighthouseRawPath}`,
    "--only-categories=performance,accessibility,best-practices",
    "--preset=desktop",
    "--throttling-method=provided",
    "--disable-storage-reset",
    "--disable-full-page-screenshot",
    "--no-enable-error-reporting",
    "--quiet",
    "--chrome-flags=--headless=new --disable-background-networking --disable-component-update --disable-default-apps --disable-dev-shm-usage --no-first-run",
  ],
  {
    cwd: frontendRoot,
    env: lighthouseEnvironment,
    stdio: "inherit",
  },
);

let lighthouseSummary = {
  schema_version: 1,
  pass: false,
  version: "13.4.0",
  scores: { performance: null, accessibility: null, best_practices: null },
  budgets: {
    performance: budgets.lighthousePerformance,
    accessibility: budgets.lighthouseAccessibility,
    best_practices: budgets.lighthouseBestPractices,
  },
  metrics: null,
  network: null,
  raw_json_path: lighthouseRawPath,
  error: lighthouse.status === 0 ? "Lighthouse JSON was not produced" : `Lighthouse exited with ${lighthouse.status ?? 1}`,
};
if (existsSync(lighthouseRawPath)) {
  const lhr = JSON.parse(readFileSync(lighthouseRawPath, "utf8"));
  const performanceScore = lhr.categories?.performance?.score ?? null;
  const accessibilityScore = lhr.categories?.accessibility?.score ?? null;
  const bestPracticesScore = lhr.categories?.["best-practices"]?.score ?? null;
  const networkItems = lhr.audits?.["network-requests"]?.details?.items ?? [];
  const externalRequests = networkItems.filter((item) => {
    try {
      const parsed = new URL(item.url);
      return ["http:", "https:"].includes(parsed.protocol) && parsed.origin !== "http://127.0.0.1:4180";
    } catch {
      return false;
    }
  }).length;
  const httpErrorResponses = networkItems.filter((item) => Number(item.statusCode ?? 0) >= 400).length;
  lighthouseSummary = {
    schema_version: 1,
    pass: lighthouse.status === 0
      && performanceScore !== null
      && performanceScore >= budgets.lighthousePerformance
      && accessibilityScore === budgets.lighthouseAccessibility
      && bestPracticesScore !== null
      && bestPracticesScore >= budgets.lighthouseBestPractices
      && externalRequests === 0
      && httpErrorResponses === 0,
    version: lhr.lighthouseVersion,
    scores: {
      performance: performanceScore,
      accessibility: accessibilityScore,
      best_practices: bestPracticesScore,
    },
    budgets: {
      performance: budgets.lighthousePerformance,
      accessibility: budgets.lighthouseAccessibility,
      best_practices: budgets.lighthouseBestPractices,
    },
    metrics: {
      first_contentful_paint_ms: lhr.audits?.["first-contentful-paint"]?.numericValue ?? null,
      largest_contentful_paint_ms: lhr.audits?.["largest-contentful-paint"]?.numericValue ?? null,
      cumulative_layout_shift: lhr.audits?.["cumulative-layout-shift"]?.numericValue ?? null,
      total_blocking_time_ms: lhr.audits?.["total-blocking-time"]?.numericValue ?? null,
    },
    network: {
      total_requests: networkItems.length,
      external_requests: externalRequests,
      http_error_responses: httpErrorResponses,
    },
    browser: {
      chrome_path_source: "playwright.chromium.executablePath()",
      runtime_user_agent: lhr.environment?.hostUserAgent ?? null,
    },
    raw_json_path: lighthouseRawPath,
    error: null,
  };
}
const lighthouseSummaryPath = resolve(artifactRoot, "lighthouse-summary.json");
writeFileSync(lighthouseSummaryPath, `${JSON.stringify(lighthouseSummary, null, 2)}\n`);
process.stdout.write(`Lighthouse scores: performance=${lighthouseSummary.scores.performance} accessibility=${lighthouseSummary.scores.accessibility} best-practices=${lighthouseSummary.scores.best_practices} result=${lighthouseSummary.pass ? "PASS" : "FAIL"}\n`);

const playwright = spawnSync(
  resolve(frontendRoot, "node_modules/.bin/playwright"),
  ["test", "--config", "playwright.perf.config.ts"],
  {
    cwd: frontendRoot,
    env: commandEnvironment,
    stdio: "inherit",
  },
);

server.kill("SIGTERM");
if (server.exitCode === null) await Promise.race([once(server, "exit"), delay(2_000)]);
if (server.exitCode === null) server.kill("SIGKILL");

const browserSummaryPath = resolve(artifactRoot, "browser-summary.json");
const browserSummary = existsSync(browserSummaryPath)
  ? JSON.parse(readFileSync(browserSummaryPath, "utf8"))
  : null;
const combinedSummary = {
  schema_version: 1,
  deterministic_profile: "local synthetic API + Playwright-bundled Chrome for Testing",
  bundle: bundleSummary,
  browser: browserSummary,
  lighthouse: lighthouseSummary,
  pass: Boolean(bundleSummary.pass && browserSummary?.pass && lighthouseSummary.pass),
};
const summaryPath = resolve(artifactRoot, "summary.json");
writeFileSync(summaryPath, `${JSON.stringify(combinedSummary, null, 2)}\n`);
process.stdout.write(`Performance evidence: ${summaryPath}\n`);
process.stdout.write(`${JSON.stringify(combinedSummary, null, 2)}\n`);

if (lighthouse.status !== 0 || !lighthouseSummary.pass || playwright.status !== 0 || !browserSummary?.pass) {
  process.exit(lighthouse.status || playwright.status || 1);
}
