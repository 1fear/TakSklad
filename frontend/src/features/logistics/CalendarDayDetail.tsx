import { useEffect, useState } from "react";
import { CheckCircle2, ChevronLeft, ChevronRight, Loader2, Lock } from "lucide-react";

import type { LogisticsCalendarDay, LogisticsCalendarDayOrders } from "../../api";

const WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"];

function formatDate(value: string) {
  if (!value) return "-";
  const [year, month, day] = value.slice(0, 10).split("-");
  return year && month && day ? `${day}.${month}.${year}` : value;
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("ru-RU").format(value);
}

export function CalendarDayDetail({
  day,
  dayOrders,
  loading,
  regionDirectoryEmpty,
  canAdminWrite,
  busyAction,
  onPrevDay,
  onNextDay,
  onSaveDay,
  onDownload,
}: {
  day: LogisticsCalendarDay;
  dayOrders: LogisticsCalendarDayOrders | null;
  loading: boolean;
  regionDirectoryEmpty: boolean;
  canAdminWrite: boolean;
  busyAction: string;
  onPrevDay: () => void;
  onNextDay: () => void;
  onSaveDay: (day: LogisticsCalendarDay, isNonWorking: boolean, reason: string) => void;
  onDownload: (zone: "city" | "region") => void;
}) {
  const [reason, setReason] = useState("");
  useEffect(() => {
    setReason(day.reason || "");
  }, [day.date, day.reason]);

  const busy = busyAction === `calendar-day:${day.date}`;

  const [zone, setZone] = useState<"city" | "region">("city");
  const [rowFilter, setRowFilter] = useState<"all" | "orders" | "returns">("all");
  const rows = (dayOrders?.orders ?? []).filter((row) => {
    if (row.zone !== zone) return false;
    if (rowFilter === "orders") return !row.is_returned;
    if (rowFilter === "returns") return row.is_returned;
    return true;
  });

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
            <button type="button" onClick={onPrevDay} aria-label="Предыдущий день"><ChevronLeft size={16} /></button>
            <button type="button" onClick={onNextDay} aria-label="Следующий день"><ChevronRight size={16} /></button>
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
        </section>

        <section className="zone-card region" role="group" aria-label="Область">
          <h4>Область <em>{formatNumber(day.region_orders)} из {formatNumber(day.orders_count)}</em></h4>
          <div className="zone-figures">
            <div><b>{formatNumber(day.region_orders)}</b><small>заказов</small></div>
            <div><b className="ret">{formatNumber(day.region_returns)}</b><small>возвратов</small></div>
            <div><b>{formatNumber(day.region_blocks)}</b><small>блоков</small></div>
          </div>
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
            </div>
            <button className="ghost-button sm" type="button" onClick={() => onDownload(zone)}>
              {zone === "city" ? "Выгрузить XLSX город" : "Выгрузить XLSX область"}
            </button>
          </div>
        </div>

        <div className="table-scroll" role="tabpanel">
          <table className="data-table">
            <thead>
              <tr>
                <th>Клиент</th><th>Товары</th><th className="numeric-cell">Блоки</th>
                <th>Окно</th><th>Статус</th><th>SkladBot / Smartup</th><th className="numeric-cell">Сумма</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
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
                    <span className={`status-badge ${row.is_returned ? "ret" : ""}`}>
                      {row.is_returned ? "Возврат" : row.status === "completed" ? "Завершён" : "В работе"}
                    </span>
                  </td>
                  <td>
                    <span className="cell-sub">{row.skladbot_request_number || "-"}</span>
                    <span className="cell-sub">{row.smartup_id || ""}</span>
                  </td>
                  <td className="numeric-cell">{formatNumber(row.line_total)}</td>
                </tr>
              ))}
              {rows.length === 0 && !loading && (
                <tr><td colSpan={7} className="empty-state">Заказов в этой зоне за день нет</td></tr>
              )}
              {loading && (
                <tr><td colSpan={7} className="empty-state">Загрузка заказов дня</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {canAdminWrite && (
        <div className="day-detail-controls">
          <label className="admin-reason-field">
            <span>Причина / комментарий</span>
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              placeholder="Например: праздник, логистика не работает"
            />
          </label>
          <div className="action-buttons">
            <button className="ghost-button" onClick={() => onSaveDay(day, true, reason || "Нерабочий день логистики")} disabled={busy}>
              {busy ? <Loader2 className="spin" size={16} /> : <Lock size={16} />}
              Не работает
            </button>
            <button className="ghost-button" onClick={() => onSaveDay(day, false, reason || "Рабочий день логистики")} disabled={busy}>
              {busy ? <Loader2 className="spin" size={16} /> : <CheckCircle2 size={16} />}
              Работает
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
