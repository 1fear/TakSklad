import { Download, FileSpreadsheet, Loader2, Upload } from "lucide-react";
import { useRef, useState } from "react";

import {
  type AdminTableRequest,
  type ApiConfig,
  type ExcelImportPreview,
  commitExcelImport,
  downloadAdminOrders,
  previewExcelImport,
} from "../../api";

type ExcelImportControlsProps = {
  config: ApiConfig;
  canImport: boolean;
  exportFilters: AdminTableRequest;
  onCommitted: () => Promise<void>;
  onError: (error: unknown, fallback: string) => void;
  onNotice: (message: string) => void;
};

export default function ExcelImportControls({
  config,
  canImport,
  exportFilters,
  onCommitted,
  onError,
  onNotice,
}: ExcelImportControlsProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ExcelImportPreview | null>(null);
  const [busy, setBusy] = useState("");

  function chooseFile(next: File | null) {
    setFile(next);
    setPreview(null);
  }

  async function previewFile() {
    if (!file || !canImport) return;
    setBusy("preview");
    try {
      const result = await previewExcelImport(config, file);
      setPreview(result);
      onNotice(`Excel проверен: новых заказов ${result.orders_new}, импортируемых строк ${result.rows_importable}`);
    } catch (error) {
      onError(error, "Не удалось проверить Excel");
    } finally {
      setBusy("");
    }
  }

  async function commitFile() {
    if (!file || !preview || !canImport) return;
    if (preview.invalid_rows > 0 || preview.errors.length > 0) {
      onError(new Error("Импорт заблокирован: preview содержит ошибки"), "Исправьте Excel и повторите preview");
      return;
    }
    if (!window.confirm(`Импортировать ${preview.rows_importable} строк из ${file.name} в PostgreSQL?`)) return;
    setBusy("commit");
    try {
      const result = await commitExcelImport(config, file);
      await onCommitted();
      onNotice(`Excel импортирован: заказов ${result.orders_created}, позиций ${result.items_created}, строк ${result.rows_imported}`);
      setFile(null);
      setPreview(null);
      if (inputRef.current) inputRef.current.value = "";
    } catch (error) {
      onError(error, "Не удалось импортировать Excel");
    } finally {
      setBusy("");
    }
  }

  async function exportOrders() {
    setBusy("export");
    try {
      const result = await downloadAdminOrders(config, exportFilters);
      const href = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(href);
      onNotice("XLSX-выгрузка сформирована из PostgreSQL");
    } catch (error) {
      onError(error, "Не удалось выгрузить заказы XLSX");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="excel-controls" aria-label="Excel и PostgreSQL">
      {canImport && (
        <>
          <label className="excel-file-button">
            <FileSpreadsheet size={16} />
            <span>{file?.name || "Выбрать Excel"}</span>
            <input
              ref={inputRef}
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
            />
          </label>
          <button className="ghost-button" onClick={() => void previewFile()} disabled={!file || Boolean(busy)}>
            {busy === "preview" ? <Loader2 className="spin" size={16} /> : <Upload size={16} />}
            Проверить Excel
          </button>
          {preview && (
            <button className="primary-button" onClick={() => void commitFile()} disabled={Boolean(busy) || preview.invalid_rows > 0 || preview.errors.length > 0}>
              {busy === "commit" ? <Loader2 className="spin" size={16} /> : <Upload size={16} />}
              Импортировать {preview.rows_importable}
            </button>
          )}
        </>
      )}
      <button className="ghost-button" onClick={() => void exportOrders()} disabled={Boolean(busy)}>
        {busy === "export" ? <Loader2 className="spin" size={16} /> : <Download size={16} />}
        Выгрузить XLSX
      </button>
      {preview && (
        <span className={preview.invalid_rows || preview.errors.length ? "excel-preview error" : "excel-preview ok"} role="status">
          Строк: {preview.rows_total}; новых заказов: {preview.orders_new}; дублей: {preview.duplicate_rows}; ошибок: {preview.invalid_rows}
        </span>
      )}
    </div>
  );
}
