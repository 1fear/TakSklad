import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    clearMocks: true,
    mockReset: true,
    restoreMocks: true,
    fileParallelism: false,
    coverage: {
      provider: "v8",
      reportsDirectory: "./coverage",
      reporter: ["text", "json-summary", "html"],
      // The API client is the deterministic application boundary shared by every
      // operator workflow. The stateful App surface is protected by focused
      // characterization, accessibility and browser suites instead of a noisy
      // line-coverage target over its rendering branches.
      include: ["src/api.ts", "src/api/**/*.ts"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/__tests__/**",
        "src/test/**",
        "src/vite-env.d.ts",
      ],
      thresholds: {
        lines: 85,
        statements: 85,
        branches: 80,
      },
    },
  },
});
