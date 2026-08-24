import { AlertCircle, CheckCircle2, Loader2, RefreshCw, RotateCcw, Undo2 } from "lucide-react";
import type { FormEvent, RefObject } from "react";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";

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
import OrderCorrelationDetails from "../orders/OrderCorrelationDetails";
import PrintSheetModal, { type PrintSheetSnapshot } from "./PrintSheetModal";
import ScannerInput from "./ScannerInput";
import { isKizFormatRule, kizFormatMessage, kizFormatViolation, normalizeKizCode } from "./kizFormat";
import { projectItemProgress } from "./offline/projection";
import type { OfflineEvent } from "./offline/queueTypes";
import { useOfflineQueue } from "./offline/useOfflineQueue";
import "./WarehousePanel.css";

type WarehousePanelProps = {
  config: ApiConfig;
  canWrite: boolean;
  actor: string;
  onError: (error: unknown, fallback: string) => void;
  onNotice: (message: string) => void;
};

type SelectionSnapshot = {
  orderId: string;
  itemId: string;
};

const WORKSTATION_ID = "taksklad-web";

/**
 * A refusal the queue must never swallow.
 *
 * The offline queue exists for a backend that could not be reached. A backend
 * that answered, but answered with a different code, is a fail-closed condition:
 * queueing it would hide a real mismatch behind a hopeful retry.
 */
class ScanNotQueueableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ScanNotQueueableError";
  }
}

export default function WarehousePanel({ config, canWrite, actor, onError, onNotice }: WarehousePanelProps) {
  const scanInputRef = useRef<HTMLInputElement>(null);
  const printOpenButtonRef = useRef<HTMLButtonElement>(null);
  const errorHandlerRef = useRef(onError);
  const noticeHandlerRef = useRef(onNotice);
  const selectionRef = useRef<SelectionSnapshot>({ orderId: "", itemId: "" });
  const [orders, setOrders] = useState<Order[]>([]);
  const [selectedOrderId, setSelectedOrderId] = useState("");
  const [selectedItemId, setSelectedItemId] = useState("");
  const [scanCode, setScanCode] = useState("");
  const [availability, setAvailability] = useState<KizAvailability | null>(null);
  const [returnLookup, setReturnLookup] = useState("");
  const [returnOrder, setReturnOrder] = useState<Order | null>(null);
  const [returnReference, setReturnReference] = useState("");
  const [loading, setLoading] = useState(false);
  const [scanBusy, setScanBusy] = useState<"" | "scan" | "undo" | "complete">("");
  const [returnBusy, setReturnBusy] = useState<"" | "lookup" | "confirm">("");
  const [lastCompletedSnapshot, setLastCompletedSnapshot] = useState<PrintSheetSnapshot | null>(null);
  const [printModalOpen, setPrintModalOpen] = useState(false);

  const selectedOrder = useMemo(
    () => orders.find((order) => order.id === selectedOrderId) ?? orders[0] ?? null,
    [orders, selectedOrderId],
  );
  const selectedItem = useMemo(
    () => selectedOrder?.items.find((item) => item.id === selectedItemId) ?? selectedOrder?.items[0] ?? null,
    [selectedItemId, selectedOrder],
  );
  const recentScans = useMemo(
    () => recentScanEntries(selectedItem),
    [selectedItem],
  );
  const orderProgress = useMemo(
    () => summarizeOrderProgress(selectedOrder),
    [selectedOrder],
  );
  const hasBusyAction = Boolean(scanBusy || returnBusy);

  useEffect(() => {
    errorHandlerRef.current = onError;
  }, [onError]);

  useEffect(() => {
    noticeHandlerRef.current = onNotice;
  }, [onNotice]);

  const refreshOrders = useCallback(async (
    showNotice = true,
    preserve?: Partial<SelectionSnapshot>,
    options: { reportError?: boolean } = {},
  ) => {
    const reportError = options.reportError ?? true;
    setLoading(true);
    try {
      const next = await listActiveOrders(config);
      const selection = nextSelection(next, preserve ?? selectionRef.current);
      setOrders(next);
      setSelectedOrderId(selection.orderId);
      setSelectedItemId(selection.itemId);
      setAvailability(null);
      if (showNotice) noticeHandlerRef.current(`Активные заказы обновлены: ${next.length}`);
      return true;
    } catch (error) {
      if (reportError) errorHandlerRef.current(error, "Не удалось загрузить активные заказы");
      return false;
    } finally {
      setLoading(false);
    }
  }, [config]);

  const onQueueReplayed = useCallback(() => {
    void refreshOrders(false, undefined, { reportError: false });
  }, [refreshOrders]);

  const offlineQueue = useOfflineQueue(config, { onReplayed: onQueueReplayed });
  const pendingForSelectedOrder = selectedOrder
    ? offlineQueue.pendingScansForOrder(selectedOrder.id)
    : 0;
  const pendingForSelectedItem = selectedItem
    ? Boolean(offlineQueue.lastPendingScanForItem(selectedItem.id))
    : false;

  // Progress the operator sees counts what is confirmed by the backend plus what
  // is still waiting in the queue. Without it an offline scan looks like it never
  // happened, and the operator scans the same block twice.
  const projectedItem = useMemo(
    () => (selectedItem ? projectItemProgress(selectedItem, offlineQueue.pending) : null),
    [offlineQueue.pending, selectedItem],
  );
  const canScanSelectedItem = Boolean(
    canWrite
      && selectedItem
      && requiresKiz(selectedItem)
      && selectedItem.quantity_blocks > 0
      && projectedItem
      && !projectedItem.complete,
  );
  const orderComplete = Boolean(selectedOrder?.items.length)
    && selectedOrder!.items.every((item) => isCompletionSatisfied(item, offlineQueue.pending));
  const itemProgress = useMemo(
    () => summarizeItemProgress(selectedItem, offlineQueue.pending),
    [offlineQueue.pending, selectedItem],
  );

  useEffect(() => {
    void refreshOrders(false);
  }, [refreshOrders]);

  useEffect(() => {
    selectionRef.current = { orderId: selectedOrderId, itemId: selectedItemId };
  }, [selectedItemId, selectedOrderId]);

  useEffect(() => {
    if (!selectedOrderId || !selectedItemId || printModalOpen) return;
    focusScanner(scanInputRef);
  }, [printModalOpen, selectedItemId, selectedOrderId]);

  function chooseOrder(orderId: string) {
    const order = orders.find((value) => value.id === orderId) ?? null;
    setSelectedOrderId(orderId);
    setSelectedItemId(order?.items[0]?.id ?? "");
    setScanCode("");
    setAvailability(null);
  }

  function chooseItem(itemId: string) {
    setSelectedItemId(itemId);
    setAvailability(null);
  }

  async function submitScan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const code = normalizeKizCode(scanCode);
    if (!selectedItem || !code || !canScanSelectedItem || scanBusy) return;

    const formatViolation = kizFormatViolation(code);
    if (formatViolation) {
      const message = kizFormatMessage(formatViolation);
      setAvailability({
        code,
        available: false,
        reason: formatViolation,
        latest_movement_type: "",
        latest_order_item_id: "",
        existing_order_item_id: "",
      });
      onError(new Error(message), message);
      focusScanner(scanInputRef, true);
      return;
    }

    if (offlineQueue.pendingCodes.has(code)) {
      const message = "Этот КИЗ уже в очереди на отправку";
      onError(new Error(message), message);
      focusScanner(scanInputRef, true);
      return;
    }

    setScanBusy("scan");
    setAvailability(null);
    try {
      // The preflight is a hint, not an authority. When it cannot be reached the
      // scan still goes to the POST below, which is the only answer that counts.
      const preflight = await lookupKizAvailability(config, code, selectedItem.id).catch(() => null);
      if (preflight) {
        setAvailability(preflight);
        if (!preflight.available) {
          const message = availabilityMessage(preflight);
          onError(new Error(message), message);
          focusScanner(scanInputRef, true);
          return;
        }
      }

      const result = await createScan(config, {
        order_item_id: selectedItem.id,
        code,
        workstation_id: WORKSTATION_ID,
        scanned_by: actor || "web",
      });
      if (result.code !== code) {
        const mismatch = {
          code,
          available: false,
          reason: "response_mismatch",
          latest_movement_type: "",
          latest_order_item_id: "",
          existing_order_item_id: "",
        } satisfies KizAvailability;
        setAvailability(mismatch);
        throw new ScanNotQueueableError("Сервер вернул другой КИЗ. Скан не подтвержден.");
      }

      setScanCode("");
      const refreshed = await refreshOrders(
        false,
        { orderId: selectedOrder.id, itemId: selectedItem.id },
        { reportError: false },
      );
      setAvailability({
        code,
        available: true,
        reason: "saved",
        latest_movement_type: "",
        latest_order_item_id: "",
        existing_order_item_id: "",
      });
      onNotice(refreshed ? "КИЗ сохранён в PostgreSQL" : "КИЗ сохранён, но список не обновился — нажмите Обновить.");
      focusScanner(scanInputRef);
    } catch (error) {
      // A backend that cannot be reached must not cost the warehouse a block that
      // physically left the shelf. Refusals on the merits keep the old behaviour:
      // the code stays in the field so the operator can see what was rejected.
      if (!(error instanceof ScanNotQueueableError) && offlineQueue.shouldQueue(error)) {
        try {
          await offlineQueue.enqueueScan({
            orderId: selectedOrder.id,
            orderItemId: selectedItem.id,
            code,
            actor: actor || "web",
            workstationId: WORKSTATION_ID,
          });
          setScanCode("");
          setAvailability({
            code,
            available: true,
            reason: "queued_offline",
            latest_movement_type: "",
            latest_order_item_id: "",
            existing_order_item_id: "",
          });
          onNotice("КИЗ сохранён локально, отправим при связи");
          focusScanner(scanInputRef);
          return;
        } catch (queueError) {
          onError(queueError, "Не удалось сохранить КИЗ даже локально");
          focusScanner(scanInputRef, true);
          return;
        }
      }

      setAvailability((current) => current?.available ? {
        code,
        available: false,
        reason: "create_failed",
        latest_movement_type: "",
        latest_order_item_id: "",
        existing_order_item_id: "",
      } : current);
      onError(error, "Не удалось сохранить КИЗ");
      focusScanner(scanInputRef, true);
    } finally {
      setScanBusy("");
    }
  }

  async function removeLastCode() {
    if (!selectedItem || !canWrite || scanBusy) return;

    // A queued scan never reached the backend, so undoing it there would take back
    // somebody else's code. The newest local one goes first, exactly as the desktop
    // queue does in `undo_backend_scan`.
    const queued = offlineQueue.lastPendingScanForItem(selectedItem.id);
    if (queued) {
      if (!window.confirm(`Убрать из очереди КИЗ ${shortCode(queued.code)}?`)) return;
      setScanBusy("undo");
      try {
        await offlineQueue.removePendingScan(queued);
        setAvailability(null);
        onNotice("КИЗ убран из очереди, на сервер он не уходил");
        focusScanner(scanInputRef);
      } catch (error) {
        onError(error, "Не удалось убрать КИЗ из очереди");
        focusScanner(scanInputRef);
      } finally {
        setScanBusy("");
      }
      return;
    }

    const code = recentScans.at(-1)?.code ?? "";
    if (!code) return;
    if (!window.confirm(`Отменить последний КИЗ ${shortCode(code)}?`)) return;

    setScanBusy("undo");
    try {
      await undoScan(config, {
        order_item_id: selectedItem.id,
        code,
        workstation_id: WORKSTATION_ID,
        actor: actor || "web",
      });
      setAvailability(null);
      const refreshed = await refreshOrders(
        false,
        { orderId: selectedOrder?.id ?? "", itemId: selectedItem.id },
        { reportError: false },
      );
      onNotice(refreshed ? "Последний КИЗ отменён в PostgreSQL" : "Последний КИЗ отменён, но список не обновился — нажмите Обновить.");
      focusScanner(scanInputRef);
    } catch (error) {
      onError(error, "Не удалось отменить последний КИЗ");
      focusScanner(scanInputRef);
    } finally {
      setScanBusy("");
    }
  }

  async function completeOrder() {
    if (!selectedOrder || !orderComplete || !canWrite || scanBusy) return;
    if (!window.confirm(`Завершить заказ ${orderLabel(selectedOrder)}?`)) return;

    setScanBusy("complete");
    try {
      const completedOrder = await completeWarehouseOrder(config, selectedOrder.id);
      setLastCompletedSnapshot(orderToPrintSnapshot(completedOrder));
      setPrintModalOpen(true);
      const refreshed = await refreshOrders(false, undefined, { reportError: false });
      onNotice(refreshed ? "Заказ завершён и сохранён в PostgreSQL" : "Заказ завершён, но список не обновился — нажмите Обновить.");
    } catch (error) {
      onError(error, "Не удалось завершить заказ");
    } finally {
      setScanBusy("");
    }
  }

  async function findReturn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const lookup = returnLookup.trim();
    if (!lookup || returnBusy) return;

    setReturnBusy("lookup");
    setReturnOrder(null);
    try {
      const order = await lookupReturn(config, lookup);
      setReturnOrder(order);
      setReturnReference(order.skladbot_request_number || lookup);
      onNotice("Заказ найден в архиве PostgreSQL");
    } catch (error) {
      onError(error, "Не удалось найти завершённый заказ");
    } finally {
      setReturnBusy("");
    }
  }

  async function confirmReturn() {
    if (!returnOrder || !canWrite || returnBusy) return;
    if (!window.confirm(`Оформить полный возврат заказа ${orderLabel(returnOrder)}?`)) return;

    setReturnBusy("confirm");
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
      setReturnBusy("");
    }
  }

  function openPrintModal() {
    if (!lastCompletedSnapshot) return;
    setPrintModalOpen(true);
  }

  function closePrintModal() {
    setPrintModalOpen(false);
    queueMicrotask(() => printOpenButtonRef.current?.focus());
  }

  return (
    <>
      <section className="warehouse-panel" aria-label="DB-only склад">
        <div className="panel-header">
          <div>
            <h2>Склад · PostgreSQL</h2>
            <span className="panel-subtitle">Сканирование, завершение и полный возврат через backend</span>
          </div>
          <button className="ghost-button" onClick={() => void refreshOrders(true, selectionRef.current)} disabled={loading || hasBusyAction}>
            {loading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
            Обновить
          </button>
        </div>

        {!canWrite && (
          <div className="warehouse-warning" role="status">
            <AlertCircle size={18} /> Доступен просмотр. Для сканирования, завершения и возвратов нужно право warehouse:write.
          </div>
        )}

        {offlineQueue.pending.length > 0 && (
          <div className="warehouse-queue" role="status" aria-label="Очередь на отправку">
            <div>
              <strong>В очереди: {offlineQueue.pending.length}</strong>
              <span>Эти КИЗы записаны локально и уйдут в PostgreSQL, когда вернётся связь.</span>
            </div>
            <button
              className="ghost-button"
              onClick={() => void offlineQueue.replayNow()}
              disabled={offlineQueue.replaying}
              type="button"
            >
              {offlineQueue.replaying ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
              Отправить сейчас
            </button>
          </div>
        )}

        {offlineQueue.storageError && (
          <div className="warehouse-warning" role="status">
            <AlertCircle size={18} /> Локальная очередь недоступна: при обрыве связи скан не сохранится.
          </div>
        )}

        {lastCompletedSnapshot && (
          <div className="warehouse-print-ready" role="status">
            <div>
              <strong>{lastCompletedSnapshot.client}</strong>
              <span>Последний завершённый заказ готов к печати в этой сессии.</span>
            </div>
            <button
              ref={printOpenButtonRef}
              className="primary-button"
              onClick={openPrintModal}
              type="button"
            >
              Открыть печать
            </button>
          </div>
        )}

        <div className="warehouse-grid">
          <div className="warehouse-card">
            <h3>Активный заказ</h3>
            <label>
              <span>Заказ</span>
              <select
                value={selectedOrder?.id ?? ""}
                onChange={(event) => chooseOrder(event.target.value)}
                disabled={!orders.length || loading || Boolean(scanBusy)}
              >
                {orders.map((order) => (
                  <option key={order.id} value={order.id}>{orderLabel(order)}</option>
                ))}
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
                <div className="warehouse-progress" role="status">
                  <strong>Обязательные КИЗы</strong>
                  <span>{orderProgress.label}</span>
                  {typeof orderProgress.ratio === "number" && (
                    <i
                      className="warehouse-progress-meter"
                      style={{ "--p": orderProgress.ratio } as CSSProperties}
                      aria-hidden="true"
                    />
                  )}
                </div>
                <label>
                  <span>Позиция</span>
                  <select
                    value={selectedItem?.id ?? ""}
                    onChange={(event) => chooseItem(event.target.value)}
                    disabled={loading || Boolean(scanBusy)}
                  >
                    {selectedOrder.items.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.product} · {item.scanned_blocks}/{item.quantity_blocks}
                      </option>
                    ))}
                  </select>
                </label>
                {selectedItem && (
                  <>
                    <div className="warehouse-item-progress" role="status">
                      <strong>{selectedItem.product}</strong>
                      <span>{itemProgress}</span>
                    </div>

                    {requiresKiz(selectedItem) && selectedItem.quantity_blocks > 0 ? (
                      <>
                        <ScannerInput
                          inputRef={scanInputRef}
                          value={scanCode}
                          disabled={!canScanSelectedItem || loading}
                          submitDisabled={!canScanSelectedItem || !normalizeKizCode(scanCode) || Boolean(scanBusy)}
                          busy={scanBusy === "scan"}
                          onChange={(value) => {
                            setScanCode(value);
                            setAvailability(null);
                          }}
                          onSubmit={submitScan}
                        />
                        {availability && (
                          <div className={availability.available ? "warehouse-check ok" : "warehouse-check error"} role="status">
                            {availability.available ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
                            {availabilityMessage(availability)}
                          </div>
                        )}
                        <div className="warehouse-recent-scans">
                          <div className="warehouse-recent-head">
                            <strong>Последние сканы</strong>
                            <span>{recentScans.length ? `${recentScans.length} шт.` : "Пока пусто"}</span>
                          </div>
                          {recentScans.length ? (
                            <ul>
                              {recentScans.slice(-5).reverse().map((entry) => (
                                <li key={`${entry.code}-${entry.scanned_at ?? "na"}`}>
                                  <span>{shortCode(entry.code)}</span>
                                  <span>{entry.block_quantity} бл.</span>
                                  <span>{formatScanMoment(entry.scanned_at)}</span>
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <p className="warehouse-empty">КИЗы для этой позиции ещё не записаны.</p>
                          )}
                        </div>
                      </>
                    ) : (
                      <div className="warehouse-check ok" role="status">
                        <CheckCircle2 size={16} />
                        Для этой позиции КИЗ не требуется.
                      </div>
                    )}
                    <div className="warehouse-actions">
                      <button
                        className="ghost-button"
                        onClick={() => void removeLastCode()}
                        disabled={!canWrite || (!recentScans.length && !pendingForSelectedItem) || Boolean(scanBusy)}
                      >
                        {scanBusy === "undo" ? <Loader2 className="spin" size={16} /> : <Undo2 size={16} />}
                        Отменить последний КИЗ
                      </button>
                      <button
                        className="primary-button"
                        onClick={() => void completeOrder()}
                        disabled={!canWrite || !orderComplete || Boolean(scanBusy) || pendingForSelectedOrder > 0}
                      >
                        {scanBusy === "complete" ? <Loader2 className="spin" size={16} /> : <CheckCircle2 size={16} />}
                        Завершить заказ
                      </button>
                    </div>
                    {pendingForSelectedOrder > 0 && (
                      <p className="warehouse-queue-hint">Сначала отправьте очередь</p>
                    )}
                  </>
                )}
              </>
            ) : <p className="warehouse-empty">Активных заказов нет.</p>}
          </div>

          <div className="warehouse-card">
            <h3>Возврат из архива</h3>
            <form className="warehouse-return-search" onSubmit={findReturn}>
              <label>
                <span>Номер или ID SkladBot, либо ID заказа</span>
                <input
                  value={returnLookup}
                  onChange={(event) => setReturnLookup(event.target.value)}
                  placeholder="WH-R-..."
                  disabled={Boolean(returnBusy)}
                />
              </label>
              <button className="ghost-button" type="submit" disabled={!returnLookup.trim() || Boolean(returnBusy)}>
                {returnBusy === "lookup" ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
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
                  <input
                    value={returnReference}
                    onChange={(event) => setReturnReference(event.target.value)}
                    disabled={Boolean(returnBusy)}
                  />
                </label>
                <p>Будет оформлен только полный возврат всех позиций и КИЗов заказа.</p>
                <button className="danger-button" onClick={() => void confirmReturn()} disabled={!canWrite || Boolean(returnBusy)}>
                  {returnBusy === "confirm" ? <Loader2 className="spin" size={16} /> : <RotateCcw size={16} />}
                  Подтвердить полный возврат
                </button>
              </div>
            )}
          </div>
        </div>
      </section>

      <PrintSheetModal open={printModalOpen} snapshot={lastCompletedSnapshot} onClose={closePrintModal} />
    </>
  );
}

function nextSelection(orders: Order[], preserve: Partial<SelectionSnapshot>) {
  const order = orders.find((candidate) => candidate.id === preserve.orderId) ?? orders[0] ?? null;
  const item = order?.items.find((candidate) => candidate.id === preserve.itemId) ?? order?.items[0] ?? null;
  return {
    orderId: order?.id ?? "",
    itemId: item?.id ?? "",
  };
}

function isCompletionSatisfied(item: Order["items"][number], pending: OfflineEvent[] = []) {
  if (!requiresKiz(item) || item.quantity_blocks <= 0) return true;
  return projectItemProgress(item, pending).complete;
}

function summarizeOrderProgress(order: Order | null) {
  if (!order) return { label: "Нет активного заказа" };
  const requiredItems = order.items.filter((item) => requiresKiz(item) && item.quantity_blocks > 0);
  if (!requiredItems.length) return { label: "В заказе нет обязательных КИЗов" };
  const scannedBlocks = requiredItems.reduce((sum, item) => sum + Math.min(item.scanned_blocks, item.quantity_blocks), 0);
  const requiredBlocks = requiredItems.reduce((sum, item) => sum + item.quantity_blocks, 0);
  const completedItems = requiredItems.filter((item) => isCompletionSatisfied(item)).length;
  return {
    label: `${scannedBlocks}/${requiredBlocks} блоков · ${completedItems}/${requiredItems.length} позиций`,
    // Доля нужна для полосы прогресса: оператор считывает её взглядом,
    // а не перечитывает «0/2 блоков» текстом при каждом скане
    ratio: requiredBlocks > 0 ? scannedBlocks / requiredBlocks : 0,
    scannedBlocks,
    requiredBlocks,
  };
}

function summarizeItemProgress(item: Order["items"][number] | null, pending: OfflineEvent[] = []) {
  if (!item) return "Позиция не выбрана";
  if (!requiresKiz(item) || item.quantity_blocks <= 0) return "Позиция не влияет на завершение по КИЗам.";
  const progress = projectItemProgress(item, pending);
  const remaining = Math.max(0, item.quantity_blocks - progress.scannedBlocks);
  if (remaining === 0) return `Готово: ${progress.scannedBlocks}/${item.quantity_blocks} блоков.`;
  return `Осталось ${remaining} блок. · ${progress.scannedBlocks}/${item.quantity_blocks} уже записано.`;
}

function recentScanEntries(item: Order["items"][number] | null) {
  if (!item) return [];
  if (item.scan_entries?.length) return item.scan_entries;
  return item.scan_codes.map((code) => ({
    code,
    scan_type: "unit",
    block_quantity: 1,
    scanned_at: null,
  }));
}

function orderToPrintSnapshot(order: Order): PrintSheetSnapshot {
  return {
    orderId: order.id,
    orderDate: order.order_date,
    paymentType: order.payment_type,
    client: order.client,
    address: order.address,
    representative: order.representative ?? null,
    skladbotRequestNumber: order.skladbot_request_number,
    completedAt: new Date().toISOString(),
    items: order.items.map((item) => ({
      id: item.id,
      product: item.product,
      quantity_blocks: item.quantity_blocks,
      scanned_blocks: item.scanned_blocks,
      quantity_pieces: item.quantity_pieces,
      requires_kiz: requiresKiz(item),
    })),
  };
}

function availabilityMessage(availability: KizAvailability) {
  if (availability.available) {
    if (availability.reason === "saved") return "КИЗ подтверждён и записан.";
    if (availability.reason === "queued_offline") return "КИЗ сохранён локально, отправим при связи.";
    return "КИЗ доступен для записи.";
  }
  if (isKizFormatRule(availability.reason)) return kizFormatMessage(availability.reason);
  switch (availability.reason) {
    case "same_order_item_scan":
      return "Этот КИЗ уже записан для выбранной позиции.";
    case "other_order_item_scan_without_movement":
    case "other_order_item_scan_busy":
      return "Этот КИЗ уже занят другой позицией.";
    case "latest_movement_busy":
      return "Этот КИЗ недоступен в текущем состоянии склада.";
    case "kiz_blocked":
      return "Этот КИЗ заблокирован и не может быть записан.";
    case "response_mismatch":
      return "Сервер вернул другой КИЗ. Скан не подтвержден.";
    case "create_failed":
      return "Сохранение не подтверждено. Проверьте код и обновите заказ.";
    default:
      return "КИЗ недоступен для выбранной позиции.";
  }
}

function orderLabel(order: Order) {
  return order.skladbot_request_number || `${order.client} · ${order.order_date || "без даты"}`;
}

function orderHasReturn(order: Order) {
  return order.status === "returned"
    || Boolean(order.return_status || order.returned_at || order.return_reference)
    || Boolean(order.skladbot_return_request_id || order.skladbot_return_request_number);
}

function shortCode(code: string) {
  if (code.length <= 24) return code;
  return `${code.slice(0, 12)}…${code.slice(-8)}`;
}

/**
 * Return focus to the scanner field.
 *
 * `selectAll` matters for hardware keyboard-wedge scanners: after a rejected
 * scan the code deliberately stays in the field so the operator can read it,
 * but the field is also refocused. Without selecting it, the next scan would be
 * typed onto the end of the rejected code and the glued result can still look
 * like a valid KIZ (right prefix, plausible length), so it would be persisted
 * as a real code. Selecting makes the next scan replace the field instead.
 */
function focusScanner(inputRef: RefObject<HTMLInputElement | null>, selectAll = false) {
  queueMicrotask(() => {
    const input = inputRef.current;
    if (!input) return;
    input.focus();
    if (selectAll) input.select();
  });
}

function formatScanMoment(value: string | null) {
  if (!value) return "Без времени";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return "Без времени";
  return new Date(parsed).toLocaleString("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  });
}

function requiresKiz(item: Pick<Order["items"][number], "requires_kiz">) {
  return item.requires_kiz !== false;
}
