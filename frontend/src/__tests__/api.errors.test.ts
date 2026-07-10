import { delay, http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { ApiRequestError, apiRequest, type ApiConfig } from "../api";
import { defaultHandlers, server } from "./server";

const config: ApiConfig = { apiUrl: "", token: "", csrfToken: "synthetic-csrf" };

beforeEach(() => server.use(...defaultHandlers));

describe("apiRequest negative-state matrix", () => {
  it.each([
    {
      status: 401,
      statusText: "Unauthorized",
      body: { detail: { code: "session_expired", message: "Сессия истекла" } },
      code: "session_expired",
      message: "Сессия истекла",
    },
    {
      status: 409,
      statusText: "Conflict",
      body: { detail: { code: "write_conflict", message: "Данные уже изменены" } },
      code: "write_conflict",
      message: "Данные уже изменены",
    },
    {
      status: 422,
      statusText: "Unprocessable Entity",
      body: {
        detail: {
          code: "validation_failed",
          message: "Проверьте поля",
          errors: [{ order_id: "order-1", message: "Некорректный статус" }],
        },
      },
      code: "validation_failed",
      message: "Проверьте поля: Некорректный статус [order-1]",
    },
  ])("preserves the $status structured error contract", async ({ status, statusText, body, code, message }) => {
    server.use(http.get("/test/error", () => HttpResponse.json(body, { status, statusText })));

    const rejection = await apiRequest(config, "/test/error").catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(ApiRequestError);
    expect(rejection).toMatchObject({ status, statusText, code });
    expect((rejection as Error).message).toContain(message);
  });

  it("sanitizes a 500 HTML proxy response instead of rendering raw markup", async () => {
    server.use(http.get("/test/error", () => HttpResponse.text(
      "<html><head><title>Gateway unavailable</title></head><body>private proxy body</body></html>",
      { status: 500, statusText: "Internal Server Error", headers: { "Content-Type": "text/html" } },
    )));

    await expect(apiRequest(config, "/test/error")).rejects.toMatchObject({
      status: 500,
      message: expect.stringContaining("API вернул HTML-ошибку: Gateway unavailable"),
    });
  });

  it("aborts a deterministic timeout and exposes a bounded operator message", async () => {
    server.use(http.get("/test/timeout", async () => {
      await delay("infinite");
      return HttpResponse.json({ ok: true });
    }));

    const rejection = await apiRequest(config, "/test/timeout", { timeoutMs: 5 })
      .catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(Error);
    expect((rejection as Error).message).toBe("Запрос /test/timeout не ответил за 0 сек.");
  });
});
