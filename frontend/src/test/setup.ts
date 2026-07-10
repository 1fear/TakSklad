import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { toHaveNoViolations } from "jest-axe";
import { afterAll, afterEach, beforeAll, beforeEach, expect, vi } from "vitest";

import { server } from "./server";

type ConsoleLevel = "error" | "warn";
type ConsoleDiagnostic = {
  level: ConsoleLevel;
  values: unknown[];
};

let diagnostics: ConsoleDiagnostic[] = [];
let errorSpy: ReturnType<typeof vi.spyOn>;
let warnSpy: ReturnType<typeof vi.spyOn>;

function formatDiagnostic({ level, values }: ConsoleDiagnostic): string {
  const message = values
    .map((value) => value instanceof Error ? value.stack ?? value.message : String(value))
    .join(" ");
  return `${level}: ${message}`;
}

expect.extend(toHaveNoViolations);

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
});

beforeEach(() => {
  diagnostics = [];
  errorSpy = vi.spyOn(console, "error").mockImplementation((...values) => {
    diagnostics.push({ level: "error", values });
  });
  warnSpy = vi.spyOn(console, "warn").mockImplementation((...values) => {
    diagnostics.push({ level: "warn", values });
  });
});

afterEach(() => {
  cleanup();
  server.resetHandlers();

  const captured = diagnostics.map(formatDiagnostic);
  errorSpy.mockRestore();
  warnSpy.mockRestore();
  diagnostics = [];

  if (captured.length > 0) {
    throw new Error(`Unexpected console diagnostics:\n${captured.join("\n")}`);
  }
});

afterAll(() => {
  server.close();
});
