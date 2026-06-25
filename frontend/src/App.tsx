import {
  Activity,
  AlertCircle,
  BarChart3,
  Building2,
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
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Server,
  ShieldCheck,
  SquareCode,
  Trash2,
  Undo2,
} from "lucide-react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { Fragment, FormEvent, useEffect, useMemo, useState } from "react";
import {
  AdminIncident,
  AdminActivity,
  AdminIncidentsResponse,
  AdminTable,
  AdminTableRow,
  ApiConfig,
  ApiRequestError,
  ClientPoint,
  ClientPointOrderSummary,
  DashboardDaySummary,
  DayReport,
  EventQueueDiagnostics,
  EventQueueEvent,
  ImportRecord,
  ReadinessResponse,
  SkladBotDryRun,
  archiveOrderWithoutKiz,
  cancelOrder,
  completeOrdersWithoutKiz,
  defaultApiUrl,
  deleteActiveOrder,
  downloadDiagnosticsLog,
  getAdminEvents,
  getAdminIncidents,
  getAdminTable,
  getAuthSession,
  getClientPointOrderSummary,
  getDashboardDaySummary,
  getDayReport,
  getReadiness,
  listClientPoints,
  listImports,
  listSkladBotDryRuns,
  loginWeb,
  logoutWeb,
  rebuildSkladBotDryRun,
  resetOrderForRescan,
  restoreOrder,
  resyncGoogleOrder,
  resyncSkladBotOrder,
  retryAdminEvent,
  retryPendingGoogle,
  syncSources,
  updateClientPointTimeslot,
  updateIncidentStatus,
} from "./api";
import "./styles.css";

type Tab = "table" | "clients" | "report" | "imports" | "skladbotDryRun" | "incidents" | "activity";
type StatusFilter = "all" | "active" | "archive" | "archive_no_kiz" | "cancelled" | "returned" | "removed_from_google";
type ScanFilter = "all" | "not_started" | "in_progress" | "completed" | "over_scanned" | "no_plan";
type SkladBotFilter = "all" | "found" | "missing" | "problem";
type GoogleFilter = "all" | "synced" | "pending" | "removed_from_google" | "unknown";
type ClientTimeslotFilter = "all" | "custom" | "default";
type IncidentStatusFilter = "all" | "open" | "in_progress" | "manual_review" | "resolved" | "ignored" | "cancelled";
type IncidentSeverityFilter = "all" | "info" | "warning" | "critical";
type OrderActionKind = "resync" | "archive" | "completeWithoutKiz" | "cancel" | "deleteActive" | "resetRescan" | "restore" | "resyncSkladBot";
type ClientPointFormDraft = {
  clientName: string;
  address: string;
  coordinates: string;
  representative: string;
  deliveryFrom: string;
  deliveryTo: string;
};
type ActionState = {
  selectedCount: number;
  disabledReason: Record<OrderActionKind, string>;
  plannedBlocks: number;
  scannedBlocks: number;
  pendingGoogleExports: number;
};

const SAME_ORIGIN_API_LABEL = "same-origin /api";

function defaultClientPointDraft(): ClientPointFormDraft {
  return {
    clientName: "",
    address: "",
    coordinates: "",
    representative: "",
    deliveryFrom: "10:00",
    deliveryTo: "18:00",
  };
}

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
  const [clientPoints, setClientPoints] = useState<ClientPoint[]>([]);
  const [readiness, setReadiness] = useState<ReadinessResponse | null>(null);
  const [eventQueue, setEventQueue] = useState<EventQueueDiagnostics | null>(null);
  const [incidents, setIncidents] = useState<AdminIncident[]>([]);
  const [incidentSummary, setIncidentSummary] = useState<Record<string, unknown>>({});
  const [report, setReport] = useState<DayReport | null>(null);
  const [dashboardSummary, setDashboardSummary] = useState<DashboardDaySummary | null>(null);
  const [reportDate, setReportDate] = useState(todayIso());
  const [shipmentDateFilter, setShipmentDateFilter] = useState("");
  const [search, setSearch] = useState("");
  const [clientSearch, setClientSearch] = useState("");
  const [incidentSearch, setIncidentSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [scanFilter, setScanFilter] = useState<ScanFilter>("all");
  const [skladbotFilter, setSkladbotFilter] = useState<SkladBotFilter>("all");
  const [googleFilter, setGoogleFilter] = useState<GoogleFilter>("all");
  const [clientTimeslotFilter, setClientTimeslotFilter] = useState<ClientTimeslotFilter>("all");
  const [incidentStatusFilter, setIncidentStatusFilter] = useState<IncidentStatusFilter>("all");
  const [incidentSeverityFilter, setIncidentSeverityFilter] = useState<IncidentSeverityFilter>("all");
  const [incidentSourceFilter, setIncidentSourceFilter] = useState("all");
  const [tab, setTab] = useState<Tab>("table");
  const [selectedOrderIds, setSelectedOrderIds] = useState<string[]>([]);
  const [selectedIncidentId, setSelectedIncidentId] = useState("");
  const [selectedEventId, setSelectedEventId] = useState("");
  const [editingClientPointId, setEditingClientPointId] = useState("");
  const [expandedClientPointId, setExpandedClientPointId] = useState("");
  const [clientOrderSummaries, setClientOrderSummaries] = useState<Record<string, ClientPointOrderSummary>>({});
  const [clientOrderSummaryLoadingId, setClientOrderSummaryLoadingId] = useState("");
  const [clientSlotDraft, setClientSlotDraft] = useState({ deliveryFrom: "", deliveryTo: "" });
  const [clientPointCreateOpen, setClientPointCreateOpen] = useState(false);
  const [newClientPointDraft, setNewClientPointDraft] = useState<ClientPointFormDraft>(() => defaultClientPointDraft());
  const [adminActionReason, setAdminActionReason] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [loading, setLoading] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [authUser, setAuthUser] = useState("");
  const [authRole, setAuthRole] = useState("");
  const [authPermissions, setAuthPermissions] = useState<string[]>([]);
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
  const filteredIncidents = useMemo(
    () => filterIncidents(incidents, {
      status: incidentStatusFilter,
      severity: incidentSeverityFilter,
      source: incidentSourceFilter,
      search: incidentSearch,
    }),
    [incidents, incidentStatusFilter, incidentSeverityFilter, incidentSourceFilter, incidentSearch],
  );
  const filteredClientPoints = useMemo(
    () => filterClientPoints(clientPoints, clientSearch, clientTimeslotFilter),
    [clientPoints, clientSearch, clientTimeslotFilter],
  );
  const clientPointSummary = useMemo(
    () => ({
      total: clientPoints.length,
      saved: clientPoints.filter((point) => point.is_saved).length,
      custom: clientPoints.filter((point) => point.has_custom_timeslot).length,
      default: clientPoints.filter((point) => !point.has_custom_timeslot).length,
    }),
    [clientPoints],
  );
  const sourceOptions = useMemo(
    () => Array.from(new Set(incidents.map((item) => item.source).filter(Boolean))).sort(),
    [incidents],
  );
  const actionableEvents = useMemo(
    () => (eventQueue?.recent_events ?? [])
      .filter((event) => ["failed", "pending", "processing", "blocked"].includes(event.status)),
    [eventQueue],
  );
  const selectedIncident = useMemo(
    () => incidents.find((item) => item.id === selectedIncidentId) ?? filteredIncidents[0],
    [incidents, selectedIncidentId, filteredIncidents],
  );
  const selectedEvent = useMemo(
    () => actionableEvents.find((event) => event.id === selectedEventId) ?? actionableEvents[0],
    [actionableEvents, selectedEventId],
  );
  const totalAdminRows = adminTable?.total_rows ?? rows.length;
  const canAdminWrite = authPermissions.includes("admin:write");
  const canEditClientPoints = authPermissions.includes("client_points:write");
  const dayTotals = dashboardSummary?.totals;

  async function refreshAll(activeConfig = config, showNotice = true) {
    setLoading(true);
    setError("");
    if (showNotice) setNotice("");
    try {
      const [nextAdminTable, nextDashboardSummary, nextReport, nextImports, nextClientPoints, nextReadiness, nextEventQueue, nextIncidents] = await Promise.all([
        getAdminTable(activeConfig, { offset: 0 }),
        getDashboardDaySummary(activeConfig, reportDate),
        getDayReport(activeConfig, reportDate),
        listImports(activeConfig),
        listClientPoints(activeConfig).catch(() => []),
        getReadiness(activeConfig).catch(() => null),
        getAdminEvents(activeConfig).catch(() => null),
        getAdminIncidents(activeConfig).catch(() => ({ items: [], summary: {} }) as AdminIncidentsResponse),
      ]);
      setAdminTable(nextAdminTable);
      setDashboardSummary(nextDashboardSummary);
      setReport(nextReport);
      setImports(nextImports);
      setClientPoints(nextClientPoints);
      setExpandedClientPointId("");
      setClientOrderSummaries({});
      setReadiness(nextReadiness);
      setEventQueue(nextEventQueue);
      setIncidents(nextIncidents.items);
      setIncidentSummary(nextIncidents.summary);
      void refreshDryRuns(activeConfig);
      setSelectedOrderIds((current) => current.filter((id) => nextAdminTable.rows.some((row) => row.order_id === id)));
      setSelectedIncidentId((current) => current && nextIncidents.items.some((item) => item.id === current) ? current : "");
      setSelectedEventId((current) => current && nextEventQueue?.recent_events.some((event) => event.id === current) ? current : "");
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

  async function refreshDryRuns(activeConfig = config) {
    try {
      setDryRuns(await listSkladBotDryRuns(activeConfig));
    } catch {
      setDryRuns([]);
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
      setAuthRole(session.role || "");
      setAuthPermissions(session.permissions ?? []);
      if (session.authenticated) {
        await refreshAll(config, false);
      }
    } catch {
      setAuthenticated(false);
      setAuthUser("");
      setAuthRole("");
      setAuthPermissions([]);
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
      setAuthRole(session.role || "");
      setAuthPermissions(session.permissions ?? []);
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
      setAuthRole("");
      setAuthPermissions([]);
      setAdminTable(null);
      setImports([]);
      setDryRuns([]);
      setClientPoints([]);
      setReadiness(null);
      setEventQueue(null);
      setIncidents([]);
      setIncidentSummary({});
      setReport(null);
      setDashboardSummary(null);
      setSelectedOrderIds([]);
      setSelectedIncidentId("");
      setSelectedEventId("");
      setEditingClientPointId("");
      setClientSlotDraft({ deliveryFrom: "", deliveryTo: "" });
      setAdminActionReason("");
    }
  }

  function toggleOrderSelection(orderId: string) {
    if (!canAdminWrite) return;
    setSelectedOrderIds((current) => (
      current.includes(orderId)
        ? current.filter((value) => value !== orderId)
        : [...current, orderId]
    ));
  }

  function toggleVisibleOrderSelection() {
    if (!canAdminWrite) return;
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

  function startClientPointEdit(point: ClientPoint) {
    setEditingClientPointId(point.id);
    setClientSlotDraft({
      deliveryFrom: point.delivery_from || "10:00",
      deliveryTo: point.delivery_to || "18:00",
    });
  }

  function cancelClientPointEdit() {
    setEditingClientPointId("");
    setClientSlotDraft({ deliveryFrom: "", deliveryTo: "" });
  }

  async function toggleClientPointOrderHistory(point: ClientPoint) {
    if (expandedClientPointId === point.id) {
      setExpandedClientPointId("");
      return;
    }
    setExpandedClientPointId(point.id);
    if (point.orders_count <= 0 || clientOrderSummaries[point.id]) {
      return;
    }
    setClientOrderSummaryLoadingId(point.id);
    setError("");
    try {
      const summary = await getClientPointOrderSummary(config, point.client_name);
      setClientOrderSummaries((current) => ({ ...current, [point.id]: summary }));
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось загрузить историю заказов клиента");
    } finally {
      setClientOrderSummaryLoadingId("");
    }
  }

  async function saveClientPointTimeslot(point: ClientPoint) {
    const deliveryFrom = clientSlotDraft.deliveryFrom || point.delivery_from;
    const deliveryTo = clientSlotDraft.deliveryTo || point.delivery_to;
    if (!deliveryFrom || !deliveryTo) {
      setError("Укажите время с и до");
      return;
    }
    setBusyAction(`client-slot:${point.id}`);
    setError("");
    setNotice("");
    try {
      await updateClientPointTimeslot(config, {
        client_name: point.client_name,
        address: point.address,
        point_name: point.point_name,
        coordinates: point.coordinates,
        representative: point.representative,
        delivery_from: deliveryFrom,
        delivery_to: deliveryTo,
        is_active: point.is_active,
        actor: "web",
        reason: "Изменение таймслота точки в web-панели",
      });
      const nextClientPoints = await listClientPoints(config).catch(() => []);
      setClientPoints(nextClientPoints);
      setExpandedClientPointId("");
      setClientOrderSummaries({});
      setEditingClientPointId("");
      setClientSlotDraft({ deliveryFrom: "", deliveryTo: "" });
      setNotice("Таймслот сохранен");
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось сохранить таймслот");
    } finally {
      setBusyAction("");
    }
  }

  async function saveNewClientPoint() {
    const clientName = newClientPointDraft.clientName.trim();
    const address = newClientPointDraft.address.trim();
    const deliveryFrom = newClientPointDraft.deliveryFrom || "10:00";
    const deliveryTo = newClientPointDraft.deliveryTo || "18:00";
    if (!clientName || !address) {
      setError("Укажите юрлицо и адрес точки");
      return;
    }
    setBusyAction("client-slot:new");
    setError("");
    setNotice("");
    try {
      await updateClientPointTimeslot(config, {
        client_name: clientName,
        address,
        coordinates: newClientPointDraft.coordinates.trim(),
        representative: newClientPointDraft.representative.trim(),
        delivery_from: deliveryFrom,
        delivery_to: deliveryTo,
        is_active: true,
        actor: "web",
        reason: "Ручное добавление точки в web-панели",
      });
      const nextClientPoints = await listClientPoints(config).catch(() => []);
      setClientPoints(nextClientPoints);
      setExpandedClientPointId("");
      setClientOrderSummaries({});
      setNewClientPointDraft(defaultClientPointDraft());
      setClientPointCreateOpen(false);
      setNotice("Точка добавлена");
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось добавить точку");
    } finally {
      setBusyAction("");
    }
  }

  async function resetClientPointTimeslot(point: ClientPoint) {
    setBusyAction(`client-slot-reset:${point.id}`);
    setError("");
    setNotice("");
    try {
      await updateClientPointTimeslot(config, {
        client_name: point.client_name,
        address: point.address,
        point_name: point.point_name,
        coordinates: point.coordinates,
        representative: point.representative,
        delivery_from: "10:00",
        delivery_to: "18:00",
        is_active: point.is_active,
        actor: "web",
        reason: "Сброс таймслота точки до значения по умолчанию",
      });
      const nextClientPoints = await listClientPoints(config).catch(() => []);
      setClientPoints(nextClientPoints);
      setExpandedClientPointId("");
      setClientOrderSummaries({});
      setEditingClientPointId("");
      setClientSlotDraft({ deliveryFrom: "", deliveryTo: "" });
      setNotice("Таймслот сброшен до 10:00-18:00");
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось сбросить таймслот");
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
      await refreshDryRuns(config);
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

  async function runIncidentStatusAction(incident: AdminIncident, status: "resolved" | "ignored") {
    const reason = adminActionReason.trim();
    if (!reason) {
      setError("Укажите причину действия");
      return;
    }
    setBusyAction(`incident:${status}:${incident.id}`);
    setError("");
    setNotice("");
    try {
      await updateIncidentStatus(config, incident.id, {
        status,
        reason,
        actor: "web",
        source: "web",
      });
      setAdminActionReason("");
      await refreshAll(config, false);
      setNotice(status === "resolved" ? "Инцидент закрыт" : "Инцидент проигнорирован");
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось обновить инцидент");
    } finally {
      setBusyAction("");
    }
  }

  async function runEventRetry(event: EventQueueEvent) {
    const reason = adminActionReason.trim();
    if (!reason) {
      setError("Укажите причину действия");
      return;
    }
    if (!event.retryable) {
      setError("Это событие нельзя повторить вручную");
      return;
    }
    setBusyAction(`event-retry:${event.id}`);
    setError("");
    setNotice("");
    try {
      await retryAdminEvent(config, event.id, {
        reason,
        actor: "web",
        source: "web",
        idempotency_key: makeIdempotencyKey(),
      });
      setAdminActionReason("");
      await refreshAll(config, false);
      setNotice("Событие возвращено в очередь");
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Не удалось повторить событие");
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
          : kind === "deleteActive"
            ? "Удаление ошибочно созданного активного заказа без КИЗов"
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
        source: "web",
        idempotency_key: makeIdempotencyKey(),
        ...(kind === "completeWithoutKiz"
          ? { expected_updated_at_by_order: selectedUpdatedAtByOrder(selectedRows) }
          : { expected_updated_at: primaryRow.updated_at || "" }),
      };
      if (kind === "resync") {
        await resyncGoogleOrder(config, primaryRow.order_id, payload);
      } else if (kind === "archive") {
        await archiveOrderWithoutKiz(config, primaryRow.order_id, payload);
      } else if (kind === "completeWithoutKiz") {
        await completeOrdersWithoutKiz(config, selectedOrderIds, payload);
      } else if (kind === "cancel") {
        await cancelOrder(config, primaryRow.order_id, payload);
      } else if (kind === "deleteActive") {
        await deleteActiveOrder(config, primaryRow.order_id, payload);
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
            <span>веб-панель</span>
          </div>
        </div>
        <nav className="nav-tabs" aria-label="Разделы панели">
          <button className={tab === "table" ? "active" : ""} onClick={() => setTab("table")} aria-current={tab === "table" ? "page" : undefined}>
            <ClipboardList size={18} />
            Таблица
          </button>
          <button className={tab === "clients" ? "active" : ""} onClick={() => setTab("clients")} aria-current={tab === "clients" ? "page" : undefined}>
            <Building2 size={18} />
            Клиенты
          </button>
          <button className={tab === "report" ? "active" : ""} onClick={() => setTab("report")} aria-current={tab === "report" ? "page" : undefined}>
            <BarChart3 size={18} />
            Отчет
          </button>
          <button className={tab === "imports" ? "active" : ""} onClick={() => setTab("imports")} aria-current={tab === "imports" ? "page" : undefined}>
            <FileSpreadsheet size={18} />
            Импорты
          </button>
          <button className={tab === "skladbotDryRun" ? "active" : ""} onClick={() => setTab("skladbotDryRun")} aria-current={tab === "skladbotDryRun" ? "page" : undefined}>
            <SquareCode size={18} />
            SkladBot dry-run
          </button>
          <button className={tab === "incidents" ? "active" : ""} onClick={() => setTab("incidents")} aria-current={tab === "incidents" ? "page" : undefined}>
            <AlertCircle size={18} />
            Инциденты
          </button>
          <button className={tab === "activity" ? "active" : ""} onClick={() => setTab("activity")} aria-current={tab === "activity" ? "page" : undefined}>
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
          <div className="topbar-actions" aria-label="Действия панели">
            <div className="user-pill" title={authUser ? `Пользователь ${maskLogin(authUser)}` : "Пользователь"}>
              <ShieldCheck size={17} />
              {authUser ? `${maskLogin(authUser)} · ${roleLabel(authRole)}` : "Вход выполнен"}
            </div>
            {canAdminWrite && (
              <>
                <button className="ghost-button" onClick={() => void retryGoogleQueue()} disabled={Boolean(busyAction)} title="Повторить Google-очередь">
                  {busyAction === "retry-google" ? <Loader2 className="spin" size={18} /> : <Database size={18} />}
                  Google очередь
                </button>
                <button className="ghost-button" onClick={() => void syncExternalSources()} disabled={Boolean(busyAction)} title="Обновить Google Sheets и SkladBot через backend">
                  {busyAction === "sync-sources" ? <Loader2 className="spin" size={18} /> : <RefreshCw size={18} />}
                  Google/SkladBot
                </button>
              </>
            )}
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
            <div className={error ? "message error" : "message success"} role="status" aria-live="polite">
            {error ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
            <span>{error || notice}</span>
          </div>
        )}

        <section className="stats-section" aria-label="Информация за день">
          <div className="stats-section-head">
            <h2>Информация за день</h2>
            <span>{formatDate(dashboardSummary?.report_date ?? reportDate)}</span>
          </div>
          <div className="stats-row">
            <Metric icon={<ClipboardList size={20} />} label="Акт. заказы" value={dayTotals?.active_orders ?? 0} />
            <Metric icon={<PackageCheck size={20} />} label="Отскан. блоков" value={dayTotals?.scanned_blocks ?? 0} />
            <Metric icon={<Box size={20} />} label="Всего блоков" value={dayTotals?.planned_blocks ?? 0} />
            <Metric icon={<Activity size={20} />} label="Всего заказов" value={dayTotals?.orders ?? 0} />
          </div>
        </section>

        {tab === "table" && (
          <section className="table-panel">
            <div className="panel-header table-panel-header">
              <div>
                <h2>Позиции заказов</h2>
                <span className="panel-subtitle">
                  {adminTablePageSummary(filteredRows.length, rows.length, totalAdminRows)}
                </span>
              </div>
              <label className="search-box">
                <Search size={16} />
                <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск" aria-label="Поиск заказов" />
              </label>
            </div>

            <div className="filters-bar">
              <input
                className="date-input"
                type="date"
                value={shipmentDateFilter}
                onChange={(event) => setShipmentDateFilter(event.target.value)}
                title="Дата отгрузки"
                aria-label="Дата отгрузки"
              />
              <SelectFilter value={statusFilter} onChange={(value) => setStatusFilter(value as StatusFilter)} ariaLabel="Фильтр статуса заказа">
                <option value="all">Все статусы</option>
                <option value="active">Активные</option>
                <option value="archive">Архив</option>
                <option value="archive_no_kiz">Архив без КИЗов</option>
                <option value="cancelled">Отменены</option>
                <option value="returned">Возвраты</option>
                <option value="removed_from_google">Удалены из Google</option>
              </SelectFilter>
              <SelectFilter value={scanFilter} onChange={(value) => setScanFilter(value as ScanFilter)} ariaLabel="Фильтр сканирования">
                <option value="all">Все сканы</option>
                <option value="not_started">Не начато</option>
                <option value="in_progress">В работе</option>
                <option value="completed">Готово</option>
                <option value="over_scanned">Перескан</option>
                <option value="no_plan">Нет плана</option>
              </SelectFilter>
              <SelectFilter value={skladbotFilter} onChange={(value) => setSkladbotFilter(value as SkladBotFilter)} ariaLabel="Фильтр SkladBot">
                <option value="all">SkladBot: все</option>
                <option value="found">Найдено</option>
                <option value="missing">Без номера</option>
                <option value="problem">Проблема</option>
              </SelectFilter>
              <SelectFilter value={googleFilter} onChange={(value) => setGoogleFilter(value as GoogleFilter)} ariaLabel="Фильтр Google">
                <option value="all">Google: все</option>
                <option value="synced">Синхронизировано</option>
                <option value="pending">Очередь</option>
                <option value="removed_from_google">Удалено</option>
                <option value="unknown">Неизвестно</option>
              </SelectFilter>
              {canAdminWrite && (
                <button
                  className="ghost-button"
                  onClick={toggleVisibleOrderSelection}
                  disabled={visibleOrderIds.length === 0}
                  title="Выбрать все заказы, которые сейчас видны после фильтров"
                >
                  <ClipboardList size={16} />
                  {allVisibleSelected ? "Снять видимые" : "Выделить все"}
                </button>
              )}
              {shipmentDateFilter && (
                <button className="ghost-button" onClick={() => setShipmentDateFilter("")} aria-label="Сбросить фильтр даты отгрузки">
                  Сбросить дату
                </button>
              )}
            </div>

            {canAdminWrite && selectedOrderIds.length > 0 && (
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
                onDeleteActive={() => void runOrderAction("deleteActive")}
                onRestore={() => void runOrderAction("restore")}
                onResyncSkladBot={() => void runOrderAction("resyncSkladBot")}
              />
            )}

            <AdminRowsTable
              rows={filteredRows}
              selectedOrderIds={selectedOrderIds}
              allVisibleSelected={allVisibleSelected}
              canSelect={canAdminWrite}
              onToggleVisible={toggleVisibleOrderSelection}
              onToggleOrder={toggleOrderSelection}
            />
          </section>
        )}

        {tab === "clients" && (
          <ClientsPanel
            points={filteredClientPoints}
            summary={clientPointSummary}
            search={clientSearch}
            timeslotFilter={clientTimeslotFilter}
            editingPointId={editingClientPointId}
            expandedPointId={expandedClientPointId}
            orderSummaries={clientOrderSummaries}
            loadingOrderSummaryId={clientOrderSummaryLoadingId}
            draft={clientSlotDraft}
            createOpen={clientPointCreateOpen}
            newPointDraft={newClientPointDraft}
            busyAction={busyAction}
            canEdit={canEditClientPoints}
            onSearchChange={setClientSearch}
            onTimeslotFilterChange={(value) => setClientTimeslotFilter(value as ClientTimeslotFilter)}
            onDraftChange={setClientSlotDraft}
            onNewPointDraftChange={setNewClientPointDraft}
            onEdit={startClientPointEdit}
            onToggleOrderHistory={(point) => void toggleClientPointOrderHistory(point)}
            onCancel={cancelClientPointEdit}
            onSave={(point) => void saveClientPointTimeslot(point)}
            onToggleCreate={() => setClientPointCreateOpen((current) => !current)}
            onCreate={() => void saveNewClientPoint()}
            onReset={(point) => void resetClientPointTimeslot(point)}
          />
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
                aria-label="Дата дневного отчета"
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
              headers={["Дата", "Источник", "Статус", "Строк", "Импортировано", "SkladBot dry-run", "Ошибки"]}
              rows={imports.map((item) => [
                new Date(item.created_at).toLocaleString("ru-RU"),
                item.source,
                item.status,
                String(item.rows_total),
                String(item.rows_imported),
                importDryRunSummaryText(item),
                importIssuesText(item),
              ])}
            />
          </section>
        )}

        {tab === "skladbotDryRun" && (
          <SkladBotDryRunPanel
            dryRuns={dryRuns}
            imports={imports}
            busyAction={busyAction}
            canAdminWrite={canAdminWrite}
            onRebuild={(eventId) => void rebuildDryRun(eventId)}
          />
        )}

        {tab === "incidents" && (
          <AdminCenterPanel
            incidents={filteredIncidents}
            allIncidents={incidents}
            incidentSummary={incidentSummary}
            sourceOptions={sourceOptions}
            events={actionableEvents}
            selectedIncident={selectedIncident}
            selectedEvent={selectedEvent}
            selectedIncidentId={selectedIncidentId}
            selectedEventId={selectedEventId}
            incidentSearch={incidentSearch}
            incidentStatusFilter={incidentStatusFilter}
            incidentSeverityFilter={incidentSeverityFilter}
            incidentSourceFilter={incidentSourceFilter}
            actionReason={adminActionReason}
            busyAction={busyAction}
            canAdminWrite={canAdminWrite}
            onSearchChange={setIncidentSearch}
            onStatusFilterChange={(value) => setIncidentStatusFilter(value as IncidentStatusFilter)}
            onSeverityFilterChange={(value) => setIncidentSeverityFilter(value as IncidentSeverityFilter)}
            onSourceFilterChange={setIncidentSourceFilter}
            onSelectIncident={setSelectedIncidentId}
            onSelectEvent={setSelectedEventId}
            onReasonChange={setAdminActionReason}
            onResolveIncident={(incident) => void runIncidentStatusAction(incident, "resolved")}
            onIgnoreIncident={(incident) => void runIncidentStatusAction(incident, "ignored")}
            onRetryEvent={(event) => void runEventRetry(event)}
          />
        )}

        {tab === "activity" && (
          <section className="table-panel">
            <div className="panel-header">
              <h2>Последняя активность</h2>
              {canAdminWrite && (
                <button className="ghost-button" onClick={() => void downloadAuditLog()} disabled={Boolean(busyAction)} title="Скачать backend diagnostics log с audit-событиями">
                  {busyAction === "audit-log" ? <Loader2 className="spin" size={16} /> : <History size={16} />}
                  Audit log
                </button>
              )}
            </div>
            <SystemDiagnosticsPanel readiness={readiness} eventQueue={eventQueue} />
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
    deleteActive: "",
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
    disabledReason.deleteActive = "Сначала обработайте Google очередь";
    disabledReason.resetRescan = "Сначала обработайте Google очередь";
    disabledReason.restore = "Сначала обработайте Google очередь";
    disabledReason.resyncSkladBot = "Сначала обработайте Google очередь";
  }
  const allActive = selectedRows.every((row) => row.status_bucket === "active");
  if (!allActive) {
    disabledReason.archive = "Доступно только для активного заказа";
    disabledReason.completeWithoutKiz = "Доступно только для активных заказов";
    disabledReason.cancel = "Доступно только для активного заказа";
    disabledReason.deleteActive = "Доступно только для активного заказа";
    disabledReason.resyncSkladBot = "Доступно только для активного заказа";
  }
  if (selectedCount > 1) {
    const reason = "Выберите один заказ";
    disabledReason.resync = reason;
    disabledReason.archive = reason;
    disabledReason.cancel = reason;
    disabledReason.deleteActive = reason;
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
  if (scannedBlocks > 0 || scanCodes > 0) {
    disabledReason.archive = "В заказе уже есть отсканированные КИЗы";
    disabledReason.cancel = "В заказе уже есть отсканированные КИЗы";
    disabledReason.deleteActive = "В заказе уже есть отсканированные КИЗы";
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
    deleteActive: reason,
    resetRescan: reason,
    restore: reason,
    resyncSkladBot: reason,
  };
}

function firstVisibleActionBlockReason(reasons: Record<OrderActionKind, string>) {
  return [
    reasons.archive,
    reasons.cancel,
    reasons.deleteActive,
    reasons.resetRescan,
    reasons.restore,
    reasons.resyncSkladBot,
    reasons.resync,
    reasons.completeWithoutKiz,
  ].find((reason, index, list) => reason && list.indexOf(reason) === index) || "";
}

function selectedUpdatedAtByOrder(rows: AdminTableRow[]) {
  const values: Record<string, string> = {};
  for (const row of rows) {
    if (row.order_id && row.updated_at && !values[row.order_id]) {
      values[row.order_id] = row.updated_at;
    }
  }
  return values;
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
  onDeleteActive,
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
  onDeleteActive: () => void;
  onRestore: () => void;
  onResyncSkladBot: () => void;
}) {
  const firstRow = selectedRows[0];
  const isBusy = Boolean(busyAction);
  const visibleBlockReason = firstVisibleActionBlockReason(state.disabledReason);

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
        {visibleBlockReason && (
          <span className="action-warning">
            <AlertCircle size={14} />
            {visibleBlockReason}
          </span>
        )}
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
          className="ghost-button danger-button"
          onClick={onDeleteActive}
          disabled={isBusy || Boolean(state.disabledReason.deleteActive)}
          title={state.disabledReason.deleteActive || "Удалить ошибочно созданный активный заказ без КИЗов; SkladBot не удаляется автоматически"}
        >
          {busyAction === "deleteActive" ? <Loader2 className="spin" size={16} /> : <Trash2 size={16} />}
          Удалить из активных
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
  ariaLabel,
  children,
}: {
  value: string;
  onChange: (value: string) => void;
  ariaLabel: string;
  children: ReactNode;
}) {
  return (
    <select className="filter-select" value={value} onChange={(event) => onChange(event.target.value)} aria-label={ariaLabel}>
      {children}
    </select>
  );
}

function adminTablePageSummary(filteredCount: number, loadedCount: number, totalCount: number) {
  if (loadedCount !== totalCount) return `Показано ${formatNumber(filteredCount)} из ${formatNumber(loadedCount)} · всего ${formatNumber(totalCount)}`;
  return `Показано ${formatNumber(filteredCount)} из ${formatNumber(loadedCount)}`;
}

function AdminRowsTable({
  rows,
  selectedOrderIds,
  allVisibleSelected,
  canSelect,
  onToggleVisible,
  onToggleOrder,
}: {
  rows: AdminTableRow[];
  selectedOrderIds: string[];
  allVisibleSelected: boolean;
  canSelect: boolean;
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
                disabled={rows.length === 0 || !canSelect}
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
                    disabled={!canSelect}
                    aria-label={`Выбрать заказ ${row.client}`}
                  />
                </td>
                <td className="date-cell">
                  <strong className="cell-title">{formatDate(row.order_date)}</strong>
                  <span className="table-muted">{row.payment_type}</span>
                </td>
                <td className="client-cell">
                  <strong className="cell-title" title={row.client}>{row.client}</strong>
                  <span className="table-muted cell-sub" title={row.address}>{row.address}</span>
                  {row.representative && <span className="table-muted cell-sub" title={row.representative}>{row.representative}</span>}
                </td>
                <td className="product-cell">
                  <strong className="cell-title" title={row.product}>{row.product}</strong>
                  <span className="table-muted cell-sub" title={row.source_file || "-"}>{row.source_file || "-"}</span>
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
                  {row.status_bucket === "returned" && (
                    <span className="table-muted cell-sub">
                      Возврат: {row.skladbot_return_request_number || "не создан"} ·{" "}
                      {returnSkladBotStatusLabel(row.skladbot_return_status)}
                    </span>
                  )}
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

function ClientsPanel({
  points,
  summary,
  search,
  timeslotFilter,
  editingPointId,
  expandedPointId,
  orderSummaries,
  loadingOrderSummaryId,
  draft,
  createOpen,
  newPointDraft,
  busyAction,
  canEdit,
  onSearchChange,
  onTimeslotFilterChange,
  onDraftChange,
  onNewPointDraftChange,
  onEdit,
  onToggleOrderHistory,
  onCancel,
  onSave,
  onToggleCreate,
  onCreate,
  onReset,
}: {
  points: ClientPoint[];
  summary: { total: number; saved: number; custom: number; default: number };
  search: string;
  timeslotFilter: ClientTimeslotFilter;
  editingPointId: string;
  expandedPointId: string;
  orderSummaries: Record<string, ClientPointOrderSummary>;
  loadingOrderSummaryId: string;
  draft: { deliveryFrom: string; deliveryTo: string };
  createOpen: boolean;
  newPointDraft: ClientPointFormDraft;
  busyAction: string;
  canEdit: boolean;
  onSearchChange: (value: string) => void;
  onTimeslotFilterChange: (value: string) => void;
  onDraftChange: (value: { deliveryFrom: string; deliveryTo: string }) => void;
  onNewPointDraftChange: (value: ClientPointFormDraft) => void;
  onEdit: (point: ClientPoint) => void;
  onToggleOrderHistory: (point: ClientPoint) => void;
  onCancel: () => void;
  onSave: (point: ClientPoint) => void;
  onToggleCreate: () => void;
  onCreate: () => void;
  onReset: (point: ClientPoint) => void;
}) {
  const creating = busyAction === "client-slot:new";
  return (
    <section className="table-panel">
      <div className="panel-header table-panel-header">
        <div>
          <h2>Клиенты и таймслоты</h2>
          <span className="panel-subtitle">Показано {formatNumber(points.length)} из {formatNumber(summary.total)}</span>
        </div>
        <label className="search-box">
          <Search size={16} />
          <input type="search" value={search} onChange={(event) => onSearchChange(event.target.value)} placeholder="Поиск клиентов" aria-label="Поиск клиентов" />
        </label>
        {canEdit && (
          <button className="primary-button client-create-toggle" onClick={onToggleCreate} aria-expanded={createOpen} aria-controls="client-point-create-form">
            <Plus size={16} />
            Создать точку
          </button>
        )}
      </div>

      <section className="stats-row compact">
        <Metric icon={<Building2 size={20} />} label="Точек" value={summary.total} />
        <Metric icon={<Database size={20} />} label="Сохранено" value={summary.saved} />
        <Metric icon={<Save size={20} />} label="Свой слот" value={summary.custom} tone={summary.custom ? "warn" : undefined} />
        <Metric icon={<ClipboardList size={20} />} label="10-18" value={summary.default} />
      </section>

      {canEdit && createOpen && (
        <section className="client-point-form" id="client-point-create-form" aria-label="Создать точку">
          <h3>Создать точку</h3>
          <div className="client-point-form-grid">
            <label>
              <span>Клиент / юрлицо</span>
              <input
                className="client-text-input"
                value={newPointDraft.clientName}
                onChange={(event) => onNewPointDraftChange({ ...newPointDraft, clientName: event.target.value })}
                autoComplete="organization"
              />
            </label>
            <label>
              <span>Адрес</span>
              <input
                className="client-text-input"
                value={newPointDraft.address}
                onChange={(event) => onNewPointDraftChange({ ...newPointDraft, address: event.target.value })}
                autoComplete="street-address"
              />
            </label>
            <label>
              <span>ТП</span>
              <input
                className="client-text-input"
                value={newPointDraft.representative}
                onChange={(event) => onNewPointDraftChange({ ...newPointDraft, representative: event.target.value })}
                autoComplete="name"
              />
            </label>
            <label>
              <span>С</span>
              <input
                className="time-input"
                type="time"
                value={newPointDraft.deliveryFrom}
                onChange={(event) => onNewPointDraftChange({ ...newPointDraft, deliveryFrom: event.target.value })}
              />
            </label>
            <label>
              <span>До</span>
              <input
                className="time-input"
                type="time"
                value={newPointDraft.deliveryTo}
                onChange={(event) => onNewPointDraftChange({ ...newPointDraft, deliveryTo: event.target.value })}
              />
            </label>
            <label>
              <span>Координаты</span>
              <input
                className="client-text-input"
                value={newPointDraft.coordinates}
                onChange={(event) => onNewPointDraftChange({ ...newPointDraft, coordinates: event.target.value })}
                inputMode="decimal"
              />
            </label>
            <button className="ghost-button client-point-add-button" onClick={onCreate} disabled={Boolean(busyAction)} aria-busy={creating}>
              {creating ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}
              Добавить
            </button>
          </div>
        </section>
      )}

      <div className="filters-bar">
        <SelectFilter value={timeslotFilter} onChange={onTimeslotFilterChange} ariaLabel="Фильтр таймслотов">
          <option value="all">Все таймслоты</option>
          <option value="custom">Уникальный слот</option>
          <option value="default">По умолчанию 10-18</option>
        </SelectFilter>
      </div>

      <div className="data-table-wrap client-points-table-wrap">
        <table className="data-table client-points-table">
          <thead>
            <tr>
              <th>Юрлицо</th>
              <th>Адрес</th>
              <th>Таймслот</th>
              <th>Заказы</th>
              <th>Статус</th>
              <th className="actions-col">Действия</th>
            </tr>
          </thead>
          <tbody>
            {points.map((point) => {
              const editing = editingPointId === point.id;
              const expanded = expandedPointId === point.id;
              const busy = busyAction === `client-slot:${point.id}`;
              const resetting = busyAction === `client-slot-reset:${point.id}`;
              return (
                <Fragment key={point.id}>
                  <tr>
                    <td>
                      <strong className="cell-title" title={point.client_name}>{point.client_name}</strong>
                      {point.representative && <span className="table-muted cell-sub" title={point.representative}>{point.representative}</span>}
                    </td>
                    <td>
                      <strong className="cell-title" title={point.point_name || point.address}>{point.point_name || point.address}</strong>
                      {point.point_name && <span className="table-muted cell-sub" title={point.address}>{point.address}</span>}
                      {point.coordinates && <span className="table-muted cell-sub" title={point.coordinates}>{point.coordinates}</span>}
                    </td>
                    <td>
                      {editing ? (
                        <div className="slot-edit-row">
                          <input
                            className="time-input"
                            type="time"
                            value={draft.deliveryFrom}
                            onChange={(event) => onDraftChange({ ...draft, deliveryFrom: event.target.value })}
                            aria-label="Доставка с"
                          />
                          <input
                            className="time-input"
                            type="time"
                            value={draft.deliveryTo}
                            onChange={(event) => onDraftChange({ ...draft, deliveryTo: event.target.value })}
                            aria-label="Доставка до"
                          />
                        </div>
                      ) : (
                        <>
                          <strong className="cell-title">{point.delivery_from}-{point.delivery_to}</strong>
                          <span className="table-muted cell-sub">{point.has_custom_timeslot ? "уникальный" : "по умолчанию"}</span>
                        </>
                      )}
                    </td>
                    <td>
                      <button
                        className="client-orders-toggle"
                        onClick={() => onToggleOrderHistory(point)}
                        disabled={point.orders_count <= 0}
                        aria-expanded={expanded}
                        aria-controls={`client-orders-${point.id}`}
                      >
                        <strong className="cell-title">{formatNumber(point.orders_count)}</strong>
                      </button>
                      <span className="table-muted cell-sub">{formatDate(point.last_order_date)}</span>
                    </td>
                    <td>
                      <span className={`status-badge ${point.has_custom_timeslot ? "client-custom" : "client-default"}`}>
                        {point.has_custom_timeslot ? "Свой слот" : "10-18"}
                      </span>
                      <span className="table-muted cell-sub">{point.is_saved ? "сохранено" : "из заказов"}</span>
                    </td>
                    <td>
                      {canEdit && editing ? (
                        <div className="client-row-actions">
                          <button className="ghost-button" onClick={() => onSave(point)} disabled={Boolean(busyAction)} aria-busy={busy}>
                            {busy ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
                            Сохранить
                          </button>
                          <button className="ghost-button quiet-button" onClick={onCancel} disabled={Boolean(busyAction)}>
                            Отмена
                          </button>
                        </div>
                      ) : canEdit ? (
                        <div className="client-row-actions">
                          <button className="ghost-button" onClick={() => onEdit(point)} disabled={Boolean(busyAction)}>
                            Редактировать
                          </button>
                          {point.has_custom_timeslot && (
                            <button className="ghost-button quiet-button" onClick={() => onReset(point)} disabled={Boolean(busyAction)}>
                              {resetting ? <Loader2 className="spin" size={16} /> : <Trash2 size={16} />}
                              Сбросить
                            </button>
                          )}
                        </div>
                      ) : (
                        <span className="table-muted">read-only</span>
                      )}
                    </td>
                  </tr>
                  {expanded && (
                    <tr className="client-orders-detail-row">
                      <td colSpan={6}>
                        <ClientOrderHistory
                          point={point}
                          summary={orderSummaries[point.id]}
                          loading={loadingOrderSummaryId === point.id}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {points.length === 0 && (
              <tr>
                <td colSpan={6}>Нет данных</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ClientOrderHistory({ point, summary, loading }: { point: ClientPoint; summary?: ClientPointOrderSummary; loading: boolean }) {
  if (loading) {
    return (
      <div className="client-orders-empty" id={`client-orders-${point.id}`}>
        <Loader2 className="spin" size={16} />
        Загрузка истории заказов...
      </div>
    );
  }
  const history = summary?.dates ?? [];
  if (!summary || history.length === 0) {
    return <div className="client-orders-empty" id={`client-orders-${point.id}`}>По этому юрлицу нет заказов в базе.</div>;
  }
  return (
    <div className="client-orders-detail" id={`client-orders-${point.id}`}>
      {history.map((entry) => (
        <section className="client-order-date-card" key={entry.shipment_date || "no-date"}>
          <div className="client-order-date-head">
            <div className="client-order-date-meta">
              <strong>{formatDate(entry.shipment_date)}</strong>
              <span>Тип оплаты: {entry.payment_type || "-"}</span>
            </div>
            <span>
              {formatNumber(entry.orders_count)} заказов · {formatNumber(entry.positions_count)} позиций · {formatClientProductQuantity(entry.quantity_blocks, entry.quantity_pieces)}
            </span>
          </div>
          <ul className="client-order-products">
            {entry.products.map((product) => (
              <li key={product.product}>
                <span title={product.product}>{product.product}</span>
                <strong title={`${formatNumber(product.positions_count)} позиций`}>
                  {formatClientProductQuantity(product.quantity_blocks, product.quantity_pieces)}
                </strong>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function SkladBotDryRunPanel({
  dryRuns,
  imports,
  busyAction,
  canAdminWrite,
  onRebuild,
}: {
  dryRuns: SkladBotDryRun[];
  imports: ImportRecord[];
  busyAction: string;
  canAdminWrite: boolean;
  onRebuild: (eventId: string) => void;
}) {
  const [importFilter, setImportFilter] = useState("");
  const filteredRuns = useMemo(
    () => dryRuns.filter((item) => !importFilter || item.import_id === importFilter),
    [dryRuns, importFilter],
  );
  const summary = useMemo(() => ({
    ready: filteredRuns.filter((item) => item.status === "ready").length,
    queued: filteredRuns.filter((item) => item.status === "queued").length,
    created: filteredRuns.filter((item) => item.status === "created").length,
    recovered: filteredRuns.filter((item) => item.status === "recovered").length,
    blocked: filteredRuns.filter((item) => item.status === "blocked").length,
    failed: filteredRuns.filter((item) => item.status === "create_failed").length,
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
          <span className="panel-subtitle">Preview и очередь автосоздания заявок SkladBot</span>
        </div>
        <SelectFilter value={importFilter} onChange={setImportFilter} ariaLabel="Фильтр импорта SkladBot dry-run">
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
        <Metric icon={<ClipboardList size={20} />} label="Queued" value={summary.queued} />
        <Metric icon={<Server size={20} />} label="Created" value={summary.created + summary.recovered} />
        <Metric icon={<AlertCircle size={20} />} label="Blocked" value={summary.blocked + summary.failed} tone={summary.blocked + summary.failed > 0 ? "warn" : undefined} />
        <Metric icon={<Server size={20} />} label="Уже WH-R" value={summary.alreadyLinked} />
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
                    {canAdminWrite ? (
                      <button className="ghost-button" onClick={() => onRebuild(item.event_id)} disabled={Boolean(busyAction)}>
                        {busyAction === rebuildAction ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
                        Пересобрать
                      </button>
                    ) : (
                      <span className="table-muted">read-only</span>
                    )}
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

function AdminCenterPanel({
  incidents,
  allIncidents,
  incidentSummary,
  sourceOptions,
  events,
  selectedIncident,
  selectedEvent,
  selectedIncidentId,
  selectedEventId,
  incidentSearch,
  incidentStatusFilter,
  incidentSeverityFilter,
  incidentSourceFilter,
  actionReason,
  busyAction,
  canAdminWrite,
  onSearchChange,
  onStatusFilterChange,
  onSeverityFilterChange,
  onSourceFilterChange,
  onSelectIncident,
  onSelectEvent,
  onReasonChange,
  onResolveIncident,
  onIgnoreIncident,
  onRetryEvent,
}: {
  incidents: AdminIncident[];
  allIncidents: AdminIncident[];
  incidentSummary: Record<string, unknown>;
  sourceOptions: string[];
  events: EventQueueEvent[];
  selectedIncident: AdminIncident | undefined;
  selectedEvent: EventQueueEvent | undefined;
  selectedIncidentId: string;
  selectedEventId: string;
  incidentSearch: string;
  incidentStatusFilter: IncidentStatusFilter;
  incidentSeverityFilter: IncidentSeverityFilter;
  incidentSourceFilter: string;
  actionReason: string;
  busyAction: string;
  canAdminWrite: boolean;
  onSearchChange: (value: string) => void;
  onStatusFilterChange: (value: string) => void;
  onSeverityFilterChange: (value: string) => void;
  onSourceFilterChange: (value: string) => void;
  onSelectIncident: (value: string) => void;
  onSelectEvent: (value: string) => void;
  onReasonChange: (value: string) => void;
  onResolveIncident: (incident: AdminIncident) => void;
  onIgnoreIncident: (incident: AdminIncident) => void;
  onRetryEvent: (event: EventQueueEvent) => void;
}) {
  const activeIncidentStatuses = ["open", "in_progress", "manual_review"];
  const openIncidents = allIncidents.filter((item) => activeIncidentStatuses.includes(item.status)).length;
  const criticalIncidents = allIncidents.filter((item) => item.severity === "critical" && activeIncidentStatuses.includes(item.status)).length;
  const retryableEvents = events.filter((event) => event.retryable).length;
  const selectedIncidentTerminal = Boolean(selectedIncident && ["resolved", "ignored", "cancelled"].includes(selectedIncident.status));

  return (
    <section className="table-panel">
      <div className="panel-header table-panel-header">
        <div>
          <h2>Инциденты и очередь</h2>
          <span className="panel-subtitle">Показано {incidents.length} инцидентов и {events.length} событий очереди</span>
        </div>
        <label className="search-box">
          <Search size={16} />
          <input type="search" value={incidentSearch} onChange={(event) => onSearchChange(event.target.value)} placeholder="Поиск" aria-label="Поиск инцидентов и очереди" />
        </label>
      </div>

      <section className="stats-row compact">
        <Metric icon={<AlertCircle size={20} />} label="Открыто" value={openIncidents} tone={openIncidents ? "warn" : undefined} />
        <Metric icon={<ShieldCheck size={20} />} label="Critical" value={criticalIncidents} tone={criticalIncidents ? "warn" : undefined} />
        <Metric icon={<Activity size={20} />} label="Retryable" value={retryableEvents} tone={retryableEvents ? "warn" : undefined} />
        <Metric icon={<Database size={20} />} label="Всего" value={numberField(incidentSummary, "total")} />
      </section>

      <div className="filters-bar">
        <SelectFilter value={incidentStatusFilter} onChange={onStatusFilterChange} ariaLabel="Фильтр статуса инцидента">
          <option value="all">Все статусы</option>
          <option value="open">Открытые</option>
          <option value="in_progress">В работе</option>
          <option value="manual_review">Ручная проверка</option>
          <option value="resolved">Закрытые</option>
          <option value="ignored">Игнор</option>
          <option value="cancelled">Отменены</option>
        </SelectFilter>
        <SelectFilter value={incidentSeverityFilter} onChange={onSeverityFilterChange} ariaLabel="Фильтр уровня инцидента">
          <option value="all">Все уровни</option>
          <option value="critical">Critical</option>
          <option value="warning">Warning</option>
          <option value="info">Info</option>
        </SelectFilter>
        <SelectFilter value={incidentSourceFilter} onChange={onSourceFilterChange} ariaLabel="Фильтр источника инцидента">
          <option value="all">Все источники</option>
          {sourceOptions.map((source) => <option key={source} value={source}>{source}</option>)}
        </SelectFilter>
      </div>

      <div className="admin-center-layout">
        <div className="admin-center-main">
          <div className="data-table-wrap admin-center-table-wrap">
            <table className="data-table admin-center-table">
              <thead>
                <tr>
                  <th>Статус</th>
                  <th>Источник</th>
                  <th>Сущность</th>
                  <th>Ошибка</th>
                  <th>Возраст</th>
                </tr>
              </thead>
              <tbody>
                {incidents.map((incident) => (
                  <tr
                    key={incident.id}
                    className={(selectedIncidentId || selectedIncident?.id) === incident.id ? "selected-row" : ""}
                    onClick={() => onSelectIncident(incident.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onSelectIncident(incident.id);
                      }
                    }}
                    tabIndex={0}
                    role="button"
                    aria-selected={(selectedIncidentId || selectedIncident?.id) === incident.id}
                  >
                    <td>
                      <span className={`status-badge incident-${incident.status}`}>{incidentStatusLabel(incident.status)}</span>
                      <span className={`status-badge severity-${incident.severity}`}>{incident.severity}</span>
                    </td>
                    <td>
                      <strong className="cell-title">{incident.source}</strong>
                      <span className="table-muted cell-sub">{formatDateTime(incident.updated_at || incident.created_at)}</span>
                    </td>
                    <td>
                      <strong className="cell-title">{linkedIncidentText(incident)}</strong>
                      <span className="table-muted cell-sub">{incident.external_ref || shortId(incident.id)}</span>
                    </td>
                    <td>
                      <strong className="cell-title">{incident.title}</strong>
                      <span className="table-muted cell-sub clamp-text">{incident.message || "-"}</span>
                    </td>
                    <td>{formatAgeSeconds(ageFromDate(incident.updated_at || incident.created_at))}</td>
                  </tr>
                ))}
                {incidents.length === 0 && (
                  <tr>
                    <td colSpan={5}>Инцидентов нет</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="data-table-wrap admin-center-table-wrap">
            <table className="data-table admin-center-table">
              <thead>
                <tr>
                  <th>Очередь</th>
                  <th>Связь</th>
                  <th>Ошибка</th>
                  <th>Возраст</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {events.map((event) => {
                  const retryAction = `event-retry:${event.id}`;
                  return (
                    <tr
                      key={event.id}
                      className={(selectedEventId || selectedEvent?.id) === event.id ? "selected-row" : ""}
                      onClick={() => onSelectEvent(event.id)}
                      onKeyDown={(keyboardEvent) => {
                        if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") {
                          keyboardEvent.preventDefault();
                          onSelectEvent(event.id);
                        }
                      }}
                      tabIndex={0}
                      role="button"
                      aria-selected={(selectedEventId || selectedEvent?.id) === event.id}
                    >
                      <td>
                        <strong className="cell-title">{event.event_type}</strong>
                        <span className={`status-badge queue-${event.status}`}>{eventStatusLabel(event.status)}</span>
                        <span className="table-muted cell-sub">попыток {event.attempts}</span>
                      </td>
                      <td>
                        <strong className="cell-title">{linkedEventText(event)}</strong>
                        <span className="table-muted cell-sub">{compactId(event.idempotency_key || event.id)}</span>
                      </td>
                      <td>
                        <span className="table-muted cell-sub clamp-text">{event.last_error || event.payload_status || "-"}</span>
                      </td>
                      <td>{formatAgeSeconds(event.age_seconds)}</td>
                      <td>
                        {canAdminWrite && event.retryable ? (
                          <button className="ghost-button" onClick={(click) => { click.stopPropagation(); onRetryEvent(event); }} disabled={Boolean(busyAction)}>
                            {busyAction === retryAction ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
                            Retry
                          </button>
                        ) : (
                          <span className="table-muted">-</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {events.length === 0 && (
                  <tr>
                    <td colSpan={5}>Событий очереди нет</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <aside className="admin-detail-panel">
          {canAdminWrite && (
            <label className="admin-reason-field">
              <span>Причина действия</span>
              <textarea
                value={actionReason}
                onChange={(event) => onReasonChange(event.target.value)}
                placeholder="Например: проверил импорт, можно повторить"
                rows={3}
              />
            </label>
          )}

          <section className="admin-detail-section">
            <div className="detail-head compact">
              <div>
                <h3>Инцидент</h3>
                <span>{selectedIncident ? shortId(selectedIncident.id) : "-"}</span>
              </div>
              {canAdminWrite && selectedIncident && (
                <div className="action-buttons">
                  <button
                    className="ghost-button"
                    onClick={() => onResolveIncident(selectedIncident)}
                    disabled={Boolean(busyAction) || selectedIncidentTerminal}
                  >
                    {busyAction === `incident:resolved:${selectedIncident.id}` ? <Loader2 className="spin" size={16} /> : <CheckCircle2 size={16} />}
                    Resolve
                  </button>
                  <button
                    className="ghost-button"
                    onClick={() => onIgnoreIncident(selectedIncident)}
                    disabled={Boolean(busyAction) || selectedIncidentTerminal}
                  >
                    {busyAction === `incident:ignored:${selectedIncident.id}` ? <Loader2 className="spin" size={16} /> : <Undo2 size={16} />}
                    Ignore
                  </button>
                </div>
              )}
            </div>
            {selectedIncident ? (
              <>
                <dl className="detail-list">
                  <div><dt>Статус</dt><dd>{incidentStatusLabel(selectedIncident.status)} / {selectedIncident.severity}</dd></div>
                  <div><dt>Источник</dt><dd>{selectedIncident.source}</dd></div>
                  <div><dt>Связь</dt><dd>{linkedIncidentText(selectedIncident)}</dd></div>
                  <div><dt>Создан</dt><dd>{formatDateTime(selectedIncident.created_at)}</dd></div>
                </dl>
                <strong className="admin-detail-title">{selectedIncident.title}</strong>
                <pre className="admin-long-text">{selectedIncident.message || "-"}</pre>
                <details className="json-preview wide">
                  <summary>Payload</summary>
                  <pre>{JSON.stringify(selectedIncident.raw_payload, null, 2)}</pre>
                </details>
              </>
            ) : (
              <div className="empty-state">Выберите инцидент</div>
            )}
          </section>

          <section className="admin-detail-section">
            <div className="detail-head compact">
              <div>
                <h3>Событие очереди</h3>
                <span>{selectedEvent ? shortId(selectedEvent.id) : "-"}</span>
              </div>
              {canAdminWrite && selectedEvent?.retryable && (
                <button className="ghost-button" onClick={() => onRetryEvent(selectedEvent)} disabled={Boolean(busyAction)}>
                  {busyAction === `event-retry:${selectedEvent.id}` ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
                  Retry
                </button>
              )}
            </div>
            {selectedEvent ? (
              <>
                <dl className="detail-list">
                  <div><dt>Тип</dt><dd>{selectedEvent.event_type}</dd></div>
                  <div><dt>Статус</dt><dd>{eventStatusLabel(selectedEvent.status)}</dd></div>
                  <div><dt>Связь</dt><dd>{linkedEventText(selectedEvent)}</dd></div>
                  <div><dt>Возраст</dt><dd>{formatAgeSeconds(selectedEvent.age_seconds)}</dd></div>
                </dl>
                <pre className="admin-long-text">{selectedEvent.last_error || "-"}</pre>
                <details className="json-preview wide" open>
                  <summary>Payload</summary>
                  <pre>{JSON.stringify(selectedEvent.raw_payload, null, 2)}</pre>
                </details>
              </>
            ) : (
              <div className="empty-state">Выберите событие</div>
            )}
          </section>
        </aside>
      </div>
    </section>
  );
}

function SystemDiagnosticsPanel({
  readiness,
  eventQueue,
}: {
  readiness: ReadinessResponse | null;
  eventQueue: EventQueueDiagnostics | null;
}) {
  const queueSummary = eventQueue?.summary ?? readiness?.queue?.summary ?? {};
  const activeQueue = numberField(queueSummary, "active");
  const failedEvents = (eventQueue?.recent_events ?? [])
    .filter((event) => ["failed", "error", "blocked", "processing", "pending"].includes(event.status))
    .slice(0, 6);
  const staleEvents = eventQueue?.stale_processing ?? recordArray(readiness?.queue?.stale_processing);
  const queueErrors = recordArray(readiness?.queue?.last_errors);
  const importErrors = recordArray(readiness?.imports?.recent_errors);

  return (
    <section className="diagnostics-panel" aria-label="Диагностика backend">
      <div className="diagnostics-grid">
        <DiagnosticCard
          label="Readiness"
          value={readiness?.status || "недоступно"}
          detail={readiness ? `${readiness.service} ${readiness.version}, ${readiness.environment}` : "endpoint не ответил"}
          tone={readiness?.status === "ok" ? "ok" : "warn"}
        />
        <DiagnosticCard
          label="Миграции"
          value={stringField(readiness?.migrations, "status") || "-"}
          detail={stringField(readiness?.migrations, "current_revision") || stringField(readiness?.migrations, "error") || "нет данных"}
          tone={stringField(readiness?.migrations, "status") === "ok" ? "ok" : "warn"}
        />
        <DiagnosticCard
          label="Очередь events"
          value={`${activeQueue} активных`}
          detail={`total ${numberField(queueSummary, "total")}, terminal ${numberField(queueSummary, "terminal")}`}
          tone={activeQueue === 0 && failedEvents.length === 0 && staleEvents.length === 0 ? "ok" : "warn"}
        />
        <DiagnosticCard
          label="Импорты"
          value={`${importErrors.length} проблем`}
          detail={importErrors.length ? "последние ошибки видны ниже" : "критичных ошибок нет"}
          tone={importErrors.length ? "warn" : "ok"}
        />
      </div>

      {(failedEvents.length > 0 || staleEvents.length > 0 || queueErrors.length > 0 || importErrors.length > 0) && (
        <div className="diagnostics-details">
          {staleEvents.length > 0 && (
            <DiagnosticList
              title="Зависшие processing"
              items={staleEvents.map((event) => diagnosticEventText(event))}
            />
          )}
          {queueErrors.length > 0 && (
            <DiagnosticList
              title="Ошибки очереди"
              items={queueErrors.map((event) => diagnosticEventText(event))}
            />
          )}
          {failedEvents.length > 0 && (
            <DiagnosticList
              title="Pending/failed события"
              items={failedEvents.map((event) => diagnosticEventText(event))}
            />
          )}
          {importErrors.length > 0 && (
            <DiagnosticList
              title="Ошибки импортов"
              items={importErrors.map((item) => diagnosticImportText(item))}
            />
          )}
        </div>
      )}
    </section>
  );
}

function DiagnosticCard({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone: "ok" | "warn";
}) {
  return (
    <div className={`diagnostic-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <em>{detail}</em>
    </div>
  );
}

function DiagnosticList({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="diagnostics-list">
      <strong>{title}</strong>
      <ul>
        {items.map((item, index) => <li key={`${title}-${index}`}>{item}</li>)}
      </ul>
    </div>
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
            <ActivityPayload payload={item.payload} />
          </div>
          <time>{formatDateTime(item.created_at)}</time>
        </div>
      ))}
      {items.length === 0 && <div className="empty-state">Активности нет</div>}
    </div>
  );
}

function ActivityPayload({ payload }: { payload: Record<string, unknown> }) {
  const chips = auditPayloadChips(payload);
  if (chips.length === 0) return null;
  return (
    <div className="activity-details">
      {chips.map((chip) => (
        <span className="activity-chip" key={chip.label}>
          <strong>{chip.label}</strong>
          {chip.value}
        </span>
      ))}
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

function filterIncidents(
  incidents: AdminIncident[],
  filters: {
    status: IncidentStatusFilter;
    severity: IncidentSeverityFilter;
    source: string;
    search: string;
  },
) {
  const query = filters.search.trim().toLowerCase();
  return incidents.filter((incident) => {
    if (filters.status !== "all" && incident.status !== filters.status) return false;
    if (filters.severity !== "all" && incident.severity !== filters.severity) return false;
    if (filters.source !== "all" && incident.source !== filters.source) return false;
    if (!query) return true;
    return [
      incident.source,
      incident.severity,
      incident.status,
      incident.title,
      incident.message,
      incident.entity_type,
      incident.entity_id,
      incident.pending_event_id,
      incident.order_id,
      incident.order_item_id,
      incident.import_id,
      incident.external_ref,
    ].some((value) => value.toLowerCase().includes(query));
  });
}

function filterClientPoints(points: ClientPoint[], search: string, timeslotFilter: ClientTimeslotFilter) {
  const query = search.trim().toLowerCase();
  return points.filter((point) => {
    if (timeslotFilter === "custom" && !point.has_custom_timeslot) return false;
    if (timeslotFilter === "default" && point.has_custom_timeslot) return false;
    if (!query) return true;
    return [
      point.client_name,
      point.point_name,
      point.address,
      point.coordinates,
      point.representative,
      point.delivery_from,
      point.delivery_to,
    ].some((value) => value.toLowerCase().includes(query));
  });
}

function linkedIncidentText(incident: AdminIncident) {
  const parts = [
    incident.order_id ? `order ${shortId(incident.order_id)}` : "",
    incident.order_item_id ? `item ${shortId(incident.order_item_id)}` : "",
    incident.import_id ? `import ${shortId(incident.import_id)}` : "",
    incident.pending_event_id ? `event ${shortId(incident.pending_event_id)}` : "",
    incident.entity_type && incident.entity_id ? `${incident.entity_type} ${shortId(incident.entity_id)}` : "",
  ].filter(Boolean);
  return parts.join(" / ") || "-";
}

function linkedEventText(event: EventQueueEvent) {
  const parts = [
    event.linked_order_id ? `order ${compactId(event.linked_order_id)}` : "",
    event.linked_import_id ? `import ${compactId(event.linked_import_id)}` : "",
    event.linked_entity_type && event.linked_entity_id ? `${event.linked_entity_type} ${compactId(event.linked_entity_id)}` : "",
  ].filter(Boolean);
  return parts.join(" / ") || "-";
}

function incidentStatusLabel(value: string) {
  if (value === "open") return "Открыт";
  if (value === "in_progress") return "В работе";
  if (value === "manual_review") return "Ручная проверка";
  if (value === "resolved") return "Закрыт";
  if (value === "ignored") return "Игнор";
  if (value === "cancelled") return "Отменен";
  return value || "-";
}

function eventStatusLabel(value: string) {
  if (value === "pending") return "В очереди";
  if (value === "processing") return "В работе";
  if (value === "failed") return "Ошибка";
  if (value === "blocked") return "Блок";
  if (value === "completed") return "Готово";
  if (value === "cancelled") return "Отменено";
  if (value === "dead") return "Dead";
  return value || "-";
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
  if (kind === "deleteActive") return `Причина удаления активного заказа ${row.client}`;
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
  if (kind === "deleteActive") return `Удалить заказ ${row.client} из активных TakSklad? Заказ и позиции будут удалены из backend, строки Google будут поставлены в очередь на удаление. Если есть SkladBot-заявка, удалите ее вручную.`;
  if (kind === "restore") return `Восстановить заказ ${row.client} в активные?`;
  if (kind === "resyncSkladBot") return `Повторно подтянуть SkladBot номер для заказа ${row.client}?`;
  return `Отменить заказ ${row.client}?`;
}

function actionSuccessText(kind: OrderActionKind) {
  if (kind === "resync") return "Ресинк Google запущен";
  if (kind === "resetRescan") return "Заказ сброшен на пересканирование";
  if (kind === "completeWithoutKiz") return "Выбранные заказы закрыты как выполненные и отправлены в архив";
  if (kind === "archive") return "Заказ перенесен в архив без КИЗов";
  if (kind === "deleteActive") return "Активный заказ удален, строки Google поставлены в очередь на удаление";
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
  if (row.skladbot_status === "created") return "Создано";
  if (row.skladbot_status === "created_recovered") return "Восстановлено";
  if (row.skladbot_status === "found" || row.skladbot_request_number || row.skladbot_request_id) return "Найдено";
  if (row.skladbot_status === "not_found") return "Не найдено";
  if (row.skladbot_status === "multiple") return "Несколько";
  if (row.skladbot_status === "pending") return "Проверяется";
  if (row.skladbot_status === "create_failed") return "Ошибка создания";
  if (row.skladbot_status === "error") return "Ошибка";
  return "Без номера";
}

function returnSkladBotStatusLabel(value: string) {
  if (value === "queued") return "В очереди";
  if (value === "created") return "Создано";
  if (value === "created_recovered") return "Восстановлено";
  if (value === "blocked") return "Заблокировано";
  if (value === "create_failed") return "Ошибка создания";
  return value || "нет статуса";
}

function dryRunStatusLabel(value: string) {
  if (value === "ready") return "Ready";
  if (value === "queued") return "В очереди";
  if (value === "created") return "Создано";
  if (value === "recovered") return "Восстановлено";
  if (value === "blocked") return "Заблокировано";
  if (value === "create_failed") return "Ошибка создания";
  if (value === "already_linked") return "Уже есть WH-R";
  return value || "-";
}

function importDryRunSummaryText(item: ImportRecord) {
  const summary = readImportDryRunSummary(item);
  if (!summary) return "Не создан";
  const ready = Number(summary.ready ?? 0);
  const queued = Number(summary.queued ?? 0);
  const created = Number(summary.created ?? 0);
  const recovered = Number(summary.recovered ?? 0);
  const blocked = Number(summary.blocked ?? 0);
  const failed = Number(summary.create_failed ?? 0);
  const alreadyLinked = Number(summary.already_linked ?? 0);
  const mode = typeof summary.mode === "string" ? summary.mode : "dry_run";
  return `${mode}: ready ${ready}, queued ${queued}, created ${created + recovered}, blocked ${blocked + failed}, WH-R ${alreadyLinked}`;
}

function importIssuesText(item: ImportRecord) {
  const issues: string[] = [];
  const raw = item.raw_payload || {};
  const errors = stringArray(raw.errors);
  const invalidRows = numberField(raw, "invalid_rows");
  const duplicateRows = numberField(raw, "duplicate_rows");
  const googleError = stringField(raw, "google_sheets_error");
  const summary = readImportDryRunSummary(item);
  const blocked = summary ? numberField(summary, "blocked") + numberField(summary, "create_failed") : 0;
  if (invalidRows > 0) issues.push(`ошибочных строк ${invalidRows}`);
  if (duplicateRows > 0) issues.push(`повторов ${duplicateRows}`);
  if (googleError) issues.push(`Google: ${googleError}`);
  if (blocked > 0) issues.push(`SkladBot blocked ${blocked}`);
  issues.push(...errors.slice(0, 3));
  return issues.length ? issues.join("; ") : "-";
}

function readImportDryRunSummary(item: ImportRecord): Record<string, unknown> | null {
  const summary = item.raw_payload?.skladbot_dry_run;
  return summary && typeof summary === "object" && !Array.isArray(summary)
    ? summary as Record<string, unknown>
    : null;
}

function auditPayloadChips(payload: Record<string, unknown>) {
  const affectedOrderIds = stringArray(payload.affected_order_ids);
  const affectedItemIds = stringArray(payload.affected_item_ids);
  const chips = [
    { label: "Причина", value: stringField(payload, "reason") },
    { label: "Кто", value: stringField(payload, "actor") },
    { label: "Источник", value: stringField(payload, "source") },
    { label: "Idempotency", value: compactId(stringField(payload, "idempotency_key")) },
    { label: "Заказов", value: affectedOrderIds.length ? String(affectedOrderIds.length) : "" },
    { label: "Позиций", value: affectedItemIds.length ? String(affectedItemIds.length) : "" },
  ];
  return chips.filter((chip) => chip.value);
}

function diagnosticEventText(event: unknown) {
  const record = isRecord(event) ? event : {};
  const type = stringField(record, "event_type") || "event";
  const status = stringField(record, "status") || "-";
  const attempts = numberField(record, "attempts");
  const age = numberField(record, "age_seconds");
  const error = stringField(record, "last_error");
  return `${type}: ${status}, попыток ${attempts}, возраст ${age}s${error ? `, ${error}` : ""}`;
}

function diagnosticImportText(item: unknown) {
  const record = isRecord(item) ? item : {};
  const source = stringField(record, "filename") || stringField(record, "source") || "import";
  const status = stringField(record, "status") || "-";
  const rows = stringField(record, "rows");
  const errors = stringArray(record.errors).slice(0, 3);
  return `${source}: ${status}${rows ? `, ${rows}` : ""}${errors.length ? `, ${errors.join("; ")}` : ""}`;
}

function ageFromDate(value: string | null) {
  if (!value) return 0;
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return 0;
  return Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
}

function formatAgeSeconds(value: number) {
  const seconds = Math.max(0, Math.floor(value || 0));
  if (seconds < 60) return `${seconds} сек`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч`;
  return `${Math.floor(hours / 24)} д`;
}

function recordArray(value: unknown) {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function stringArray(value: unknown) {
  return Array.isArray(value)
    ? value.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
}

function stringField(record: unknown, key: string) {
  if (!isRecord(record)) return "";
  const value = record[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

function numberField(record: unknown, key: string) {
  if (!isRecord(record)) return 0;
  const value = record[key];
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
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

function formatClientProductQuantity(blocks: number, pieces: number) {
  const parts = [];
  if (blocks) parts.push(`${formatNumber(blocks)} блоков`);
  if (pieces) parts.push(`${formatNumber(pieces)} шт.`);
  return parts.length ? parts.join(" · ") : "-";
}

function formatMoney(value: number) {
  return value ? formatNumber(value) : "-";
}

function shortId(value: string) {
  return value.length > 8 ? value.slice(0, 8) : value;
}

function compactId(value: string) {
  if (value.length <= 18) return value;
  return `${value.slice(0, 10)}...${value.slice(-6)}`;
}

function maskLogin(value: string) {
  const digits = value.replace(/\D/g, "");
  if (digits.length <= 4) return value;
  return `+${digits.slice(0, 3)} ... ${digits.slice(-4)}`;
}

function roleLabel(value: string) {
  if (value === "admin") return "admin";
  if (value === "logistics_slots") return "логистика";
  return "read-only";
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
