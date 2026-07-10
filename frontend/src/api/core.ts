export type ApiConfig = {
  apiUrl: string;
  token: string;
  csrfToken: string;
};

export type RequestOptions = {
  method?: string;
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
};

export class ApiRequestError extends Error {
  status: number;
  statusText: string;
  code: string;

  constructor(status: number, statusText: string, detail: string, code = "") {
    const prefix = `${status} ${statusText}`.trim();
    super(detail ? `${prefix}: ${detail}` : prefix || "Ошибка запроса");
    this.name = "ApiRequestError";
    this.status = status;
    this.statusText = statusText;
    this.code = code;
  }
}

export const LONG_REQUEST_TIMEOUT_MS = 45_000;
const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;
const MAX_TEXT_ERROR_LENGTH = 500;

export function defaultApiUrl() {
  return "";
}

export async function apiRequest<T>(
  config: ApiConfig,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const apiUrl = config.apiUrl.replace(/\/$/, "");
  const method = (options.method ?? "GET").toUpperCase();
  const bearerRequest = Boolean(config.token);
  const unsafeCookieRequest = !bearerRequest && !["GET", "HEAD", "OPTIONS"].includes(method);
  ensureCookieApiIsSameOrigin(apiUrl, bearerRequest);
  const timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
  const timeoutController = timeoutMs > 0 ? new AbortController() : undefined;
  const signal = timeoutController && options.signal
    ? AbortSignal.any([timeoutController.signal, options.signal])
    : timeoutController?.signal ?? options.signal;
  const timeoutId = timeoutController ? setTimeout(() => timeoutController.abort(), timeoutMs) : undefined;
  const response = await fetch(`${apiUrl}${path}`, {
    method,
    credentials: bearerRequest ? "omit" : "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(config.token ? { Authorization: `Bearer ${config.token}` } : {}),
      ...(unsafeCookieRequest && config.csrfToken ? { "X-TakSklad-CSRF": config.csrfToken } : {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
    signal,
  }).catch((error) => {
    if (isAbortError(error) && options.signal?.aborted) throw error;
    if (isAbortError(error) && (!options.signal || timeoutController?.signal.aborted)) {
      throw new Error(`Запрос ${path} не ответил за ${Math.round(timeoutMs / 1000)} сек.`);
    }
    throw error;
  }).finally(() => {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    let code = "";
    const body = await response.text();
    if (body) {
      try {
        const payload = JSON.parse(body);
        detail = formatApiErrorDetail(payload);
        code = apiErrorCode(payload);
      } catch {
        detail = formatTextApiErrorDetail(response.status, body);
      }
    }
    throw new ApiRequestError(response.status, response.statusText, detail, code);
  }

  return response.json() as Promise<T>;
}

function apiErrorCode(payload: unknown): string {
  if (!isRecord(payload)) return "";
  const detail = isRecord(payload.detail) ? payload.detail : payload;
  return typeof detail.code === "string" ? detail.code : "";
}

export function ensureCookieApiIsSameOrigin(apiUrl: string, bearerRequest: boolean) {
  if (bearerRequest || !apiUrl || typeof window === "undefined") return;
  const target = new URL(apiUrl, window.location.origin);
  if (target.origin !== window.location.origin) {
    throw new Error("Cookie-сессия разрешена только для same-origin API.");
  }
}

function isAbortError(error: unknown) {
  return (
    error instanceof DOMException
    || (typeof error === "object" && error !== null && "name" in error)
  ) && error.name === "AbortError";
}

function formatApiErrorDetail(payload: unknown): string {
  const detail = isRecord(payload) && "detail" in payload ? payload.detail : payload;
  if (typeof detail === "string") return detail;
  if (isRecord(detail)) {
    const message = typeof detail.message === "string" ? detail.message : "";
    const errors = Array.isArray(detail.errors)
      ? detail.errors.map(formatApiErrorItem).filter(Boolean)
      : [];
    if (message && errors.length) return `${message}: ${errors.join("; ")}`;
    if (message) return message;
  }
  try {
    return JSON.stringify(detail ?? payload);
  } catch {
    return "Ошибка запроса";
  }
}

function formatTextApiErrorDetail(status: number, body: string): string {
  const text = body.trim();
  if (status === 401) return "Сессия закончилась или доступ к API не подтвержден. Войдите снова.";
  if (!text) return "";
  if (looksLikeHtml(text)) {
    const title = htmlTitle(text) || htmlHeading(text);
    const cleanTitle = normalizeErrorText(stripHtml(title));
    return cleanTitle ? `API вернул HTML-ошибку: ${cleanTitle}` : "API вернул HTML-ошибку";
  }
  return normalizeErrorText(text).slice(0, MAX_TEXT_ERROR_LENGTH);
}

function looksLikeHtml(value: string) {
  return /<\s*(html|head|body|title|center|h1)\b/i.test(value);
}

function htmlTitle(value: string) {
  return value.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] ?? "";
}

function htmlHeading(value: string) {
  return value.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i)?.[1] ?? "";
}

function stripHtml(value: string) {
  return value.replace(/<[^>]*>/g, " ");
}

function normalizeErrorText(value: string) {
  return value.replace(/\s+/g, " ").trim();
}

function formatApiErrorItem(value: unknown): string {
  if (!isRecord(value)) return "";
  const message = typeof value.message === "string" ? value.message : "";
  const orderId = typeof value.order_id === "string" ? value.order_id : "";
  if (message && orderId) return `${message} [${orderId}]`;
  return message;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
