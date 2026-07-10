import { apiRequest, type ApiConfig } from "./core";

export type AuthSession = {
  authenticated: boolean;
  login: string;
  role: string;
  permissions: string[];
  expires_at: string | null;
  csrf_token: string;
};

export function getAuthSession(config: ApiConfig, signal?: AbortSignal) {
  return apiRequest<AuthSession>(config, "/api/v1/auth/session", { signal });
}

export function loginWeb(config: ApiConfig, login: string, password: string) {
  return apiRequest<AuthSession>(config, "/api/v1/auth/login", {
    method: "POST",
    body: { login, password },
  });
}

export function logoutWeb(config: ApiConfig) {
  return apiRequest<AuthSession>(config, "/api/v1/auth/logout", { method: "POST" });
}
