import { AlertCircle, CheckCircle2, Loader2, PackageCheck, RefreshCw, RotateCcw, Search, Undo2 } from "lucide-react";
import type { FormEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  type ApiConfig,
  type KizAvailability,
  type Order,
  completeWarehouseOrder,
  createScan,
  listActiveOrders,
  lookupKizAvailability,
  lookupReturn,
  markReturn,
  undoScan,
} from "../../api";

type WarehousePanelProps = {
  config: ApiConfig;
  canWrite: boolean;
  actor: string;
  onError: (error: unknown, fallback: string) => void;
  onNotice: (message: string) => void;
};

const WORKSTATION_ID = "taksklad-web";

export default function WarehousePanel({ config, canWrite, actor, onError, onNotice }: WarehousePanelProps) {
  const scanInputRef = useRef<HTMLInputElement>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [selectedOrderId, setSelectedOrderId] = useState("");
  const [selectedItemId, setSelectedItemId] = useState("");
  const [scanCode, setScanCode] = useState("");
  const [availability, setAvailability] = useState<KizAvailability | null>(null);
  const [returnLookup, setReturnLookup] = useState("");
  const [returnOrder, setReturnOrder] = useState<Order | null>(null);
  const [returnReference, setReturnReference] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");

  const selectedOrder = useMemo(
    () => orders.find((order) => order.id === selectedOrderId) ?? orders[0],
    [orders, selectedOrderId],
  );
  const selectedItem = useMemo(
    () => selectedOrder?.items.find((item) => item.id === selectedItemId) ?? selectedOrder?.items[0],
    [selectedOrder, selectedItemId],
  );
  const orderComplete = Boolean(selectedOrder?.items.length)
    && selectedOrder!.items.every((item) => item.quantity_blocks > 0 && item.scanned_blocks >= item.quantity_blocks);

  useEffect(() => {
    void refreshOrders(false);
    // The authenticated config is stable for the mounted workspace session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.apiUrl, config.csrfToken, config.token]);

  async function refreshOrders(showNotice = true) {
    setLoading(true);
    try {
      const next = await listActiveOrders(config);
      setOrders(next);
      setSelectedOrderId((current) => next.some((order) => order.id === current) ? current : (next[0]?.id ?? ""));
      setSelectedItemId((current) => next.some((order) => order.items.some((item) => item.id === current)) ? current : (next[0]?.items[0]?.id ?? ""));
      if (showNotice) onNotice(`Активные заказы обновлены: ${next.length}`);
    } catch (error) {
      onError(error, "Не удалось загрузить активные заказы");
    } finally {
      setLoading(false);
    }
  }

  function chooseOrder(orderId: string) {
    const order = orders.find((value) => value.id === orderId);
    setSelectedOrderId(orderId);
    setSelectedItemId(order?.items[0]?.id ?? "");
    setAvailability(null);
    setScanCode("");
    queueMicrotask(() => scanInputRef.current?.focus());
  }

  async function submitScan(event: FormEvent) {
    event.preventDefault();
    const code = scannerCode(scanCode);
    if (!selectedItem || !code || !canWrite) return;
    setBusy("scan");
    setAvailability(null);
    try {
      const check = await lookupKizAvailability(config, code, selectedItem.id);
      setAvailability(check);
      if (!check.available) throw new Error(check.reason || "КИЗ недоступен для выбранной позиции");
      await createScan(config, {
        order_item_id: selectedItem.id,
        code,
        workstation_id: WORKSTATION_ID,
        scanned_by: actor || "web",
      });
      setScanCode("");
      setAvailability(null);
      await refreshOrders(false);
      onNotice("КИЗ сохранён в PostgreSQL");
      queueMicrotask(() => scanInputRef.current?.focus());
    } catch (error) {
      onError(error, "Не удалось сохранить КИЗ");
    } finally {
      setBusy("");
    }
  }

  async function removeLastCode() {
    const code = selectedItem?.scan_codes.at(-1) ?? "";
    if (!selectedItem || !code || !canWrite) return;
    if (!window.confirm(`Отменить последний КИЗ ${shortCode(code)}?`)) return;
    setBusy("undo");
    try {
      await undoScan(config, {
        order_item_id: selectedItem.id,
        code,
        workstation_id: WORKSTATION_ID,
        actor: actor || "web",
      });
      await refreshOrders(false);
      onNotice("Последний КИЗ отменён в PostgreSQL");
    } catch (error) {
      onError(error, "Не удалось отменить последний КИЗ");
    } finally {
      setBusy("");
    }
  }

  async function completeOrder() {
    if (!selectedOrder || !orderComplete || !canWrite) return;
    if (!window.confirm(`Завершить заказ ${orderLabel(selectedOrder)}?`)) return;
    setBusy("complete");
    try {
      await completeWarehouseOrder(config, selectedOrder.id);
      await refreshOrders(false);
      onNotice("Заказ завершён и сохранён в PostgreSQL");
    } catch (error) {
      onError(error, "Не удалось завершить заказ");
    } finally {
      setBusy("");
    }
  }

  async function findReturn(event: FormEvent) {
    event.preventDefault();
    const lookup = returnLookup.trim();
    if (!lookup) return;
    setBusy("return-lookup");
    setReturnOrder(null);
    try {
      const order = await lookupReturn(config, lookup);
      setReturnOrder(order);
      setReturnReference(order.skladbot_request_number || lookup);
      onNotice("Заказ найден в архиве PostgreSQL");
    } catch (error) {
      onError(error, "Не удалось найти завершённый заказ");
    } finally {
      setBusy("");
    }
  }

  async function confirmReturn() {
    if (!returnOrder || !canWrite) return;
    if (!window.confirm(`Оформить полный возврат заказа ${orderLabel(returnOrder)}?`)) return;
    setBusy("return");
    try {
      await markReturn(config, returnOrder.id, {
        return_reference: returnReference.trim(),
        returned_by: actor || "web",
        confirmed_items: returnOrder.items.map((item) => ({
          item_id: item.id,
          product: item.product,
          quantity_blocks: item.quantity_blocks,
          quantity_pieces: item.quantity_pieces,
        })),
      });
      setReturnOrder(null);
      setReturnLookup("");
      setReturnReference("");
      onNotice("Возврат зафиксирован в PostgreSQL; КИЗы снова доступны");
    } catch (error) {
      onError(error, "Не удалось оформить возврат");
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="warehouse-panel" aria-label="DB-only склад">
      <div className="panel-header">
        <div>
          <h2>Склад · PostgreSQL</h2>
          <span className="panel-subtitle">Сканирование и возвраты напрямую через backend</span>
        </div>
        <button className="ghost-button" onClick={() => void refreshOrders()} disabled={loading || Boolean(busy)}>
          {loading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
          Обновить
        </button>
      </div>

      {!canWrite && (
        <div className="warehouse-warning" role="status">
          <AlertCircle size={18} /> Доступен просмотр. Для сканирования и возвратов нужно право warehouse:write.
        </div>
      )}

      <div className="warehouse-grid">
        <div className="warehouse-card">
          <h3>Активный заказ</h3>
          <label>
            <span>Заказ</span>
            <select value={selectedOrder?.id ?? ""} onChange={(event) => chooseOrder(event.target.value)} disabled={!orders.length}>
              {orders.map((order) => <option key={order.id} value={order.id}>{orderLabel(order)}</option>)}
            </select>
          </label>
          {selectedOrder ? (
            <>
              <p className="warehouse-meta">{selectedOrder.client} · {selectedOrder.address}</p>
              <label>
                <span>Позиция</span>
                <select value={selectedItem?.id ?? ""} onChange={(event) => { setSelectedItemId(event.target.value); setAvailability(null); }}>
                  {selectedOrder.items.map((item) => (
                    <option key={item.id} value={item.id}>{item.product} · {item.scanned_blocks}/{item.quantity_blocks}</option>
                  ))}
                </select>
              </label>
              <form className="warehouse-scan-form" onSubmit={submitScan}>
                <label>
                  <span>КИЗ</span>
                  <input
                    ref={scanInputRef}
                    value={scanCode}
                    onChange={(event) => { setScanCode(event.target.value); setAvailability(null); }}
                    autoComplete="off"
                    inputMode="text"
                    placeholder="Отсканируйте код"
                    disabled={!canWrite || Boolean(busy)}
                  />
                </label>
                <button className="primary-button" type="submit" disabled={!canWrite || !selectedItem || !scannerCode(scanCode) || Boolean(busy)}>
                  {busy === "scan" ? <Loader2 className="spin" size={16} /> : <PackageCheck size={16} />}
                  Записать
                </button>
              </form>
              {availability && (
                <div className={availability.available ? "warehouse-check ok" : "warehouse-check error"} role="status">
                  {availability.available ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
                  {availability.available ? "КИЗ доступен" : (availability.reason || "КИЗ недоступен")}
                </div>
              )}
              <div className="warehouse-actions">
                <button className="ghost-button" onClick={() => void removeLastCode()} disabled={!canWrite || !selectedItem?.scan_codes.length || Boolean(busy)}>
                  {busy === "undo" ? <Loader2 className="spin" size={16} /> : <Undo2 size={16} />}
                  Отменить последний КИЗ
                </button>
                <button className="primary-button" onClick={() => void completeOrder()} disabled={!canWrite || !orderComplete || Boolean(busy)}>
                  {busy === "complete" ? <Loader2 className="spin" size={16} /> : <CheckCircle2 size={16} />}
                  Завершить заказ
                </button>
              </div>
            </>
          ) : <p className="warehouse-empty">Активных заказов нет.</p>}
        </div>

        <div className="warehouse-card">
          <h3>Возврат из архива</h3>
          <form className="warehouse-return-search" onSubmit={findReturn}>
            <label>
              <span>Номер SkladBot, клиент или ID заказа</span>
              <input value={returnLookup} onChange={(event) => setReturnLookup(event.target.value)} placeholder="WH-R-..." />
            </label>
            <button className="ghost-button" type="submit" disabled={!returnLookup.trim() || Boolean(busy)}>
              {busy === "return-lookup" ? <Loader2 className="spin" size={16} /> : <Search size={16} />}
              Найти
            </button>
          </form>
          {returnOrder && (
            <div className="warehouse-return-result">
              <strong>{orderLabel(returnOrder)}</strong>
              <span>{returnOrder.client} · позиций {returnOrder.items.length}</span>
              <label>
                <span>Основание возврата</span>
                <input value={returnReference} onChange={(event) => setReturnReference(event.target.value)} />
              </label>
              <p>Будет оформлен полный возврат всех позиций и КИЗов заказа.</p>
              <button className="danger-button" onClick={() => void confirmReturn()} disabled={!canWrite || Boolean(busy)}>
                {busy === "return" ? <Loader2 className="spin" size={16} /> : <RotateCcw size={16} />}
                Подтвердить полный возврат
              </button>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function orderLabel(order: Order) {
  return order.skladbot_request_number || `${order.client} · ${order.order_date || "без даты"}`;
}

function shortCode(code: string) {
  if (code.length <= 24) return code;
  return `${code.slice(0, 12)}…${code.slice(-8)}`;
}

function scannerCode(value: string) {
  return value.replace(/[\r\n]+$/g, "");
}
