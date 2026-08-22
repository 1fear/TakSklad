import type { FormEvent } from "react";
import { lazy, Suspense, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import { getAuthSession, loginWeb, logoutWeb, type AuthSession } from "./api/auth";
import { defaultApiUrl, type ApiConfig } from "./api/core";
import { LoadingGate, LoginScreen } from "./auth/AuthShell";
import {
  alternateSurfacePath,
  hasAdminSurfaceAccess,
  hasOperatorSurfaceAccess,
  resolveAppSurface,
  surfaceTitle,
  type AppSurface,
} from "./workspace/surface";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/600.css";
import "./styles.css";

const AdminWorkspace = lazy(() => import("./workspace/AdminWorkspace"));
const OperatorWorkspace = lazy(() => import("./workspace/OperatorWorkspace"));

function initialConfig(): ApiConfig {
  return { apiUrl: defaultApiUrl(), token: "", csrfToken: "" };
}

function App() {
  const [config, setConfig] = useState<ApiConfig>(initialConfig);
  const [authChecked, setAuthChecked] = useState(false);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [loginPhone, setLoginPhone] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginLoading, setLoginLoading] = useState(false);
  const [loginError, setLoginError] = useState("");
  const surface = resolveAppSurface(window.location.pathname);

  useEffect(() => {
    const controller = new AbortController();
    void initializeAuth(controller.signal);
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function initializeAuth(signal: AbortSignal) {
    setAuthChecked(false);
    setLoginError("");
    try {
      const nextSession = await getAuthSession(config, signal);
      const nextConfig = { ...config, csrfToken: nextSession.csrf_token || "" };
      setConfig(nextConfig);
      setSession(nextSession.authenticated ? nextSession : null);
    } catch {
      if (signal.aborted) return;
      setSession(null);
      setConfig((current) => ({ ...current, csrfToken: "" }));
      setLoginError("Не удалось проверить сессию. Проверьте соединение и повторите вход.");
    } finally {
      if (!signal.aborted) setAuthChecked(true);
    }
  }

  async function submitLogin(event: FormEvent) {
    event.preventDefault();
    const normalizedPhone = loginPhone.trim().replace(/[^\d+]/g, "");
    if (!normalizedPhone || !loginPassword) {
      setLoginError("Введите телефон и пароль");
      return;
    }

    setLoginLoading(true);
    setLoginError("");
    try {
      const nextSession = await loginWeb(config, normalizedPhone, loginPassword);
      setConfig((current) => ({ ...current, csrfToken: nextSession.csrf_token || "" }));
      setLoginPassword("");
      setSession(nextSession.authenticated ? nextSession : null);
    } catch (failure) {
      const message = failure instanceof Error ? failure.message : "";
      setLoginError(loginFailureMessage(message));
    } finally {
      setLoginLoading(false);
    }
  }

  function expireSession() {
    setConfig((current) => ({ ...current, csrfToken: "" }));
    setSession(null);
    setLoginError("Сессия закончилась. Войдите снова.");
  }

  function logout(logoutConfig: ApiConfig) {
    setConfig((current) => ({ ...current, csrfToken: "" }));
    setSession(null);
    setLoginError("");
    void logoutWeb(logoutConfig).catch((failure) => {
      const message = failure instanceof Error ? failure.message : "";
      setLoginError(message || "Сервер не подтвердил выход. Войдите снова перед продолжением.");
    });
  }

  if (!authChecked) return <LoadingGate />;

  if (!session) {
    return (
      <LoginScreen
        surface={surface}
        phone={loginPhone}
        password={loginPassword}
        error={loginError}
        loading={loginLoading}
        onPhoneChange={setLoginPhone}
        onPasswordChange={setLoginPassword}
        onSubmit={submitLogin}
      />
    );
  }

  const canUseOperatorSurface = hasOperatorSurfaceAccess(session.permissions);
  const canUseAdminSurface = hasAdminSurfaceAccess(session.permissions);

  if (surface === "operator" && !canUseOperatorSurface) {
    return (
      <AccessDeniedScreen
        surface={surface}
        canUseAlternateSurface={canUseAdminSurface}
        title="Нет доступа к складской web-панели"
        message={canUseAdminSurface
          ? "Для операционного контура нужно право warehouse:read. Откройте административный контур."
          : "Для операционного контура нужно право warehouse:read. Обратитесь к администратору склада."}
        onLogout={() => logout(config)}
      />
    );
  }

  if (surface === "admin" && !canUseAdminSurface) {
    return (
      <AccessDeniedScreen
        surface={surface}
        canUseAlternateSurface={canUseOperatorSurface}
        title="Нет доступа к панели управления"
        message={canUseOperatorSurface
          ? "Для административного контура нужны права admin:read или доступный admin-раздел. Откройте складской контур."
          : "Для административного контура нужны права admin:read или доступный admin-раздел. Обратитесь к администратору склада."}
        onLogout={() => logout(config)}
      />
    );
  }

  return (
    <Suspense fallback={<LoadingGate />}>
      {surface === "admin" ? (
        <AdminWorkspace
          config={config}
          authUser={session.login}
          authRole={session.role}
          authPermissions={session.permissions}
          onSessionExpired={expireSession}
          onLogout={logout}
        />
      ) : (
        <OperatorWorkspace
          config={config}
          authUser={session.login}
          authRole={session.role}
          authPermissions={session.permissions}
          onSessionExpired={expireSession}
          onLogout={logout}
        />
      )}
    </Suspense>
  );
}

function loginFailureMessage(message: string) {
  if (message.includes("401")) return "Телефон или пароль не подходят";
  if (message.includes("429")) return "Слишком много попыток. Попробуйте позже.";
  if (message.includes("503")) return "Вход пока не настроен на сервере.";
  if (message.includes("500") || message.includes("502") || message.includes("504")) {
    return "Сайт не может подключиться к backend. Обновите страницу или попробуйте позже.";
  }
  return "Не удалось выполнить вход. Проверьте связь и попробуйте ещё раз.";
}

function AccessDeniedScreen({
  surface,
  canUseAlternateSurface,
  title,
  message,
  onLogout,
}: {
  surface: AppSurface;
  canUseAlternateSurface: boolean;
  title: string;
  message: string;
  onLogout: () => void;
}) {
  const fallbackPath = alternateSurfacePath(surface);
  const fallbackTitle = surfaceTitle(surface === "admin" ? "operator" : "admin");

  return (
    <main className="login-shell">
      <section className="login-panel" aria-label="Доступ к панели">
        <div className="login-panel-head">
          <h1>{title}</h1>
        </div>
        <p>{message}</p>
        {canUseAlternateSurface && (
          <a className="login-submit" href={fallbackPath}>
            Открыть {fallbackTitle.toLowerCase()}
          </a>
        )}
        <button className="login-submit" type="button" onClick={onLogout}>
          Выйти и сменить пользователя
        </button>
      </section>
    </main>
  );
}

export default App;

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
