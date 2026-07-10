import type { ImportRecord } from "../../api";

export type ImportHistoryPanelProps = { imports: ImportRecord[] };

export default function ImportHistoryPanel({ imports }: ImportHistoryPanelProps) {
  return (
    <section className="table-panel">
      <div className="panel-header">
        <h2>История импортов</h2>
      </div>
      <div className="data-table-wrap">
        <table className="data-table">
          <thead>
            <tr>{["Дата", "Источник", "Статус", "Строк", "Импортировано", "SkladBot dry-run", "Ошибки"].map((header) => <th key={header}>{header}</th>)}</tr>
          </thead>
          <tbody>
            {imports.map((item) => (
              <tr key={item.id}>
                <td>{new Date(item.created_at).toLocaleString("ru-RU")}</td>
                <td>{item.source}</td>
                <td>{item.status}</td>
                <td>{String(item.rows_total)}</td>
                <td>{String(item.rows_imported)}</td>
                <td>{importDryRunSummaryText(item)}</td>
                <td>{importIssuesText(item)}</td>
              </tr>
            ))}
            {imports.length === 0 && (
              <tr><td colSpan={7}>Нет данных</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
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
  const mismatch = Number(summary.linked_mismatch ?? 0);
  const mode = typeof summary.mode === "string" ? summary.mode : "dry_run";
  return `${mode}: ready ${ready}, queued ${queued}, created ${created + recovered}, blocked ${blocked + failed}, mismatch ${mismatch}, WH-R ${alreadyLinked}`;
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
  const mismatch = summary ? numberField(summary, "linked_mismatch") : 0;
  if (invalidRows > 0) issues.push(`ошибочных строк ${invalidRows}`);
  if (duplicateRows > 0) issues.push(`повторов ${duplicateRows}`);
  if (googleError) issues.push(`Google: ${googleError}`);
  if (blocked > 0) issues.push(`SkladBot blocked ${blocked}`);
  if (mismatch > 0) issues.push(`SkladBot mismatch ${mismatch}`);
  issues.push(...errors.slice(0, 3));
  return issues.length ? issues.join("; ") : "-";
}

function readImportDryRunSummary(item: ImportRecord): Record<string, unknown> | null {
  const summary = item.raw_payload?.skladbot_dry_run;
  return summary && typeof summary === "object" && !Array.isArray(summary)
    ? summary as Record<string, unknown>
    : null;
}

function stringArray(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function stringField(record: unknown, key: string) {
  if (!record || typeof record !== "object" || Array.isArray(record)) return "";
  const value = (record as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

function numberField(record: unknown, key: string) {
  if (!record || typeof record !== "object" || Array.isArray(record)) return 0;
  const value = (record as Record<string, unknown>)[key];
  return typeof value === "number" && Number.isFinite(value) ? value : Number(value || 0) || 0;
}
