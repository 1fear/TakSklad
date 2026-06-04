import {
  Activity,
  AlertCircle,
  BarChart3,
  Box,
  CheckCircle2,
  ClipboardList,
  Database,
  FileSpreadsheet,
  History,
  KeyRound,
  Lock,
  LogOut,
  Loader2,
  PackageCheck,
  Phone,
  RefreshCw,
  RotateCcw,
  Search,
  Server,
  ShieldCheck,
  SquareCode,
  Undo2,
} from "lucide-react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  AdminActivity,
  AdminTable,
  AdminTableRow,
  ApiConfig,
  ApiRequestError,
  DayReport,
  ImportRecord,
  SkladBotDryRun,
  archiveOrderWithoutKiz,
  cancelOrder,
  completeOrdersWithoutKiz,
  defaultApiUrl,
  downloadDiagnosticsLog,
  getAdminTable,
  getAuthSession,
  getDayReport,
  listImports,
  listSkladBotDryRuns,
  loginWeb,
  logoutWeb,
  rebuildSkladBotDryRun,
  resetOrderForRescan,
  restoreOrder,
  resyncGoogleOrder,
  resyncSkladBotOrder,
  retryPendingGoogle,
  syncSources,
} from "./api";
import "./styles.css";

type Tab = "table" | "report" | "imports" | "skladbotDryRun" | "activity";
type StatusFilter = "all" | "active" | "archive" | "archive_no_kiz" | "cancelled" | "returned" | "removed_from_google";
type ScanFilter = "all" | "not_started" | "in_progress" | "completed" | "over_scanned" | "no_plan";
type SkladBotFilter = "all" | "found" | "missing" | "problem";
type GoogleFilter = "all" | "synced" | "pending" | "removed_from_google" | "unknown";
type OrderActionKind = "resync" | "archive" | "completeWithoutKiz" | "cancel" | "resetRescan" | "restore" | "resyncSkladBot";
type ActionState = {
  selectedCount: number;
  disabledReason: Record<OrderActionKind, string>;
  plannedBlocks: number;
  scannedBlocks: number;
  pendingGoogleExports: number;
};

const SAME_ORIGIN_API_LABEL = "same-origin /api";

function loadConfig(): ApiConfig {
  return { apiUrl: defaultApiUrl(), token: "" };
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function App() {
  const [config] = useState<ApiConfig>(() => loadConfig());
  const [adminTable, setAdminTable] = useState<AdminTable | null>(null);
  const [imports, setImports] = useState<ImportRecord[]>([]);
  const [dryRuns, setDryRuns] = useState<SkladBotDryRun[]>([]);
  const [report, setReport] = useState<DayReport | null>(null);
  const [reportDate, setReportDate] = useState(todayIso());
  const [shipmentDateFilter, setShipmentDateFilter] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [scanFilter, setScanFilter] = useState<ScanFilter>("all");
  const [skladbotFilter, setSkladbotFilter] = useState<SkladBotFilter>("all");
  const [googleFilter, setGoogleFilter] = useState<GoogleFilter>("all");
  const [tab, setTab] = useState<Tab>("table");
  const [selectedOrderIds, setSelectedOrderIds] = useState<string[]>([]);
  const [busyAction, setBusyAction] = useState("");
  const [loading, setLoading] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [authUser, setAuthUser] = useState("");
  const [loginPhone, setLoginPhone] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginLoading, setLoginLoading] = useState(false);
  const [loginError, setLoginError] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const rows = adminTable?.rows ?? [];
  const filteredRows = useMemo(() => {
    const query = search.trim().toLowerCase();
    return rows.filter((row) => {
      if (statusFilter !== "all" && row.status_bucket !== statusFilter) return false;
      if (shipmentDateFilter && row.order_date !== shipmentDateFilter) return false;
      if (scanFilter !== "all" && scanState(row) !== scanFilter) return false;
      if (!matchesSkladBotFilter(row, skladbotFilter)) return false;
      if (googleFilter !== "all" && row.google_sheet_status !== googleFilter) return false;
      if (!query) return true;
      return [
        row.client,
        row.address,
        row.representative ?? "",
        row.payment_type,
        row.product,
        row.source_file,
        row.skladbot_request_number,
        row.skladbot_request_id,
      ].some((value) => value.toLowerCase().includes(query));
    });
  }, [rows, search, statusFilter, shipmentDateFilter, scanFilter, skladbotFilter, googleFilter]);
  const selectedRows = useMemo(
    () => rows.filter((row) => selectedOrderIds.includes(row.order_id)),
    [rows, selectedOrderIds],
  );
  const visibleOrderIds = useMemo(
    () => Array.from(new Set(filteredRows.map((row) => row.order_id))),
    [filteredRows],
  );
  const allVisibleSelected = visibleOrderIds.length > 0 && visibleOrderIds.every((id) => selectedOrderIds.includes(id));
  const selectedOrder = useMemo(
    () => selectedOrderIds.length === 1 ? rows.find((row) => row.order_id === selectedOrderIds[0]) : undefined,
    [rows, selectedOrderIds],
  );
  const selectedActionState = useMemo(
    () => buildActionState(selectedOrderIds, selectedRows),
    [selectedOrderIds, selectedRows],
  );

  async function refreshAll(activeConfig = config, showNotice = true) {
    setLoading(true);
    setError("");
    if (showNotice) setNotice("");
    try {
      const [nextAdminTable, nextReport, nextImports, nextDryRuns] = await Promise.all([
        getAdminTable(activeConfig),
        getDayReport(activeConfig, reportDate),
        listImports(activeConfig),
        listSkladBotDryRuns(activeConfig),
      ]);
      setAdminTable(nextAdminTable);
      setReport(nextReport);
      setImports(nextImports);
      setDryRuns(nextDryRuns);
      setSelectedOrderIds((current) => current.filter((id) => nextAdminTable.rows.some((row) => row.order_id === id)));
      if (showNotice) {
        setNotice(`Обновлено: ${new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`);
      }
    } catch (refreshError) {
      const message = refreshError instanceof Error ? refreshError.message : "Не удалось загрузить данные";
      if (refreshError instanceof ApiRequestError && refreshError.status === 401) {
        setAuthenticated(false);
        setAuthUser("");
        setLoginError("Сессия закончилась. Войдите снова.");
      } else {
        setError(message);
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void initializeAuth();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function initializeAuth() {
    setAuthChecked(false);
    setLoginError("");
    try {
      const session = await getAuthSession(config);
      setAuthenticated(session.authenticated);
      setAuthUser(session.login || "");
      if (session.authenticated) {
        await refreshAll(config, false);
      }
    } catch {
      setAuthenticated(false);
      setAuthUser("");
    } finally {
      setAuthChecked(true);
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
      const session = await loginWeb(config, normalizedPhone, loginPassword);
      setLoginPassword("");
      setAuthenticated(session.authenticated);
      setAuthUser(session.login || normalizedPhone);
      await refreshAll(config, false);
    } catch (loginFailure) {
      const message = loginFailure instanceof Error ? loginFailure.message : "";
      setLoginError(loginFailureMessage(message));
    } finally {
      setLoginLoading(false);
    }
  }

  async function logout() {
    setBusyAction("logout");
    try {
      await logoutWeb(config);
    } catch {
      // Local state must still close access if the server response is interrupted.
    } finally {
      setBusyAction("");
      setAuthenticated(false);
      setAuthUser("");
      setAdminTable(null);
      setImports([]);
      setDryRuns([]);
      setReport(null);
      setSelectedOrderIds([]);
    }
  }

  function toggleOrderSelection(orderId: string) {
    setSelectedOrderIds((current) => (
      current.includes(orderId)
        ? current.filter((value) => value !== orderId)
        : [...current, orderId]
    ));
  }

  function toggleVisibleOrderSelection() {
    setSelectedOrderIds((current) => {
      const visible = new Set(visibleOrderIds);
      if (visible.size === 0) return current;
      if (visibleOrderIds.every((id) => current.includes(id))) {
        return current.filter((id) => !visible.has(id));
      }
      return Array.from(new Set([...current, ...visibleOrderIds]));
    });
  }

  async function retryGoogleQueue() {
    setBusyAction("retry-google");
    setError("");
    setNotice("");
    try {
      const result = await retryPendingGoogle(config);
      await refreshAll();
      setNotice(`Google очередь: ${String(result.status || "completed")}`);
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось повторить Google-очередь");
    } finally {
      setBusyAction("");
    }
  }

  async function syncExternalSources() {
    setBusyAction("sync-sources");
    setError("");
    setNotice("");
    try {
      const result = await syncSources(config, { skladbot: true, waitSkladbot: true });
      await refreshAll(config, false);
      const status = String(result.status || "completed");
      const skladbotStatus = String(result.skladbot?.status || "unknown");
      setNotice(`Источники обновлены: ${status}, SkladBot ${skladbotStatus}`);
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось обновить Google/SkladBot");
    } finally {
      setBusyAction("");
    }
  }

  async function rebuildDryRun(eventId: string) {
    if (!eventId) return;
    setBusyAction(`rebuild-dry-run:${eventId}`);
    setError("");
    setNotice("");
    try {
      await rebuildSkladBotDryRun(config, eventId);
      await refreshAll(config, false);
      setNotice("SkladBot dry-run пересобран");
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось пересобрать SkladBot dry-run");
    } finally {
      setBusyAction("");
    }
  }

  async function downloadAuditLog() {
    setBusyAction("audit-log");
    setError("");
    setNotice("");
    try {
      const { blob, filename } = await downloadDiagnosticsLog(config);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = decodeURIComponent(filename);
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(objectUrl);
      setNotice("Audit log скачан");
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось скачать audit log");
    } finally {
      setBusyAction("");
    }
  }

  async function runOrderAction(kind: OrderActionKind) {
    const primaryRow = selectedOrder ?? selectedRows[0];
    if (!primaryRow) return;
    if (kind !== "completeWithoutKiz" && !selectedOrder) return;
    const disabledReason = selectedActionState.disabledReason[kind];
    if (disabledReason) {
      setError(disabledReason);
      return;
    }
    const defaultReason = kind === "resync"
      ? "Ручная синхронизация из web-панели"
      : kind === "completeWithoutKiz"
        ? "Ручное закрытие выполненных заказов без сканирования КИЗов"
        : kind === "resetRescan"
          ? "Сброс заказа на пересканирование"
          : "";
    const reason = kind === "resetRescan"
      ? defaultReason
      : window.prompt(actionPrompt(kind, primaryRow, selectedOrderIds.length), defaultReason);
    if (reason === null) return;
    if (!window.confirm(actionConfirmText(kind, primaryRow, selectedOrderIds.length))) return;

    setBusyAction(kind);
    setError("");
    setNotice("");
    try {
      const payload = {
        reason: reason.trim() || defaultReason,
        actor: "web",
        idempotency_key: makeIdempotencyKey(),
      };
      if (kind === "resync") {
        await resyncGoogleOrder(config, primaryRow.order_id, payload);
      } else if (kind === "archive") {
        await archiveOrderWithoutKiz(config, primaryRow.order_id, payload);
      } else if (kind === "completeWithoutKiz") {
        await completeOrdersWithoutKiz(config, selectedOrderIds, payload);
      } else if (kind === "cancel") {
        await cancelOrder(config, primaryRow.order_id, payload);
      } else if (kind === "resetRescan") {
        await resetOrderForRescan(config, primaryRow.order_id, payload);
      } else if (kind === "restore") {
        await restoreOrder(config, primaryRow.order_id, payload);
      } else if (kind === "resyncSkladBot") {
        await resyncSkladBotOrder(config, primaryRow.order_id, payload);
      }
      setSelectedOrderIds([]);
      await refreshAll();
      setNotice(actionSuccessText(kind));
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Действие не выполнено");
      await refreshAll();
    } finally {
      setBusyAction("");
    }
  }

  if (!authChecked) {
    return <LoadingGate />;
  }

  if (!authenticated) {
    return (
      <LoginScreen
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

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/taksklad.png" alt="" />
          <div>
            <strong>TakSklad</strong>
            <span>web panel</span>
          </div>
        </div>
        <nav className="nav-tabs">
          <button className={tab === "table" ? "active" : ""} onClick={() => setTab("table")}>
            <ClipboardList size={18} />
            Таблица
          </button>
          <button className={tab === "report" ? "active" : ""} onClick={() => setTab("report")}>
            <BarChart3 size={18} />
            Отчет
          </button>
          <button className={tab === "imports" ? "active" : ""} onClick={() => setTab("imports")}>
            <FileSpreadsheet size={18} />
            Импорты
          </button>
          <button className={tab === "skladbotDryRun" ? "active" : ""} onClick={() => setTab("skladbotDryRun")}>
            <SquareCode size={18} />
            SkladBot dry-run
          </button>
          <button className={tab === "activity" ? "active" : ""} onClick={() => setTab("activity")}>
            <History size={18} />
            Активность
          </button>
        </nav>
        <div className="sidebar-status">
          <Server size={18} />
          <div>
            <span>API</span>
            <strong>{config.apiUrl ? config.apiUrl.replace(/^https?:\/\//, "") : SAME_ORIGIN_API_LABEL}</strong>
          </div>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <p>Web-панель</p>
            <h1>Заказы, синхронизация и активность</h1>
          </div>
          <div className="topbar-actions">
            <div className="user-pill" title={authUser ? `Пользователь ${maskLogin(authUser)}` : "Пользователь"}>
              <ShieldCheck size={17} />
              {authUser ? maskLogin(authUser) : "Вход выполнен"}
            </div>
            <button className="ghost-button" onClick={() => void retryGoogleQueue()} disabled={Boolean(busyAction)} title="Повторить Google-очередь">
              {busyAction === "retry-google" ? <Loader2 className="spin" size={18} /> : <Database size={18} />}
              Google очередь
            </button>
            <button className="ghost-button" onClick={() => void syncExternalSources()} disabled={Boolean(busyAction)} title="Обновить Google Sheets и SkladBot через backend">
              {busyAction === "sync-sources" ? <Loader2 className="spin" size={18} /> : <RefreshCw size={18} />}
              Google/SkladBot
            </button>
            <button className="icon-button" onClick={() => void refreshAll()} disabled={loading} title="Обновить">
              {loading ? <Loader2 className="spin" size={18} /> : <RefreshCw size={18} />}
              Обновить
            </button>
            <button className="ghost-button" onClick={() => void logout()} disabled={Boolean(busyAction)} title="Выйти">
              {busyAction === "logout" ? <Loader2 className="spin" size={18} /> : <LogOut size={18} />}
              Выйти
            </button>
          </div>
        </header>

        {(error || notice) && (
          <div className={error ? "message error" : "message success"}>
            {error ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
            <span>{error || notice}</span>
          </div>
        )}

        <section className="stats-row">
          <Metric icon={<ClipboardList size={20} />} label="Активных заказов" value={adminTable?.totals.active_orders ?? 0} />
          <Metric icon={<Box size={20} />} label="Позиции" value={adminTable?.totals.items ?? 0} />
          <Metric icon={<PackageCheck size={20} />} label="Сканировано" value={adminTable?.totals.scanned_blocks ?? 0} />
          <Metric
            icon={<Activity size={20} />}
            label="Google очередь"
            value={adminTable?.totals.pending_google_exports ?? 0}
            tone={(adminTable?.totals.pending_google_exports ?? 0) > 0 ? "warn" : undefined}
          />
        </section>

        {tab === "table" && (
          <section className="table-panel">
            <div className="panel-header table-panel-header">
              <div>
                <h2>Позиции заказов</h2>
                <span className="panel-subtitle">Показано {filteredRows.length} из {rows.length}</span>
              </div>
              <label className="search-box">
                <Search size={16} />
                <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск" />
              </label>
            </div>

            <div className="filters-bar">
              <input
                className="date-input"
                type="date"
                value={shipmentDateFilter}
                onChange={(event) => setShipmentDateFilter(event.target.value)}
                title="Дата отгрузки"
              />
              <SelectFilter value={statusFilter} onChange={(value) => setStatusFilter(value as StatusFilter)}>
                <option value="all">Все статусы</option>
                <option value="active">Активные</option>
                <option value="archive">Архив</option>
                <option value="archive_no_kiz">Архив без КИЗов</option>
                <option value="cancelled">Отменены</option>
                <option value="returned">Возвраты</option>
                <option value="removed_from_google">Удалены из Google</option>
              </SelectFilter>
              <SelectFilter value={scanFilter} onChange={(value) => setScanFilter(value as ScanFilter)}>
                <option value="all">Все сканы</option>
                <option value="not_started">Не начато</option>
                <option value="in_progress">В работе</option>
                <option value="completed">Готово</option>
                <option value="over_scanned">Перескан</option>
                <option value="no_plan">Нет плана</option>
              </SelectFilter>
              <SelectFilter value={skladbotFilter} onChange={(value) => setSkladbotFilter(value as SkladBotFilter)}>
                <option value="all">SkladBot: все</option>
                <option value="found">Найдено</option>
                <option value="missing">Без номера</option>
                <option value="problem">Проблема</option>
              </SelectFilter>
              <SelectFilter value={googleFilter} onChange={(value) => setGoogleFilter(value as GoogleFilter)}>
                <option value="all">Google: все</option>
                <option value="synced">Синхронизировано</option>
                <option value="pending">Очередь</option>
                <option value="removed_from_google">Удалено</option>
                <option value="unknown">Неизвестно</option>
              </SelectFilter>
              <button
                className="ghost-button"
                onClick={toggleVisibleOrderSelection}
                disabled={visibleOrderIds.length === 0}
                title="Выбрать все заказы, которые сейчас видны после фильтров"
              >
                <ClipboardList size={16} />
                {allVisibleSelected ? "Снять видимые" : "Выделить все"}
              </button>
              {shipmentDateFilter && (
                <button className="ghost-button" onClick={() => setShipmentDateFilter("")}>
                  Сбросить дату
                </button>
              )}
            </div>

            {selectedOrderIds.length > 0 && (
              <ActionBar
                selectedRows={selectedRows}
                state={selectedActionState}
                busyAction={busyAction}
                onClear={() => setSelectedOrderIds([])}
                onResync={() => void runOrderAction("resync")}
                onResetRescan={() => void runOrderAction("resetRescan")}
                onCompleteWithoutKiz={() => void runOrderAction("completeWithoutKiz")}
                onArchive={() => void runOrderAction("archive")}
                onCancel={() => void runOrderAction("cancel")}
                onRestore={() => void runOrderAction("restore")}
                onResyncSkladBot={() => void runOrderAction("resyncSkladBot")}
              />
            )}

            <AdminRowsTable
              rows={filteredRows}
              selectedOrderIds={selectedOrderIds}
              allVisibleSelected={allVisibleSelected}
              onToggleVisible={toggleVisibleOrderSelection}
              onToggleOrder={toggleOrderSelection}
            />
          </section>
        )}

        {tab === "report" && (
          <section className="table-panel">
            <div className="panel-header">
              <h2>Дневной отчет</h2>
              <input
                className="date-input"
                type="date"
                value={reportDate}
                onChange={(event) => setReportDate(event.target.value)}
                onBlur={() => void refreshAll()}
              />
            </div>
            {report && (
              <>
                <section className="stats-row compact">
                  <Metric icon={<ClipboardList size={20} />} label="Заказов" value={report.totals.orders} />
                  <Metric icon={<PackageCheck size={20} />} label="Готово" value={report.totals.completed_orders} />
                  <Metric icon={<Box size={20} />} label="Сканов сегодня" value={report.totals.scanned_today} />
                  <Metric icon={<Activity size={20} />} label="Осталось" value={report.totals.remaining_blocks} tone="warn" />
                </section>
                <DataTable
                  headers={["Клиент", "Оплата", "Заявка", "Блоки", "Осталось"]}
                  rows={report.orders.map((order) => [
                    order.client,
                    order.payment_type,
                    order.skladbot_request_number || "-",
                    `${order.scanned_blocks}/${order.planned_blocks}`,
                    String(order.remaining_blocks),
                  ])}
                />
              </>
            )}
          </section>
        )}

        {tab === "imports" && (
          <section className="table-panel">
            <div className="panel-header">
              <h2>История импортов</h2>
            </div>
            <DataTable
              headers={["Дата", "Источник", "Статус", "Строк", "Импортировано", "SkladBot dry-run"]}
              rows={imports.map((item) => [
                new Date(item.created_at).toLocaleString("ru-RU"),
                item.source,
                item.status,
                String(item.rows_total),
                String(item.rows_imported),
                importDryRunSummaryText(item),
              ])}
            />
          </section>
        )}

        {tab === "skladbotDryRun" && (
          <SkladBotDryRunPanel
            dryRuns={dryRuns}
            imports={imports}
            busyAction={busyAction}
            onRebuild={(eventId) => void rebuildDryRun(eventId)}
          />
        )}

        {tab === "activity" && (
          <section className="table-panel">
            <div className="panel-header">
              <h2>Последняя активность</h2>
              <button className="ghost-button" onClick={() => void downloadAuditLog()} disabled={Boolean(busyAction)} title="Скачать backend diagnostics log с audit-событиями">
                {busyAction === "audit-log" ? <Loader2 className="spin" size={16} /> : <History size={16} />}
                Audit log
              </button>
            </div>
            <ActivityList items={adminTable?.recent_activity ?? []} />
          </section>
        )}
      </main>
    </div>
  );
}

function LoadingGate() {
  return (
    <div className="login-shell loading-gate">
      <div className="loading-mark">
        <Loader2 className="spin" size={24} />
        <span>Загружаем доступ...</span>
      </div>
    </div>
  );
}

function LoginScreen({
  phone,
  password,
  error,
  loading,
  onPhoneChange,
  onPasswordChange,
  onSubmit,
}: {
  phone: string;
  password: string;
  error: string;
  loading: boolean;
  onPhoneChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
}) {
  return (
    <main className="login-shell">
      <section className="login-brand">
        <div className="login-logo-row">
          <img src="/taksklad.png" alt="" />
          <div>
            <strong>TakSklad</strong>
            <span>Складская web-панель</span>
          </div>
        </div>
        <div className="login-copy">
          <p>Операционный контур склада</p>
          <h1>Доступ к заказам, синхронизации и журналу действий</h1>
        </div>
        <div className="login-status-grid">
          <span><Database size={16} /> Google Sheets</span>
          <span><Server size={16} /> VDS backend</span>
          <span><PackageCheck size={16} /> SkladBot</span>
          <span><History size={16} /> Audit log</span>
        </div>
      </section>

      <section className="login-panel" aria-label="Вход в панель">
        <div className="login-panel-head">
          <Lock size={22} />
          <div>
            <h2>Вход в панель</h2>
            <span>Используйте рабочий телефон и пароль.</span>
          </div>
        </div>
        <form className="login-form" onSubmit={onSubmit}>
          <label>
            <span>Телефон</span>
            <div className="login-input">
              <Phone size={18} />
              <input
                inputMode="tel"
                autoComplete="username"
                value={phone}
                onChange={(event) => onPhoneChange(event.target.value)}
                placeholder="+998 XX XXX XX XX"
              />
            </div>
          </label>
          <label>
            <span>Пароль</span>
            <div className="login-input">
              <KeyRound size={18} />
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => onPasswordChange(event.target.value)}
                placeholder="Введите пароль"
              />
            </div>
          </label>
          {error && (
            <div className="login-error">
              <AlertCircle size={17} />
              {error}
            </div>
          )}
          <button className="login-submit" type="submit" disabled={loading || !phone.trim() || !password}>
            {loading ? <Loader2 className="spin" size={18} /> : <ShieldCheck size={18} />}
            {loading ? "Проверяем доступ..." : "Войти"}
          </button>
        </form>
        <p className="login-footnote">Нет доступа? Обратитесь к администратору склада.</p>
      </section>
    </main>
  );
}

function buildActionState(selectedOrderIds: string[], selectedRows: AdminTableRow[]): ActionState {
  const disabledReason: Record<OrderActionKind, string> = {
    resync: "",
    archive: "",
    completeWithoutKiz: "",
    cancel: "",
    resetRescan: "",
    restore: "",
    resyncSkladBot: "",
  };
  const selectedCount = selectedOrderIds.length;
  const plannedBlocks = selectedRows.reduce((sum, row) => sum + row.quantity_blocks, 0);
  const scannedBlocks = selectedRows.reduce((sum, row) => sum + row.scanned_blocks, 0);
  const scanCodes = selectedRows.reduce((sum, row) => sum + row.scan_codes_count, 0);
  const pendingGoogleExports = Math.max(0, ...selectedRows.map((row) => row.pending_google_exports));

  if (selectedCount === 0) {
    const reason = "Выберите заказ";
    return {
      selectedCount,
      disabledReason: actionDisabledReasons(reason),
      plannedBlocks,
      scannedBlocks,
      pendingGoogleExports,
    };
  }
  if (selectedRows.length === 0) {
    const reason = "Заказ не найден в текущих данных";
    return {
      selectedCount,
      disabledReason: actionDisabledReasons(reason),
      plannedBlocks,
      scannedBlocks,
      pendingGoogleExports,
    };
  }
  if (pendingGoogleExports > 0) {
    disabledReason.resync = "Сначала обработайте Google очередь";
    disabledReason.archive = "Сначала обработайте Google очередь";
    disabledReason.completeWithoutKiz = "Сначала обработайте Google очередь";
    disabledReason.cancel = "Сначала обработайте Google очередь";
    disabledReason.resetRescan = "Сначала обработайте Google очередь";
    disabledReason.restore = "Сначала обработайте Google очередь";
    disabledReason.resyncSkladBot = "Сначала обработайте Google очередь";
  }
  const allActive = selectedRows.every((row) => row.status_bucket === "active");
  if (!allActive) {
    disabledReason.archive = "Доступно только для активного заказа";
    disabledReason.completeWithoutKiz = "Доступно только для активных заказов";
    disabledReason.cancel = "Доступно только для активного заказа";
    disabledReason.resyncSkladBot = "Доступно только для активного заказа";
  }
  if (selectedCount > 1) {
    const reason = "Выберите один заказ";
    disabledReason.resync = reason;
    disabledReason.archive = reason;
    disabledReason.cancel = reason;
    disabledReason.resetRescan = reason;
    disabledReason.restore = reason;
    disabledReason.resyncSkladBot = reason;
  }
  if (selectedRows.some((row) => row.status_bucket === "returned")) {
    disabledReason.resetRescan = "Возвраты нельзя сбрасывать на пересканирование";
  }
  const canRestore = selectedRows.every((row) => row.status_bucket === "archive_no_kiz" || row.status_bucket === "cancelled");
  if (!canRestore) {
    disabledReason.restore = "Доступно только для отмененных заказов или архива без КИЗов";
  }
  const hasPartiallyScannedRows = selectedRows.some(
    (row) => (row.scanned_blocks > 0 || row.scan_codes_count > 0) && row.scanned_blocks < row.quantity_blocks,
  );
  if (scannedBlocks > 0 || scanCodes > 0) {
    disabledReason.archive = "В заказе уже есть отсканированные КИЗы";
    disabledReason.cancel = "В заказе уже есть отсканированные КИЗы";
  }
  if (hasPartiallyScannedRows) {
    disabledReason.completeWithoutKiz = "Есть частично отсканированные позиции";
  }
  if (selectedRows.some((row) => row.status_bucket === "removed_from_google")) {
    disabledReason.resync = "Заказ удален из Google";
  }

  return { selectedCount, disabledReason, plannedBlocks, scannedBlocks, pendingGoogleExports };
}

function actionDisabledReasons(reason: string): Record<OrderActionKind, string> {
  return {
    resync: reason,
    archive: reason,
    completeWithoutKiz: reason,
    cancel: reason,
    resetRescan: reason,
    restore: reason,
    resyncSkladBot: reason,
  };
}

function ActionBar({
  selectedRows,
  state,
  busyAction,
  onClear,
  onResync,
  onResetRescan,
  onCompleteWithoutKiz,
  onArchive,
  onCancel,
  onRestore,
  onResyncSkladBot,
}: {
  selectedRows: AdminTableRow[];
  state: ActionState;
  busyAction: string;
  onClear: () => void;
  onResync: () => void;
  onResetRescan: () => void;
  onCompleteWithoutKiz: () => void;
  onArchive: () => void;
  onCancel: () => void;
  onRestore: () => void;
  onResyncSkladBot: () => void;
}) {
  const firstRow = selectedRows[0];
  const isBusy = Boolean(busyAction);

  return (
    <div className="action-bar">
      <div>
        <strong>{firstRow ? firstRow.client : "Выбран заказ"}</strong>
        <span>
          {state.selectedCount === 1
            ? `${selectedRows.length} поз., ${state.scannedBlocks}/${state.plannedBlocks} блоков`
            : `${state.selectedCount} заказов`}
          {state.pendingGoogleExports > 0 ? `, Google очередь ${state.pendingGoogleExports}` : ""}
        </span>
      </div>
      <div className="action-buttons">
        <button
          className="ghost-button"
          onClick={onResync}
          disabled={isBusy || Boolean(state.disabledReason.resync)}
          title={state.disabledReason.resync || "Повторно записать заказ в Google"}
        >
          {busyAction === "resync" ? <Loader2 className="spin" size={16} /> : <Database size={16} />}
          Ресинк Google
        </button>
        <button
          className="ghost-button"
          onClick={onResetRescan}
          disabled={isBusy || Boolean(state.disabledReason.resetRescan)}
          title={state.disabledReason.resetRescan || "Сбросить сканы и вернуть заказ на пересканирование"}
        >
          {busyAction === "resetRescan" ? <Loader2 className="spin" size={16} /> : <RotateCcw size={16} />}
          Reset/rescan
        </button>
        <button
          className="ghost-button"
          onClick={onCompleteWithoutKiz}
          disabled={isBusy || Boolean(state.disabledReason.completeWithoutKiz)}
          title={state.disabledReason.completeWithoutKiz || "Закрыть выбранные активные заказы как выполненные без сканирования КИЗов"}
        >
          {busyAction === "completeWithoutKiz" ? <Loader2 className="spin" size={16} /> : <PackageCheck size={16} />}
          В архив как выполнено
        </button>
        <button
          className="ghost-button"
          onClick={onArchive}
          disabled={isBusy || Boolean(state.disabledReason.archive)}
          title={state.disabledReason.archive || "Перенести активный заказ без КИЗов"}
        >
          {busyAction === "archive" ? <Loader2 className="spin" size={16} /> : <PackageCheck size={16} />}
          В архив без КИЗов
        </button>
        <button
          className="ghost-button danger-button"
          onClick={onCancel}
          disabled={isBusy || Boolean(state.disabledReason.cancel)}
          title={state.disabledReason.cancel || "Отменить активный заказ без КИЗов"}
        >
          {busyAction === "cancel" ? <Loader2 className="spin" size={16} /> : <AlertCircle size={16} />}
          Отменить
        </button>
        <button
          className="ghost-button"
          onClick={onRestore}
          disabled={isBusy || Boolean(state.disabledReason.restore)}
          title={state.disabledReason.restore || "Восстановить заказ в активные"}
        >
          {busyAction === "restore" ? <Loader2 className="spin" size={16} /> : <Undo2 size={16} />}
          Восстановить
        </button>
        <button
          className="ghost-button"
          onClick={onResyncSkladBot}
          disabled={isBusy || Boolean(state.disabledReason.resyncSkladBot)}
          title={state.disabledReason.resyncSkladBot || "Повторно подтянуть номер заявки SkladBot"}
        >
          {busyAction === "resyncSkladBot" ? <Loader2 className="spin" size={16} /> : <Server size={16} />}
          SkladBot заказ
        </button>
        <button className="ghost-button quiet-button" onClick={onClear} disabled={isBusy}>
          Снять выбор
        </button>
      </div>
    </div>
  );
}

function Metric({
  icon,
  label,
  value,
  tone,
}: {
  icon: ReactNode;
  label: string;
  value: number | string;
  tone?: "warn";
}) {
  return (
    <div className={`metric ${tone === "warn" ? "warn" : ""}`}>
      {icon}
      <span>{label}</span>
      <strong>{typeof value === "number" ? formatNumber(value) : value}</strong>
    </div>
  );
}

function SelectFilter({
  value,
  onChange,
  children,
}: {
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
}) {
  return (
    <select className="filter-select" value={value} onChange={(event) => onChange(event.target.value)}>
      {children}
    </select>
  );
}

function AdminRowsTable({
  rows,
  selectedOrderIds,
  allVisibleSelected,
  onToggleVisible,
  onToggleOrder,
}: {
  rows: AdminTableRow[];
  selectedOrderIds: string[];
  allVisibleSelected: boolean;
  onToggleVisible: () => void;
  onToggleOrder: (orderId: string) => void;
}) {
  return (
    <div className="data-table-wrap admin-table-wrap">
      <table className="data-table admin-table">
        <colgroup>
          <col className="select-col" />
          <col className="date-col" />
          <col className="client-col" />
          <col className="product-col" />
          <col className="blocks-col" />
          <col className="status-col" />
          <col className="skladbot-col" />
          <col className="google-col" />
          <col className="money-col" />
        </colgroup>
        <thead>
          <tr>
            <th className="selection-cell">
              <input
                type="checkbox"
                checked={allVisibleSelected}
                onChange={onToggleVisible}
                disabled={rows.length === 0}
                aria-label="Выбрать видимые заказы"
              />
            </th>
            <th>Дата</th>
            <th>Клиент</th>
            <th>Товар</th>
            <th>Блоки</th>
            <th>Статус</th>
            <th>SkladBot</th>
            <th>Google</th>
            <th>Сумма</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const selected = selectedOrderIds.includes(row.order_id);
            return (
              <tr key={row.item_id} className={selected ? "selected-row" : ""}>
                <td className="selection-cell">
                  <input
                    type="checkbox"
                    checked={selected}
                    onChange={() => onToggleOrder(row.order_id)}
                    aria-label={`Выбрать заказ ${row.client}`}
                  />
                </td>
                <td className="date-cell">
                  <strong className="cell-title">{formatDate(row.order_date)}</strong>
                  <span className="table-muted">{row.payment_type}</span>
                </td>
                <td className="client-cell">
                  <strong className="cell-title">{row.client}</strong>
                  <span className="table-muted cell-sub">{row.address}</span>
                  {row.representative && <span className="table-muted cell-sub">{row.representative}</span>}
                </td>
                <td className="product-cell">
                  <strong className="cell-title">{row.product}</strong>
                  <span className="table-muted cell-sub">{row.source_file || "-"}</span>
                </td>
                <td className="blocks-cell">
                  <strong>{row.scanned_blocks}/{row.quantity_blocks}</strong>
                  <span className="table-muted">осталось {row.remaining_blocks}</span>
                  <div className="progress-track">
                    <i style={{ width: `${progressPercent(row)}%` }} />
                  </div>
                </td>
                <td>
                  <span className={`status-badge ${row.status_bucket}`}>{statusBucketLabel(row.status_bucket)}</span>
                  <span className={`activity-badge ${scanState(row)}`}>{scanStateLabel(scanState(row))}</span>
                </td>
                <td>
                  <strong className="cell-title">{row.skladbot_request_number || "-"}</strong>
                  <span className="table-muted cell-sub">{skladbotStatusLabel(row)}</span>
                </td>
                <td>
                  <span className={`status-badge google-${row.google_sheet_status}`}>
                    {googleStatusLabel(row.google_sheet_status)}
                  </span>
                  {row.pending_google_exports > 0 && (
                    <span className="table-muted">в очереди {row.pending_google_exports}</span>
                  )}
                </td>
                <td className="numeric-cell">{formatMoney(row.line_total)}</td>
              </tr>
            );
          })}
          {rows.length === 0 && (
            <tr>
              <td colSpan={9}>Нет данных</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function SkladBotDryRunPanel({
  dryRuns,
  imports,
  busyAction,
  onRebuild,
}: {
  dryRuns: SkladBotDryRun[];
  imports: ImportRecord[];
  busyAction: string;
  onRebuild: (eventId: string) => void;
}) {
  const [importFilter, setImportFilter] = useState("");
  const filteredRuns = useMemo(
    () => dryRuns.filter((item) => !importFilter || item.import_id === importFilter),
    [dryRuns, importFilter],
  );
  const summary = useMemo(() => ({
    ready: filteredRuns.filter((item) => item.status === "ready").length,
    blocked: filteredRuns.filter((item) => item.status === "blocked").length,
    alreadyLinked: filteredRuns.filter((item) => item.status === "already_linked").length,
  }), [filteredRuns]);
  const importsById = useMemo(
    () => new Map(imports.map((item) => [item.id, item])),
    [imports],
  );

  return (
    <section className="table-panel">
      <div className="panel-header table-panel-header">
        <div>
          <h2>SkladBot dry-run</h2>
          <span className="panel-subtitle">Будущие заявки без отправки в SkladBot</span>
        </div>
        <SelectFilter value={importFilter} onChange={setImportFilter}>
          <option value="">Все импорты</option>
          {imports.map((item) => (
            <option key={item.id} value={item.id}>
              {new Date(item.created_at).toLocaleString("ru-RU")} · {shortId(item.id)}
            </option>
          ))}
        </SelectFilter>
      </div>

      <section className="stats-row compact">
        <Metric icon={<SquareCode size={20} />} label="Ready" value={summary.ready} />
        <Metric icon={<AlertCircle size={20} />} label="Blocked" value={summary.blocked} tone={summary.blocked > 0 ? "warn" : undefined} />
        <Metric icon={<Server size={20} />} label="Уже WH-R" value={summary.alreadyLinked} />
        <Metric icon={<ClipboardList size={20} />} label="Всего" value={filteredRuns.length} />
      </section>

      <div className="data-table-wrap dry-run-table-wrap">
        <table className="data-table dry-run-table">
          <thead>
            <tr>
              <th>Импорт</th>
              <th>Клиент</th>
              <th>Дата/оплата</th>
              <th>Адрес</th>
              <th>Товары</th>
              <th>Статус</th>
              <th>JSON</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filteredRuns.map((item) => {
              const importRecord = importsById.get(item.import_id);
              const rebuildAction = `rebuild-dry-run:${item.event_id}`;
              return (
                <tr key={item.id}>
                  <td>
                    <strong className="cell-title">{shortId(item.import_id)}</strong>
                    <span className="table-muted cell-sub">
                      {importRecord ? new Date(importRecord.created_at).toLocaleString("ru-RU") : formatDateTime(item.generated_at)}
                    </span>
                  </td>
                  <td>
                    <strong className="cell-title">{item.client}</strong>
                    <span className="table-muted cell-sub">{shortId(item.order_id)}</span>
                  </td>
                  <td>
                    <strong className="cell-title">{formatDate(item.order_date)}</strong>
                    <span className="table-muted cell-sub">{item.payment_type}</span>
                  </td>
                  <td className="dry-run-address">{item.address || "-"}</td>
                  <td>
                    <strong className="cell-title">{item.blocks} блок.</strong>
                    <div className="dry-run-products">
                      {item.products.map((product, index) => (
                        <span key={`${item.id}-${product.product}-${index}`} className={product.status === "blocked" ? "blocked" : ""}>
                          {product.product}: {product.quantity_blocks}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td>
                    <span className={`status-badge dry-run-${item.status}`}>{dryRunStatusLabel(item.status)}</span>
                    {item.error && <span className="table-muted cell-sub">{item.error}</span>}
                  </td>
                  <td>
                    <details className="json-preview">
                      <summary>JSON</summary>
                      <pre>{JSON.stringify(item.payload, null, 2)}</pre>
                    </details>
                  </td>
                  <td>
                    <button className="ghost-button" onClick={() => onRebuild(item.event_id)} disabled={Boolean(busyAction)}>
                      {busyAction === rebuildAction ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
                      Пересобрать
                    </button>
                  </td>
                </tr>
              );
            })}
            {filteredRuns.length === 0 && (
              <tr>
                <td colSpan={8}>Dry-run еще не создан</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ActivityList({ items }: { items: AdminActivity[] }) {
  return (
    <div className="activity-list">
      {items.map((item) => (
        <div className="activity-row" key={item.id}>
          <Database size={17} />
          <div>
            <strong>{item.action}</strong>
            <span>{[item.entity_type, shortId(item.entity_id)].filter(Boolean).join(" / ") || "-"}</span>
          </div>
          <time>{formatDateTime(item.created_at)}</time>
        </div>
      ))}
      {items.length === 0 && <div className="empty-state">Активности нет</div>}
    </div>
  );
}

function DataTable({ headers, rows }: { headers: string[]; rows: string[][] }) {
  return (
    <div className="data-table-wrap">
      <table className="data-table">
        <thead>
          <tr>{headers.map((header) => <th key={header}>{header}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.join("-")}-${index}`}>
              {row.map((cell, cellIndex) => <td key={`${cell}-${cellIndex}`}>{cell}</td>)}
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={headers.length}>Нет данных</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function matchesSkladBotFilter(row: AdminTableRow, filter: SkladBotFilter) {
  const hasNumber = Boolean(row.skladbot_request_number || row.skladbot_request_id);
  if (filter === "all") return true;
  if (filter === "found") return hasNumber;
  if (filter === "missing") return !hasNumber;
  return ["not_found", "multiple", "error", "pending"].includes(row.skladbot_status);
}

function scanState(row: AdminTableRow): ScanFilter {
  if (row.quantity_blocks <= 0) return "no_plan";
  if (row.scanned_blocks > row.quantity_blocks) return "over_scanned";
  if (row.scanned_blocks >= row.quantity_blocks) return "completed";
  if (row.scanned_blocks > 0) return "in_progress";
  return "not_started";
}

function progressPercent(row: AdminTableRow) {
  if (row.quantity_blocks <= 0) return 0;
  return Math.min(100, Math.round((row.scanned_blocks / row.quantity_blocks) * 100));
}

function statusBucketLabel(value: string) {
  if (value === "active") return "Активно";
  if (value === "archive") return "Архив";
  if (value === "archive_no_kiz") return "Архив без КИЗов";
  if (value === "cancelled") return "Отменено";
  if (value === "returned") return "Возврат";
  if (value === "removed_from_google") return "Удалено";
  return value || "-";
}

function actionPrompt(kind: OrderActionKind, row: AdminTableRow, selectedCount = 1) {
  if (kind === "resync") return `Причина ресинка Google для заказа ${row.client}`;
  if (kind === "resetRescan") return `Причина сброса заказа ${row.client} на пересканирование`;
  if (kind === "completeWithoutKiz") return selectedCount > 1
    ? `Причина закрытия ${selectedCount} заказов как выполненных без КИЗов`
    : `Причина закрытия заказа ${row.client} как выполненного без КИЗов`;
  if (kind === "archive") return `Причина переноса без КИЗов для заказа ${row.client}`;
  if (kind === "restore") return `Причина восстановления заказа ${row.client}`;
  if (kind === "resyncSkladBot") return `Причина повторной проверки SkladBot для заказа ${row.client}`;
  return `Причина отмены заказа ${row.client}`;
}

function actionConfirmText(kind: OrderActionKind, row: AdminTableRow, selectedCount = 1) {
  if (kind === "resync") return `Повторно синхронизировать заказ ${row.client} с Google?`;
  if (kind === "resetRescan") return `Сбросить все КИЗы заказа ${row.client} и вернуть его на пересканирование?`;
  if (kind === "completeWithoutKiz") return selectedCount > 1
    ? `Закрыть ${selectedCount} выбранных заказов как выполненные и перенести их в архив?`
    : `Закрыть заказ ${row.client} как выполненный и перенести его в архив?`;
  if (kind === "archive") return `Перенести заказ ${row.client} в архив без КИЗов?`;
  if (kind === "restore") return `Восстановить заказ ${row.client} в активные?`;
  if (kind === "resyncSkladBot") return `Повторно подтянуть SkladBot номер для заказа ${row.client}?`;
  return `Отменить заказ ${row.client}?`;
}

function actionSuccessText(kind: OrderActionKind) {
  if (kind === "resync") return "Ресинк Google запущен";
  if (kind === "resetRescan") return "Заказ сброшен на пересканирование";
  if (kind === "completeWithoutKiz") return "Выбранные заказы закрыты как выполненные и отправлены в архив";
  if (kind === "archive") return "Заказ перенесен в архив без КИЗов";
  if (kind === "restore") return "Заказ восстановлен";
  if (kind === "resyncSkladBot") return "SkladBot проверка запущена";
  return "Заказ отменен";
}

function makeIdempotencyKey() {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function scanStateLabel(value: ScanFilter) {
  if (value === "not_started") return "Не начато";
  if (value === "in_progress") return "В работе";
  if (value === "completed") return "Готово";
  if (value === "over_scanned") return "Перескан";
  if (value === "no_plan") return "Нет плана";
  return "Все";
}

function skladbotStatusLabel(row: AdminTableRow) {
  if (row.skladbot_status === "found" || row.skladbot_request_number || row.skladbot_request_id) return "Найдено";
  if (row.skladbot_status === "not_found") return "Не найдено";
  if (row.skladbot_status === "multiple") return "Несколько";
  if (row.skladbot_status === "pending") return "Проверяется";
  if (row.skladbot_status === "error") return "Ошибка";
  return "Без номера";
}

function dryRunStatusLabel(value: string) {
  if (value === "ready") return "Ready";
  if (value === "blocked") return "Заблокировано";
  if (value === "already_linked") return "Уже есть WH-R";
  return value || "-";
}

function importDryRunSummaryText(item: ImportRecord) {
  const summary = readImportDryRunSummary(item);
  if (!summary) return "Не создан";
  const ready = Number(summary.ready ?? 0);
  const blocked = Number(summary.blocked ?? 0);
  const alreadyLinked = Number(summary.already_linked ?? 0);
  const mode = typeof summary.mode === "string" ? summary.mode : "dry_run";
  return `${mode}: ready ${ready}, blocked ${blocked}, WH-R ${alreadyLinked}`;
}

function readImportDryRunSummary(item: ImportRecord): Record<string, unknown> | null {
  const summary = item.raw_payload?.skladbot_dry_run;
  return summary && typeof summary === "object" && !Array.isArray(summary)
    ? summary as Record<string, unknown>
    : null;
}

function googleStatusLabel(value: string) {
  if (value === "synced") return "Синхр.";
  if (value === "pending") return "Очередь";
  if (value === "removed_from_google") return "Удалено";
  if (value === "unknown") return "Неизвестно";
  return value || "-";
}

function formatDate(value: string | null) {
  if (!value) return "-";
  const [year, month, day] = value.split("-");
  return year && month && day ? `${day}.${month}.${year}` : value;
}

function formatDateTime(value: string | null) {
  if (!value) return "-";
  return new Date(value).toLocaleString("ru-RU");
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("ru-RU").format(value);
}

function formatMoney(value: number) {
  return value ? formatNumber(value) : "-";
}

function shortId(value: string) {
  return value.length > 8 ? value.slice(0, 8) : value;
}

function maskLogin(value: string) {
  const digits = value.replace(/\D/g, "");
  if (digits.length <= 4) return value;
  return `+${digits.slice(0, 3)} ... ${digits.slice(-4)}`;
}

function loginFailureMessage(message: string) {
  if (message.includes("401")) {
    return "Телефон или пароль не подходят";
  }
  if (message.includes("429")) {
    return "Слишком много попыток. Попробуйте позже.";
  }
  if (message.includes("503")) {
    return "Вход пока не настроен на сервере.";
  }
  if (message.includes("500") || message.includes("502") || message.includes("504")) {
    return "Сайт не может подключиться к backend. Обновите страницу или попробуйте позже.";
  }
  return "Не удалось выполнить вход. Проверьте связь и попробуйте ещё раз.";
}

export default App;

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(<App />);
}
