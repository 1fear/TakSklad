import { describe, expect, it } from "vitest";

import { ApiRequestError } from "../api";
import { calendarReportEmptyZoneNotice } from "../workspace/calendarReportNotice";

describe("calendarReportEmptyZoneNotice", () => {
  it("даёт согласованный текст уведомления для 404 от отчёта логистики", () => {
    const error = new ApiRequestError(404, "Not Found", "Не удалось выгрузить отчёт логистики");
    expect(calendarReportEmptyZoneNotice(error)).toBe("В этой зоне за день нет заказов для выгрузки");
  });

  it("уходит в обычный путь для любой другой ошибки", () => {
    expect(calendarReportEmptyZoneNotice(new ApiRequestError(500, "Internal Server Error", "сбой"))).toBeNull();
    expect(calendarReportEmptyZoneNotice(new Error("network down"))).toBeNull();
  });
});
