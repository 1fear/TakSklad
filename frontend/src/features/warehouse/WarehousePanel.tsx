import { AlertCircle, CheckCircle2, Loader2, RefreshCw, RotateCcw, Search } from "lucide-react";
import type { FormEvent } from "react";
import { useEffect, useMemo, useState } from "react";

import {
  type ApiConfig,
  type Order,
  completeWarehouseOrder,
  listActiveOrders,
  lookupReturn,
  markReturn,
} from "../../api";
import OrderCorrelationDetails from "../orders/OrderCorrelationDetails";

type WarehousePanelProps = {
  config: ApiConfig;
  canWrite: boolean;
  actor: string;
  onError: (error: unknown, fallback: string) => void;
  onNotice: (message: string) => void;
};

export default function WarehousePanel({ config, canWrite, actor, onError, onNotice }: WarehousePanelProps) {
  const [orders, setOrders] = useState<Order[]>([]);
  const [selectedOrderId, setSelectedOrderId] = useState("");
  const [returnLookup, setReturnLookup] = useState("");
  const [returnOrder, setReturnOrder] = useState<Order | null>(null);
  const [returnReference, setReturnReference] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");

  const selectedOrder = useMemo(
    () => orders.find((order) => order.id === selectedOrderId) ?? orders[0],
    [orders, selectedOrderId],
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
      if (showNotice) onNotice(`Активные заказы обновлены: ${next.length}`);
    } catch (error) {
      onError(error, "Не удалось загрузить активные заказы");
    } finally {
      setLoading(false);
    }
  }

  function chooseOrder(orderId: string) {
    setSelectedOrderId(orderId);
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
          <span className="panel-subtitle">Заказы и возвраты напрямую через backend</span>
        </div>
        <button className="ghost-button" onClick={() => void refreshOrders()} disabled={loading || Boolean(busy)}>
          {loading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
          Обновить
        </button>
      </div>

      {!canWrite && (
        <div className="warehouse-warning" role="status">
          <AlertCircle size={18} /> Доступен просмотр. Для возвратов нужно право warehouse:write.
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
              <OrderCorrelationDetails
                smartupId={selectedOrder.smartup_id}
                skladbotRequestNumber={selectedOrder.skladbot_request_number}
                skladbotRequestId={selectedOrder.skladbot_request_id}
                returnRequestNumber={selectedOrder.skladbot_return_request_number}
                returnRequestId={selectedOrder.skladbot_return_request_id}
                showReturn={orderHasReturn(selectedOrder)}
              />
              <div className="warehouse-actions">
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
              <OrderCorrelationDetails
                smartupId={returnOrder.smartup_id}
                skladbotRequestNumber={returnOrder.skladbot_request_number}
                skladbotRequestId={returnOrder.skladbot_request_id}
                returnRequestNumber={returnOrder.skladbot_return_request_number}
                returnRequestId={returnOrder.skladbot_return_request_id}
                showReturn={orderHasReturn(returnOrder)}
              />
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

function orderHasReturn(order: Order) {
  return order.status === "returned"
    || Boolean(order.return_status || order.returned_at || order.return_reference)
    || Boolean(order.skladbot_return_request_id || order.skladbot_return_request_number);
}
