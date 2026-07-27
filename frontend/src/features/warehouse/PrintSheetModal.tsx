import { Printer, X } from "lucide-react";
import { useEffect, useRef } from "react";

export type PrintSheetSnapshotItem = {
  id: string;
  product: string;
  quantity_blocks: number;
  scanned_blocks: number;
  quantity_pieces: number;
  requires_kiz: boolean;
};

export type PrintSheetSnapshot = {
  orderId: string;
  orderDate: string | null;
  paymentType: string;
  client: string;
  address: string;
  representative: string | null;
  skladbotRequestNumber: string;
  completedAt: string;
  items: PrintSheetSnapshotItem[];
};

type PrintSheetModalProps = {
  open: boolean;
  snapshot: PrintSheetSnapshot | null;
  onClose: () => void;
};

export default function PrintSheetModal({ open, snapshot, onClose }: PrintSheetModalProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const printButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    document.body.classList.add("warehouse-print-open");
    printButtonRef.current?.focus();
    function handleKeydown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
      if (!focusable.length) return;
      const currentIndex = focusable.indexOf(document.activeElement as HTMLElement);
      if (event.shiftKey && currentIndex <= 0) {
        event.preventDefault();
        focusable.at(-1)?.focus();
        return;
      }
      if (!event.shiftKey && currentIndex === focusable.length - 1) {
        event.preventDefault();
        focusable[0]?.focus();
      }
    }
    window.addEventListener("keydown", handleKeydown);
    return () => {
      document.body.classList.remove("warehouse-print-open");
      window.removeEventListener("keydown", handleKeydown);
    };
  }, [open, onClose]);

  if (!open || !snapshot) return null;

  return (
    <div className="warehouse-print-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose();
    }}>
      <section
        ref={dialogRef}
        className="warehouse-print-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="warehouse-print-title"
        aria-describedby="warehouse-print-description"
      >
        <button className="warehouse-print-close" onClick={onClose} aria-label="Закрыть печать">
          <X size={18} />
        </button>

        <div className="warehouse-print-sheet">
          <header className="warehouse-print-head">
            <div>
              <h2 id="warehouse-print-title">Печать сводного листа</h2>
              <p id="warehouse-print-description">
                Сводка подготовлена для браузерной печати. Печать запускается только по кнопке ниже.
              </p>
            </div>
            <span className="warehouse-print-timestamp">Завершено: {formatMoment(snapshot.completedAt)}</span>
          </header>

          <dl className="warehouse-print-meta">
            <div>
              <dt>Заявка</dt>
              <dd>{snapshot.skladbotRequestNumber || snapshot.orderId}</dd>
            </div>
            <div>
              <dt>Клиент</dt>
              <dd>{snapshot.client}</dd>
            </div>
            <div>
              <dt>Дата</dt>
              <dd>{snapshot.orderDate || "Без даты"}</dd>
            </div>
            <div>
              <dt>Оплата</dt>
              <dd>{snapshot.paymentType}</dd>
            </div>
            <div>
              <dt>Адрес</dt>
              <dd>{snapshot.address}</dd>
            </div>
            <div>
              <dt>Представитель</dt>
              <dd>{snapshot.representative || "Не указан"}</dd>
            </div>
          </dl>

          <table className="warehouse-print-table">
            <thead>
              <tr>
                <th scope="col">Товар</th>
                <th scope="col">Блоки</th>
                <th scope="col">Штуки</th>
                <th scope="col">КИЗ</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.items.map((item) => (
                <tr key={item.id}>
                  <td>{item.product}</td>
                  <td>{item.scanned_blocks}/{item.quantity_blocks}</td>
                  <td>{item.quantity_pieces}</td>
                  <td>{item.requires_kiz ? "Обязателен" : "Не требуется"}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="warehouse-print-actions">
            <button
              ref={printButtonRef}
              className="primary-button"
              onClick={() => window.print()}
            >
              <Printer size={18} />
              Печать
            </button>
            <button className="ghost-button" onClick={onClose}>Закрыть</button>
          </div>
        </div>
      </section>
    </div>
  );
}

function formatMoment(value: string) {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  return new Date(parsed).toLocaleString("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  });
}
