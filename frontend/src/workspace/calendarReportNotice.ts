import { ApiRequestError } from "../api";

export const EMPTY_ZONE_REPORT_NOTICE = "В этой зоне за день нет заказов для выгрузки";

export function calendarReportEmptyZoneNotice(error: unknown): string | null {
  if (error instanceof ApiRequestError && error.status === 404) {
    return EMPTY_ZONE_REPORT_NOTICE;
  }
  return null;
}
