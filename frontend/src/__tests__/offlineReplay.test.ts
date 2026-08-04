import { describe, expect, it } from "vitest";

import { ApiRequestError } from "../api/core";
import {
  DUPLICATE_SCAN_ACK_CODE,
  NON_RETRYABLE_SCAN_CODES,
  classifyReplayFailure,
} from "../features/warehouse/offline/errorPolicy";

describe("classifyReplayFailure", () => {
  it("дубликат скана считается успехом: backend уже знает этот КИЗ", () => {
    const error = new ApiRequestError(409, "Conflict", "already scanned", DUPLICATE_SCAN_ACK_CODE);
    expect(classifyReplayFailure(error)).toBe("synced");
  });

  it.each(NON_RETRYABLE_SCAN_CODES)("409 %s блокируется навсегда", (code) => {
    expect(classifyReplayFailure(new ApiRequestError(409, "Conflict", "", code))).toBe("blocked");
  });

  it("409 с неизвестным кодом блокируется, как на десктопе", () => {
    expect(classifyReplayFailure(new ApiRequestError(409, "Conflict", "", "something_new"))).toBe("blocked");
  });

  it("422 kiz_format_invalid блокируется, а не крутится вечно", () => {
    const error = new ApiRequestError(422, "Unprocessable Entity", "", "kiz_format_invalid");
    expect(classifyReplayFailure(error)).toBe("blocked");
  });

  it("404 на несуществующей позиции блокируется", () => {
    expect(classifyReplayFailure(new ApiRequestError(404, "Not Found", "Order item not found"))).toBe("blocked");
  });

  it("403 блокируется: прав не прибавится от повтора", () => {
    expect(classifyReplayFailure(new ApiRequestError(403, "Forbidden", ""))).toBe("blocked");
  });

  it("401 повторяется: нужна новая сессия, а не потеря скана", () => {
    expect(classifyReplayFailure(new ApiRequestError(401, "Unauthorized", ""))).toBe("retry");
  });

  it("408 и 429 повторяются", () => {
    expect(classifyReplayFailure(new ApiRequestError(408, "Request Timeout", ""))).toBe("retry");
    expect(classifyReplayFailure(new ApiRequestError(429, "Too Many Requests", ""))).toBe("retry");
  });

  it("5xx повторяется", () => {
    expect(classifyReplayFailure(new ApiRequestError(503, "Service Unavailable", ""))).toBe("retry");
    expect(classifyReplayFailure(new ApiRequestError(502, "Bad Gateway", ""))).toBe("retry");
  });

  it("сетевая ошибка повторяется", () => {
    expect(classifyReplayFailure(new TypeError("Failed to fetch"))).toBe("retry");
  });

  it("таймаут запроса повторяется", () => {
    expect(classifyReplayFailure(new Error("Запрос /api/v1/scans не ответил за 15 сек."))).toBe("retry");
  });
});
