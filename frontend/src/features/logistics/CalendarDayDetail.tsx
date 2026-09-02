import { useEffect, useState } from "react";
import { CheckCircle2, ChevronLeft, ChevronRight, Loader2, Lock, Plus } from "lucide-react";

import type {
  ClientPoint,
  LogisticsCalendarDay,
  LogisticsCalendarDayOrder,
  LogisticsCalendarDayOrders,
  LogisticsManualStopPayload,
  LogisticsManualStopRow,
} from "../../api";
import { ManualStopForm, manualStopValuesFromRow, type ManualStopFormValues } from "./ManualStopForm";

const WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"];

const LIFECYCLE_STATUS_LABELS: Record<LogisticsCalendarDayOrder["lifecycle_status"], string> = {
  returned: "Возврат",
  assembling: "В сборке",
  assembled: "Собран",
  shipped: "Отгружен",
  delivered: "Доставлен",
};

const LIFECYCLE_STATUS_CLASSES: Record<LogisticsCalendarDayOrder["lifecycle_status"], string> = {
  returned: "ret",
  assembling: "assembling",
  assembled: "assembled",
  shipped: "shipped",
  delivered: "delivered",
};

function formatDate(value: string) {
  if (!value) return "-";
  const [year, month, day] = value.slice(0, 10).split("-");
  return year && month && day ? `${day}.${month}.${year}` : value;
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("ru-RU").format(value);
}

/** Русское склонение по числу: 1 точка, 2 точки, 5 точек */
function plural(count: number, one: string, few: string, many: string) {
  const tens = Math.abs(count) % 100;
  const units = count % 10;
  if (tens > 10 && tens < 20) return many;
  if (units === 1) return one;
  if (units >= 2 && units <= 4) return few;
  return many;
}

function manualStopsSummary(stops: LogisticsManualStopRow[]) {
  const blocks = stops.reduce((sum, stop) => sum + stop.blocks, 0);
  const stopsWord = plural(stops.length, "ручная точка", "ручные точки", "ручных точек");
  const blocksWord = plural(blocks, "блок", "блока", "блоков");
  return `+ ${formatNumber(stops.length)} ${stopsWord}, ${formatNumber(blocks)} ${blocksWord}`;
}

export function CalendarDayDetail({
  day,
  dayOrders,
  loading,
  regionDirectoryEmpty,
  canAdminWrite,
  busyAction,
  canGoPrevDay,
  canGoNextDay,
  manualStopSearchResults,
  manualStopSearching,
  onPrevDay,
  onNextDay,
  onSaveDay,
  onDownload,
  onManualStopSearch,
  onManualStopSave,
  onManualStopDelete,
}: {
  day: LogisticsCalendarDay;
  dayOrders: LogisticsCalendarDayOrders | null;
  loading: boolean;
  regionDirectoryEmpty: boolean;
  canAdminWrite: boolean;
  busyAction: string;
  canGoPrevDay: boolean;
  canGoNextDay: boolean;
  manualStopSearchResults: ClientPoint[];
  manualStopSearching: boolean;
  onPrevDay: () => void;
  onNextDay: () => void;
  onSaveDay: (day: LogisticsCalendarDay, isNonWorking: boolean, reason: string) => void;
  onDownload: (zone: "city" | "region") => void;
  onManualStopSearch: (query: string) => void;
  onManualStopSave: (payload: LogisticsManualStopPayload) => void;
  onManualStopDelete: (id: string) => void;
}) {
  const [reason, setReason] = useState("");
  useEffect(() => {
    setReason(day.reason || "");
  }, [day.date, day.reason]);

  const busy = busyAction === `calendar-day:${day.date}`;
  const manualStopBusy = busyAction === `manual-stop:${day.date}`;

  const [zone, setZone] = useState<"city" | "region">("city");
  const [rowFilter, setRowFilter] = useState<"all" | "orders" | "returns" | "manual">("all");
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState("");
  const [editingValues, setEditingValues] = useState<ManualStopFormValues | null>(null);
  useEffect(() => {
    // Смена дня закрывает форму: точка привязана к дате, и открытая форма
    // сохранила бы её не в тот день
    setFormOpen(false);
    setEditingId("");
    setEditingValues(null);
  }, [day.date]);

  const dayOrdersMatchDay = dayOrders != null && dayOrders.date === day.date;
  const rows = (dayOrdersMatchDay ? dayOrders.orders : []).filter((row) => {
    if (row.zone !== zone) return false;
    if (rowFilter === "manual") return false;
    if (rowFilter === "orders") return !row.is_returned;
    if (rowFilter === "returns") return row.is_returned;
    return true;
  });
  const allManualStops = dayOrdersMatchDay ? (dayOrders.manual_stops ?? []) : [];
  const zoneManualStops = allManualStops.filter((stop) => stop.zone === zone);
  const manualStops = zoneManualStops.filter(() => rowFilter === "all" || rowFilter === "manual");
  const columnCount = canAdminWrite ? 8 : 7;

  function openNewStop() {
    setEditingId("");
    setEditingValues(null);
    setFormOpen(true);
    onManualStopSearch("");
  }

  function openEditStop(stop: LogisticsManualStopRow) {
    setEditingId(stop.id);
    setEditingValues(manualStopValuesFromRow(stop));
    setFormOpen(true);
    onManualStopSearch("");
  }

  function closeForm() {
    setFormOpen(false);
    setEditingId("");
    setEditingValues(null);
    onManualStopSearch("");
  }

  return (
    <div className="day-detail">
      <div className="day-detail-head">
        <div>
          <h3>{formatDate(day.date)}, {WEEKDAYS[day.weekday]?.toLowerCase() || "-"}</h3>
          <span className="panel-subtitle">
            Разбивка считается текущим справочником областных точек, за прошедшие даты
            она может отличаться от того, что ушло в XLSX в тот день
          </span>
        </div>
        <div className="day-detail-actions">
          <span className={`status-badge ${day.is_non_working ? "calendar-closed" : "queue-completed"}`}>
            {day.is_non_working ? "Логистика не работает" : "Рабочий день"}
          </span>
          <div className="day-nav">
            <button type="button" onClick={onPrevDay} disabled={!canGoPrevDay} aria-label="Предыдущий день"><ChevronLeft size={16} /></button>
            <button type="button" onClick={onNextDay} disabled={!canGoNextDay} aria-label="Следующий день"><ChevronRight size={16} /></button>
          </div>
        </div>
      </div>

      {regionDirectoryEmpty && (
        <p className="alert-bar" role="status">
          Справочник областных точек пуст, вся доставка временно считается городской
          Разбивка совпадает с XLSX, но области в ней не будет, пока справочник не восстановят
        </p>
      )}

      <div className="zone-cards">
        <section className="zone-card" role="group" aria-label="Город">
          <h4>Город <em>{formatNumber(day.city_orders)} из {formatNumber(day.orders_count)}</em></h4>
          <div className="zone-figures">
            <div><b>{formatNumber(day.city_orders)}</b><small>заказов</small></div>
            <div><b className="ret">{formatNumber(day.city_returns)}</b><small>возвратов</small></div>
            <div><b>{formatNumber(day.city_blocks)}</b><small>блоков</small></div>
          </div>
          {allManualStops.some((stop) => stop.zone === "city") && (
            <p className="zone-foot">{manualStopsSummary(allManualStops.filter((stop) => stop.zone === "city"))}</p>
          )}
        </section>

        <section className="zone-card region" role="group" aria-label="Область">
          <h4>Область <em>{formatNumber(day.region_orders)} из {formatNumber(day.orders_count)}</em></h4>
          <div className="zone-figures">
            <div><b>{formatNumber(day.region_orders)}</b><small>заказов</small></div>
            <div><b className="ret">{formatNumber(day.region_returns)}</b><small>возвратов</small></div>
            <div><b>{formatNumber(day.region_blocks)}</b><small>блоков</small></div>
          </div>
          {allManualStops.some((stop) => stop.zone === "region") && (
            <p className="zone-foot">{manualStopsSummary(allManualStops.filter((stop) => stop.zone === "region"))}</p>
          )}
        </section>

        <section className="zone-card total" role="group" aria-label="Итого за день">
          <h4>Итого за день</h4>
          <div className="zone-figures">
            <div><b>{formatNumber(day.orders_count)}</b><small>заказов</small></div>
            <div><b className="ret">{formatNumber(day.returned_orders)}</b><small>возвратов</small></div>
            <div><b>{formatNumber(day.planned_blocks)}</b><small>блоков</small></div>
          </div>
          <p className="zone-foot">
            Вне логистики: {formatNumber(day.excluded_orders)}, это самовывоз и заказы без остатка,
            они не входят ни в город, ни в область
          </p>
          {allManualStops.length > 0 && (
            <p className="zone-foot">
              {manualStopsSummary(allManualStops)}, в счёт заказов и блоков они не входят
            </p>
          )}
        </section>
      </div>

      <div className="list-panel">
        <div className="list-tabs" role="tablist" aria-label="Зона доставки">
          <button
            type="button"
            role="tab"
            aria-selected={zone === "city"}
            onClick={() => setZone("city")}
          >
            <i className="dot" />Город <span className="tab-count">{day.city_orders} + {day.city_returns} возвр.</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={zone === "region"}
            onClick={() => setZone("region")}
          >
            <i className="dot region" />Область <span className="tab-count">{day.region_orders} + {day.region_returns} возвр.</span>
          </button>
        </div>

        <div className="list-head">
          <h4>
            <span className="count">
              {zone === "city"
                ? `${day.city_orders} заказов и ${day.city_returns} возвратов · ${day.city_blocks} блоков`
                : `${day.region_orders} заказов и ${day.region_returns} возвратов · ${day.region_blocks} блоков`}
            </span>
          </h4>
          <div className="list-tools">
            <div className="chips">
              <button type="button" aria-pressed={rowFilter === "all"} onClick={() => setRowFilter("all")}>Все</button>
              <button type="button" aria-pressed={rowFilter === "orders"} onClick={() => setRowFilter("orders")}>Заказы</button>
              <button type="button" aria-pressed={rowFilter === "returns"} onClick={() => setRowFilter("returns")}>Возвраты</button>
              <button type="button" aria-pressed={rowFilter === "manual"} onClick={() => setRowFilter("manual")}>
                Ручные <span className="tab-count">{zoneManualStops.length}</span>
              </button>
            </div>
            {canAdminWrite && (
              <button className="ghost-button sm" type="button" onClick={openNewStop} disabled={formOpen && !editingId}>
                <Plus size={14} />Добавить точку
              </button>
            )}
            <button className="ghost-button sm" type="button" onClick={() => onDownload(zone)}>
              {zone === "city" ? "Выгрузить XLSX город" : "Выгрузить XLSX область"}
            </button>
            <span className="panel-subtitle">Возвраты в XLSX не входят</span>
          </div>
        </div>

        {canAdminWrite && formOpen && (
          <ManualStopForm
            serviceDate={day.date}
            editingId={editingId}
            initialValues={editingValues}
            busy={manualStopBusy}
            searchResults={manualStopSearchResults}
            searching={manualStopSearching}
            onSearch={onManualStopSearch}
            onSubmit={(payload) => {
              onManualStopSave(payload);
              closeForm();
            }}
            onCancel={closeForm}
          />
        )}

        <div className="table-scroll" role="tabpanel">
          <table className="data-table">
            <thead>
              <tr>
                <th>Клиент</th><th>Товары</th><th className="numeric-cell">Блоки</th>
                <th>Окно</th><th>Статус</th><th>SkladBot / Smartup</th><th className="numeric-cell">Сумма</th>
                {canAdminWrite && <th>Действия</th>}
              </tr>
            </thead>
            <tbody>
              {!loading && rows.map((row) => (
                <tr key={row.order_id} className={row.is_returned ? "ret-row" : ""}>
                  <td>
                    <strong className="cell-title">{row.client}</strong>
                    <span className="cell-sub">{row.address}</span>
                    {row.representative && <span className="cell-sub">{row.representative}</span>}
                  </td>
                  <td>
                    <strong className="cell-title">{row.products || "-"}</strong>
                    {row.source_file && <span className="cell-sub">{row.source_file}</span>}
                  </td>
                  <td className="numeric-cell">
                    <strong>{row.scanned_blocks}/{row.quantity_blocks}</strong>
                    <span className="cell-sub">осталось {row.remaining_blocks}</span>
                  </td>
                  <td>
                    <span className="cell-sub">{row.delivery_from || "-"}</span>
                    <span className="cell-sub">{row.delivery_to || ""}</span>
                  </td>
                  <td>
                    <span className={`status-badge ${LIFECYCLE_STATUS_CLASSES[row.lifecycle_status]}`}>
                      {LIFECYCLE_STATUS_LABELS[row.lifecycle_status]}
                    </span>
                  </td>
                  <td>
                    <span className="cell-sub">{row.skladbot_request_number || "-"}</span>
                    <span className="cell-sub">{row.smartup_id || ""}</span>
                  </td>
                  <td className="numeric-cell">{formatNumber(row.line_total)}</td>
                  {canAdminWrite && <td />}
                </tr>
              ))}
              {!loading && manualStops.map((stop) => (
                <tr key={stop.id} className="manual-row">
                  <td>
                    <strong className="cell-title">{stop.point_name || stop.client}</strong>
                    <span className="cell-sub">{stop.address}</span>
                    {stop.representative && <span className="cell-sub">{stop.representative}</span>}
                  </td>
                  <td>
                    <strong className="cell-title">{stop.comment || "-"}</strong>
                    <span className="cell-sub">{stop.coordinates}</span>
                  </td>
                  <td className="numeric-cell"><strong>{formatNumber(stop.blocks)}</strong></td>
                  <td>
                    <span className="cell-sub">{stop.delivery_from || "-"}</span>
                    <span className="cell-sub">{stop.delivery_to || ""}</span>
                  </td>
                  <td><span className="status-badge manual">Ручная точка</span></td>
                  <td><span className="cell-sub">-</span></td>
                  <td className="numeric-cell">-</td>
                  {canAdminWrite && (
                    <td>
                      <div className="row-actions">
                        <button type="button" className="ghost-button sm" onClick={() => openEditStop(stop)}>Правка</button>
                        <button
                          type="button"
                          className="ghost-button sm"
                          onClick={() => onManualStopDelete(stop.id)}
                          disabled={manualStopBusy}
                        >
                          Убрать
                        </button>
                      </div>
                    </td>
                  )}
                </tr>
              ))}
              {!loading && rows.length === 0 && manualStops.length === 0 && (
                <tr><td colSpan={columnCount} className="empty-state">Заказов в этой зоне за день нет</td></tr>
              )}
              {loading && (
                <tr><td colSpan={columnCount} className="empty-state">Загрузка заказов дня</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="day-detail-controls">
        <label className="admin-reason-field">
          <span>Причина / комментарий</span>
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={2}
            disabled={!canAdminWrite}
            placeholder="Например: праздник, логистика не работает"
          />
        </label>
        {canAdminWrite && (
          <div className="action-buttons">
            <button className="ghost-button" onClick={() => onSaveDay(day, true, reason || "Нерабочий день логистики")} disabled={Boolean(busyAction)}>
              {busy ? <Loader2 className="spin" size={16} /> : <Lock size={16} />}
              Не работает
            </button>
            <button className="ghost-button" onClick={() => onSaveDay(day, false, reason || "Рабочий день логистики")} disabled={Boolean(busyAction)}>
              {busy ? <Loader2 className="spin" size={16} /> : <CheckCircle2 size={16} />}
              Работает
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
