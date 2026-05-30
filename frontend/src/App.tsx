import {
  Activity,
  AlertCircle,
  BarChart3,
  Box,
  Check,
  CheckCircle2,
  CircleDot,
  ClipboardList,
  Database,
  FileSpreadsheet,
  Loader2,
  PackageCheck,
  RefreshCw,
  Search,
  Send,
  Server,
} from "lucide-react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  ApiConfig,
  DayReport,
  ImportRecord,
  Order,
  OrderItem,
  completeOrder,
  createScan,
  defaultApiUrl,
  getDayReport,
  listActiveOrders,
  listImports,
} from "./api";
import "./styles.css";

type Tab = "orders" | "report" | "imports";

const CONFIG_KEY = "taksklad-web-config";
const SAME_ORIGIN_API_LABEL = "same-origin /api";

function loadConfig(): ApiConfig {
  try {
    const stored = localStorage.getItem(CONFIG_KEY);
    if (stored) {
      const parsed = JSON.parse(stored) as Partial<ApiConfig>;
      return {
        apiUrl: parsed.apiUrl || defaultApiUrl(),
        token: parsed.token || "",
      };
    }
  } catch {
    // Keep a working default if localStorage contains an old draft shape.
  }
  return { apiUrl: defaultApiUrl(), token: "" };
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function statusLabel(value: string) {
  if (["completed", "done", "closed"].includes(value)) return "Готово";
  if (value === "not_completed") return "В работе";
  return value || "Без статуса";
}

function progress(order: Order) {
  const planned = order.items.reduce((sum, item) => sum + Math.max(0, item.quantity_blocks || 0), 0);
  const scanned = order.items.reduce((sum, item) => sum + Math.max(0, item.scanned_blocks || 0), 0);
  return { planned, scanned, remaining: Math.max(0, planned - scanned) };
}

function formatDate(value: string | null) {
  if (!value) return "-";
  const [year, month, day] = value.split("-");
  return year && month && day ? `${day}.${month}.${year}` : value;
}

function App() {
  const [config, setConfig] = useState<ApiConfig>(() => loadConfig());
  const [draftConfig, setDraftConfig] = useState<ApiConfig>(() => loadConfig());
  const [orders, setOrders] = useState<Order[]>([]);
  const [imports, setImports] = useState<ImportRecord[]>([]);
  const [report, setReport] = useState<DayReport | null>(null);
  const [reportDate, setReportDate] = useState(todayIso());
  const [selectedOrderId, setSelectedOrderId] = useState<string>("");
  const [selectedItemId, setSelectedItemId] = useState<string>("");
  const [scanCode, setScanCode] = useState("");
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState<Tab>("orders");
  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selectedOrder = useMemo(
    () => orders.find((order) => order.id === selectedOrderId) ?? orders[0],
    [orders, selectedOrderId],
  );

  const selectedItem = useMemo(() => {
    if (!selectedOrder) return undefined;
    return selectedOrder.items.find((item) => item.id === selectedItemId) ?? selectedOrder.items[0];
  }, [selectedOrder, selectedItemId]);

  const filteredOrders = useMemo(() => {
    const value = search.trim().toLowerCase();
    if (!value) return orders;
    return orders.filter((order) => {
      const request = `${order.skladbot_request_number} ${order.skladbot_request_id}`.toLowerCase();
      return [order.client, order.address, order.payment_type, request]
        .filter(Boolean)
        .some((text) => text.toLowerCase().includes(value));
    });
  }, [orders, search]);

  const totals = useMemo(() => {
    return orders.reduce(
      (acc, order) => {
        const orderProgress = progress(order);
        acc.orders += 1;
        acc.blocks += orderProgress.planned;
        acc.scanned += orderProgress.scanned;
        acc.remaining += orderProgress.remaining;
        return acc;
      },
      { orders: 0, blocks: 0, scanned: 0, remaining: 0 },
    );
  }, [orders]);

  async function refreshAll(activeConfig = config) {
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const [nextOrders, nextReport, nextImports] = await Promise.all([
        listActiveOrders(activeConfig),
        getDayReport(activeConfig, reportDate),
        listImports(activeConfig),
      ]);
      setOrders(nextOrders);
      setReport(nextReport);
      setImports(nextImports);
      if (nextOrders.length > 0 && !nextOrders.some((order) => order.id === selectedOrderId)) {
        setSelectedOrderId(nextOrders[0].id);
        setSelectedItemId(nextOrders[0].items[0]?.id ?? "");
      }
      setNotice(`Обновлено: ${new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`);
    } catch (refreshError) {
      setError(refreshError instanceof Error ? refreshError.message : "Не удалось загрузить данные");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refreshAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (selectedOrder && !selectedItemId) {
      setSelectedItemId(selectedOrder.items[0]?.id ?? "");
    }
  }, [selectedOrder, selectedItemId]);

  function saveConfig(event: FormEvent) {
    event.preventDefault();
    const nextConfig = {
      apiUrl: draftConfig.apiUrl.replace(/\/$/, ""),
      token: "",
    };
    localStorage.setItem(CONFIG_KEY, JSON.stringify(nextConfig));
    setConfig(nextConfig);
    void refreshAll(nextConfig);
  }

  async function submitScan(event: FormEvent) {
    event.preventDefault();
    if (!selectedItem || !scanCode.trim()) return;
    setBusyAction("scan");
    setError("");
    setNotice("");
    try {
      await createScan(config, selectedItem.id, scanCode);
      setScanCode("");
      await refreshAll();
      setNotice("КИЗ записан");
    } catch (scanError) {
      setError(scanError instanceof Error ? scanError.message : "Не удалось записать КИЗ");
    } finally {
      setBusyAction("");
    }
  }

  async function finishOrder() {
    if (!selectedOrder) return;
    setBusyAction("complete");
    setError("");
    setNotice("");
    try {
      await completeOrder(config, selectedOrder.id);
      await refreshAll();
      setNotice("Заказ завершен");
    } catch (completeError) {
      setError(completeError instanceof Error ? completeError.message : "Не удалось завершить заказ");
    } finally {
      setBusyAction("");
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/taksklad.png" alt="" />
          <div>
            <strong>TakSklad</strong>
            <span>web draft</span>
          </div>
        </div>
        <nav className="nav-tabs">
          <button className={tab === "orders" ? "active" : ""} onClick={() => setTab("orders")}>
            <ClipboardList size={18} />
            Заказы
          </button>
          <button className={tab === "report" ? "active" : ""} onClick={() => setTab("report")}>
            <BarChart3 size={18} />
            Отчет
          </button>
          <button className={tab === "imports" ? "active" : ""} onClick={() => setTab("imports")}>
            <FileSpreadsheet size={18} />
            Импорты
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
            <p>Склад сегодня</p>
            <h1>Заказы, КИЗ и отчеты</h1>
          </div>
          <button className="icon-button" onClick={() => void refreshAll()} disabled={loading} title="Обновить">
            {loading ? <Loader2 className="spin" size={18} /> : <RefreshCw size={18} />}
            Обновить
          </button>
        </header>

        <form className="connection-bar" onSubmit={saveConfig}>
          <label>
            <Server size={16} />
            <input
              value={draftConfig.apiUrl}
              onChange={(event) => setDraftConfig({ ...draftConfig, apiUrl: event.target.value })}
              placeholder={SAME_ORIGIN_API_LABEL}
            />
          </label>
          <button type="submit">
            <CheckCircle2 size={16} />
            Применить
          </button>
        </form>

        {(error || notice) && (
          <div className={error ? "message error" : "message success"}>
            {error ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
            <span>{error || notice}</span>
          </div>
        )}

        <section className="stats-row">
          <Metric icon={<ClipboardList size={20} />} label="Активных заказов" value={totals.orders} />
          <Metric icon={<Box size={20} />} label="Блоков всего" value={totals.blocks} />
          <Metric icon={<PackageCheck size={20} />} label="Отсканировано" value={totals.scanned} />
          <Metric icon={<Activity size={20} />} label="Осталось" value={totals.remaining} tone="warn" />
        </section>

        {tab === "orders" && (
          <section className="orders-layout">
            <div className="orders-panel">
              <div className="panel-header">
                <h2>Активные заказы</h2>
                <label className="search-box">
                  <Search size={16} />
                  <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск" />
                </label>
              </div>
              <div className="orders-list">
                {filteredOrders.map((order) => (
                  <OrderRow
                    key={order.id}
                    order={order}
                    selected={selectedOrder?.id === order.id}
                    onClick={() => {
                      setSelectedOrderId(order.id);
                      setSelectedItemId(order.items[0]?.id ?? "");
                    }}
                  />
                ))}
                {filteredOrders.length === 0 && <div className="empty-state">Заказов нет</div>}
              </div>
            </div>

            <div className="detail-panel">
              {selectedOrder ? (
                <>
                  <div className="detail-head">
                    <div>
                      <div className="muted-row">{formatDate(selectedOrder.order_date)} / {selectedOrder.payment_type}</div>
                      <h2>{selectedOrder.client}</h2>
                      <p>{selectedOrder.address}</p>
                    </div>
                    <button onClick={finishOrder} disabled={busyAction === "complete"}>
                      {busyAction === "complete" ? <Loader2 className="spin" size={17} /> : <Check size={17} />}
                      Завершить
                    </button>
                  </div>

                  <div className="request-row">
                    <Database size={17} />
                    <span>Заявка SkladBot</span>
                    <strong>{selectedOrder.skladbot_request_number || "-"}</strong>
                  </div>

                  <div className="items-grid">
                    {selectedOrder.items.map((item) => (
                      <ItemButton
                        key={item.id}
                        item={item}
                        selected={selectedItem?.id === item.id}
                        onClick={() => setSelectedItemId(item.id)}
                      />
                    ))}
                  </div>

                  {selectedItem && (
                    <form className="scan-form" onSubmit={submitScan}>
                      <label>
                        <CircleDot size={17} />
                        <input
                          value={scanCode}
                          onChange={(event) => setScanCode(event.target.value)}
                          placeholder="КИЗ"
                          autoComplete="off"
                        />
                      </label>
                      <button type="submit" disabled={!scanCode.trim() || busyAction === "scan"}>
                        {busyAction === "scan" ? <Loader2 className="spin" size={17} /> : <Send size={17} />}
                        Записать
                      </button>
                    </form>
                  )}

                  <div className="codes-list">
                    <h3>Последние КИЗ</h3>
                    <div>
                      {(selectedItem?.scan_codes ?? []).slice(-12).reverse().map((code) => (
                        <span key={code}>{code}</span>
                      ))}
                      {(selectedItem?.scan_codes ?? []).length === 0 && <em>Нет сканов</em>}
                    </div>
                  </div>
                </>
              ) : (
                <div className="empty-state">Выберите заказ</div>
              )}
            </div>
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
              headers={["Дата", "Источник", "Статус", "Строк", "Импортировано"]}
              rows={imports.map((item) => [
                new Date(item.created_at).toLocaleString("ru-RU"),
                item.source,
                item.status,
                String(item.rows_total),
                String(item.rows_imported),
              ])}
            />
          </section>
        )}
      </main>
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
  value: number;
  tone?: "warn";
}) {
  return (
    <div className={`metric ${tone === "warn" ? "warn" : ""}`}>
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function OrderRow({ order, selected, onClick }: { order: Order; selected: boolean; onClick: () => void }) {
  const orderProgress = progress(order);
  const percent = orderProgress.planned > 0 ? Math.round((orderProgress.scanned / orderProgress.planned) * 100) : 0;
  return (
    <button className={`order-row ${selected ? "selected" : ""}`} onClick={onClick}>
      <div>
        <strong>{order.client}</strong>
        <span>{order.address}</span>
      </div>
      <div className="order-meta">
        <span>{order.skladbot_request_number || order.payment_type}</span>
        <strong>{orderProgress.scanned}/{orderProgress.planned}</strong>
      </div>
      <div className="progress-track">
        <i style={{ width: `${Math.min(100, percent)}%` }} />
      </div>
    </button>
  );
}

function ItemButton({ item, selected, onClick }: { item: OrderItem; selected: boolean; onClick: () => void }) {
  const percent = item.quantity_blocks > 0 ? Math.round((item.scanned_blocks / item.quantity_blocks) * 100) : 0;
  return (
    <button className={`item-button ${selected ? "selected" : ""}`} onClick={onClick}>
      <span>{statusLabel(item.status)}</span>
      <strong>{item.product}</strong>
      <em>{item.scanned_blocks}/{item.quantity_blocks} блоков</em>
      <div className="progress-track">
        <i style={{ width: `${Math.min(100, percent)}%` }} />
      </div>
    </button>
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

export default App;

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(<App />);
}
