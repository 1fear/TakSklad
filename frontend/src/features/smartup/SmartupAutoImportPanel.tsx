import { AlertCircle, CheckCircle2, PackageCheck, RefreshCw, Server } from "lucide-react";
import type { ReactNode } from "react";
import { useMemo } from "react";

import type { SmartupAutoImportHistory, SmartupAutoImportRun } from "../../api";

export default function SmartupAutoImportPanel({ history }: { history: SmartupAutoImportHistory | null }) {
  const runs = history?.runs ?? [];
  const eventById = useMemo(
    () => new Map((history?.events ?? []).map((event) => [event.id, event])),
    [history],
  );
  const summary = history?.summary ?? {};
  const failedRuns = runs.filter((run) => run.status === "failed").length;
  const processingRuns = runs.filter((run) => run.status === "processing").length;

  return (
    <section className="table-panel">
      <div className="panel-header table-panel-header">
        <div>
          <h2>Smartup auto import</h2>
          <span className="panel-subtitle">Последние запуски, файлы выгрузки, ошибки и созданные заказы</span>
        </div>
        <span className="table-muted">обновлено {formatDateTime(history?.generated_at ?? null)}</span>
      </div>

      <section className="stats-row compact">
        <Metric icon={<RefreshCw size={20} />} label="Запусков" value={numberField(summary, "total")} />
        <Metric icon={<CheckCircle2 size={20} />} label="Готово" value={numberField(summary, "completed")} />
        <Metric icon={<PackageCheck size={20} />} label="Заказов" value={numberField(summary, "orders_created")} />
        <Metric icon={<AlertCircle size={20} />} label="Ошибок" value={failedRuns} tone={failedRuns ? "warn" : undefined} />
        <Metric icon={<Server size={20} />} label="В работе" value={processingRuns} tone={processingRuns ? "warn" : undefined} />
      </section>

      <div className="data-table-wrap smartup-table-wrap">
        <table className="data-table smartup-table">
          <thead><tr><th>Слот</th><th>Статус</th><th>Файл</th><th>Отгрузка</th><th>Заказы</th><th>SkladBot</th><th>Ошибка</th><th>JSON</th></tr></thead>
          <tbody>
            {runs.map((run) => {
              const event = eventById.get(run.id);
              return (
                <tr key={run.id}>
                  <td><strong className="cell-title">{formatDate(run.export_date)} · {run.slot || "-"}</strong><span className="table-muted cell-sub">{formatDateTime(run.completed_at || run.failed_at || run.updated_at || run.created_at)}</span></td>
                  <td><span className={`status-badge queue-${run.status}`}>{smartupRunStatusLabel(run.status)}</span><span className="table-muted cell-sub">часть {run.part ?? "-"}</span></td>
                  <td><strong className="cell-title">{run.filename || "-"}</strong><span className="table-muted cell-sub clamp-text">{run.export_path || run.audit_path || "-"}</span></td>
                  <td>{smartupDeliveryDatesText(run)}</td>
                  <td><strong className="cell-title">{run.orders_created} создано</strong><span className="table-muted cell-sub">выбрано {run.selected_orders}, строк {run.rows}, дублей {run.duplicate_rows}</span></td>
                  <td><strong className="cell-title">{smartupSkladbotStatusText(run)}</strong><span className="table-muted cell-sub">{smartupLogisticsText(run)}</span></td>
                  <td><span className="table-muted cell-sub clamp-text">{run.error || "-"}</span></td>
                  <td><details className="json-preview"><summary>JSON</summary><pre>{JSON.stringify(event?.raw_payload ?? run, null, 2)}</pre></details></td>
                </tr>
              );
            })}
            {runs.length === 0 && <tr><td colSpan={8}>Smartup запусков еще нет</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Metric({ icon, label, value, tone }: { icon: ReactNode; label: string; value: number | string; tone?: "warn" }) {
  return <div className={`metric ${tone === "warn" ? "warn" : ""}`}>{icon}<span>{label}</span><strong>{typeof value === "number" ? new Intl.NumberFormat("ru-RU").format(value) : value}</strong></div>;
}

function smartupRunStatusLabel(value: string) {
  if (value === "completed") return "Готово";
  if (value === "failed") return "Ошибка";
  if (value === "processing") return "В работе";
  if (value === "pending") return "В очереди";
  if (value === "cancelled") return "Отменено";
  return value || "-";
}

function smartupDeliveryDatesText(run: SmartupAutoImportRun) {
  if (!run.delivery_dates.length) return "-";
  return run.delivery_dates.map(formatDate).join(", ");
}

function smartupSkladbotStatusText(run: SmartupAutoImportRun) {
  if (run.skladbot_status === "completed") return "Создание выполнено";
  if (run.skladbot_status === "skipped") return "Пропущено";
  if (run.skladbot_status === "failed") return "Ошибка";
  if (run.skladbot_status) return run.skladbot_status;
  return run.imports_count ? `${run.imports_count} импортов` : "-";
}

function smartupLogisticsText(run: SmartupAutoImportRun) {
  const reports = run.logistics_reports;
  if (!reports.length) return "отчет логистики: -";
  const sent = reports.filter((item) => stringField(item, "status") === "sent").length;
  const failed = reports.filter((item) => stringField(item, "status") === "failed").length;
  return `отчет логистики: sent ${sent}, failed ${failed}`;
}

function formatDate(value: string | null) {
  if (!value) return "-";
  const [year, month, day] = value.slice(0, 10).split("-");
  return year && month && day ? `${day}.${month}.${year}` : value;
}

function formatDateTime(value: string | null) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("ru-RU");
}

function numberField(record: unknown, key: string) {
  if (!record || typeof record !== "object" || Array.isArray(record)) return 0;
  const value = (record as Record<string, unknown>)[key];
  return typeof value === "number" && Number.isFinite(value) ? value : Number(value || 0) || 0;
}

function stringField(record: unknown, key: string) {
  if (!record || typeof record !== "object" || Array.isArray(record)) return "";
  const value = (record as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}
