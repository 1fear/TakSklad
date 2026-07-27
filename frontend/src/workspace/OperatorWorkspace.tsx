import { AlertCircle, CheckCircle2, Loader2, LogOut, ShieldCheck } from "lucide-react";
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";

import { ApiRequestError, type ApiConfig } from "../api";
import { hasAdminSurfaceAccess } from "./surface";
import "../operator-workspace.css";

const WarehousePanel = lazy(() =>
  import("../features/warehouse/WarehousePanel").then((module) => ({ default: module.default })),
);

type OperatorWorkspaceProps = {
  config: ApiConfig;
  authUser: string;
  authRole: string;
  authPermissions: string[];
  onSessionExpired: () => void;
  onLogout: (config: ApiConfig) => void;
};

export default function OperatorWorkspace({
  config,
  authUser,
  authRole,
  authPermissions,
  onSessionExpired,
  onLogout,
}: OperatorWorkspaceProps) {
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const canUseAdminSurface = hasAdminSurfaceAccess(authPermissions);
  const canWarehouseWrite = authPermissions.includes("warehouse:write");
  const userLabel = useMemo(() => (
    authUser ? `${maskLogin(authUser)} · ${roleLabel(authRole)}` : roleLabel(authRole)
  ), [authRole, authUser]);

  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, []);

  function handleSessionExpired() {
    setBusyAction("");
    setError("");
    setNotice("");
    onSessionExpired();
  }

  function showActionError(actionError: unknown, fallback: string) {
    if (actionError instanceof ApiRequestError && actionError.status === 401) {
      handleSessionExpired();
      return;
    }
    if (actionError instanceof ApiRequestError && actionError.status === 403) {
      if (["csrf_invalid", "origin_denied"].includes(actionError.code)) {
        setError("Защита браузерной сессии устарела. Обновите страницу и войдите снова.");
      } else {
        setError("Эта роль не имеет доступа к складскому действию.");
      }
      return;
    }
    setError(actionError instanceof Error ? actionError.message : fallback);
  }

  function logout() {
    setBusyAction("logout");
    setError("");
    setNotice("");
    onLogout(config);
  }

  return (
    <div className="operator-app-shell">
      <header className="operator-topbar">
        <div className="operator-brand">
          <div>
            <p>Складская web-панель</p>
            <h1 ref={headingRef} tabIndex={-1}>Операторский складской контур</h1>
          </div>
          <span className="operator-copy">Сканирование КИЗов, возвраты и печать через защищённый API</span>
        </div>
        <div className="operator-topbar-actions" aria-label="Действия оператора">
          <div className="user-pill" title={authUser ? `Пользователь ${maskLogin(authUser)}` : "Пользователь"}>
            <ShieldCheck size={17} />
            {userLabel}
          </div>
          {canUseAdminSurface && (
            <a className="ghost-button operator-link" href="/admin">
              Панель управления
            </a>
          )}
          <button className="ghost-button" onClick={logout} disabled={Boolean(busyAction)} title="Выйти">
            {busyAction === "logout" ? <Loader2 className="spin" size={18} /> : <LogOut size={18} />}
            Выйти
          </button>
        </div>
      </header>

      {(error || notice) && (
        <div
          className={error ? "message error operator-message" : "message success operator-message"}
          role={error ? "alert" : "status"}
          aria-live={error ? "assertive" : "polite"}
        >
          {error ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
          <span>{error || notice}</span>
        </div>
      )}

      <main className="operator-workspace">
        <Suspense fallback={<OperatorPanelFallback />}>
          <WarehousePanel
            config={config}
            canWrite={canWarehouseWrite}
            actor={authUser || "web"}
            onError={showActionError}
            onNotice={(message) => {
              setError("");
              setNotice(message);
            }}
          />
        </Suspense>
      </main>
    </div>
  );
}

function OperatorPanelFallback() {
  return (
    <section className="table-panel empty-state" role="status" aria-live="polite">
      <Loader2 className="spin" size={18} />
      Загружаем складской контур...
    </section>
  );
}

function maskLogin(value: string) {
  const digits = value.replace(/\D/g, "");
  if (digits.length <= 4) return value;
  return `+${digits.slice(0, 3)} ... ${digits.slice(-4)}`;
}

function roleLabel(value: string) {
  if (value === "admin") return "admin";
  if (value === "logistics_slots") return "логистика";
  if (value === "operator") return "оператор";
  return "read-only";
}
