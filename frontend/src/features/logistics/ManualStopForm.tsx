import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2, Plus, Save } from "lucide-react";

import type { ClientPoint, LogisticsManualStopPayload, LogisticsManualStopRow } from "../../api";

const DEFAULT_DELIVERY_FROM = "10:00";
const DEFAULT_DELIVERY_TO = "18:00";
const COORDINATES_RE = /^\s*-?\d+(?:[.,]\d+)?\s*,\s*-?\d+(?:[.,]\d+)?\s*$/;

export type ManualStopFormValues = {
  client_name: string;
  point_name: string;
  address: string;
  coordinates: string;
  representative: string;
  delivery_from: string;
  delivery_to: string;
  blocks: string;
  comment: string;
  save_to_directory: boolean;
};

const EMPTY_VALUES: ManualStopFormValues = {
  client_name: "",
  point_name: "",
  address: "",
  coordinates: "",
  representative: "",
  delivery_from: DEFAULT_DELIVERY_FROM,
  delivery_to: DEFAULT_DELIVERY_TO,
  blocks: "0",
  comment: "",
  save_to_directory: true,
};

export function manualStopValuesFromRow(row: LogisticsManualStopRow): ManualStopFormValues {
  return {
    client_name: row.client,
    point_name: row.point_name,
    address: row.address,
    coordinates: row.coordinates,
    representative: row.representative,
    delivery_from: row.delivery_from || DEFAULT_DELIVERY_FROM,
    delivery_to: row.delivery_to || DEFAULT_DELIVERY_TO,
    blocks: String(row.blocks ?? 0),
    comment: row.comment,
    // Точка уже заведена, повторно класть её в справочник незачем
    save_to_directory: false,
  };
}

export function manualStopFormError(values: ManualStopFormValues): string {
  if (!values.client_name.trim()) return "Укажите клиента";
  if (!values.address.trim()) return "Укажите адрес";
  if (!COORDINATES_RE.test(values.coordinates)) {
    return "Координаты вводятся парой чисел через запятую, например 41.311081, 69.240562";
  }
  if (!/^\d+$/.test(values.blocks.trim())) return "Блоки это целое число, ноль допустим";
  if (values.delivery_from >= values.delivery_to) return "Начало окна должно быть раньше конца";
  return "";
}

export function manualStopPayload(
  values: ManualStopFormValues,
  serviceDate: string,
  id?: string,
): LogisticsManualStopPayload {
  return {
    ...(id ? { id } : {}),
    service_date: serviceDate,
    client_name: values.client_name.trim(),
    point_name: values.point_name.trim(),
    address: values.address.trim(),
    coordinates: values.coordinates.trim(),
    representative: values.representative.trim(),
    delivery_from: values.delivery_from,
    delivery_to: values.delivery_to,
    blocks: Number(values.blocks.trim()),
    comment: values.comment.trim(),
    save_to_directory: values.save_to_directory,
  };
}

export function ManualStopForm({
  serviceDate,
  editingId,
  initialValues,
  busy,
  searchResults,
  searching,
  onSearch,
  onSubmit,
  onCancel,
}: {
  serviceDate: string;
  editingId: string;
  initialValues: ManualStopFormValues | null;
  busy: boolean;
  searchResults: ClientPoint[];
  searching: boolean;
  onSearch: (query: string) => void;
  onSubmit: (payload: LogisticsManualStopPayload) => void;
  onCancel: () => void;
}) {
  const [values, setValues] = useState<ManualStopFormValues>(initialValues ?? EMPTY_VALUES);
  const [query, setQuery] = useState("");
  const [touched, setTouched] = useState(false);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setValues(initialValues ?? EMPTY_VALUES);
    setTouched(false);
    setQuery("");
  }, [editingId, initialValues]);

  useEffect(() => () => {
    if (searchTimer.current) clearTimeout(searchTimer.current);
  }, []);

  const error = useMemo(() => manualStopFormError(values), [values]);

  function set<K extends keyof ManualStopFormValues>(key: K, value: ManualStopFormValues[K]) {
    setValues((current) => ({ ...current, [key]: value }));
  }

  function runSearch(value: string) {
    setQuery(value);
    // Пауза перед запросом: справочник ищется по каждому нажатию, а точек
    // на боевой базе тысячи, без неё панель шлёт запрос на каждую букву
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => onSearch(value.trim()), 250);
  }

  function applyPoint(point: ClientPoint) {
    setValues((current) => ({
      ...current,
      client_name: point.client_name,
      point_name: point.point_name,
      address: point.address,
      coordinates: point.coordinates,
      representative: point.representative,
      delivery_from: point.delivery_from || DEFAULT_DELIVERY_FROM,
      delivery_to: point.delivery_to || DEFAULT_DELIVERY_TO,
      // Точка уже в справочнике, дублировать её не нужно
      save_to_directory: false,
    }));
    setQuery("");
    onSearch("");
  }

  function submit() {
    setTouched(true);
    if (error) return;
    onSubmit(manualStopPayload(values, serviceDate, editingId || undefined));
  }

  return (
    <form
      className="manual-stop-form"
      aria-label={editingId ? "Правка ручной точки" : "Новая ручная точка"}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="manual-stop-search">
        <label>
          <span>Найти сохранённую точку</span>
          <input
            type="search"
            value={query}
            placeholder="Название, клиент или адрес"
            onChange={(event) => runSearch(event.target.value)}
          />
        </label>
        {searching && <span className="panel-subtitle"><Loader2 className="spin" size={14} /> Ищем</span>}
        {!searching && query.trim() !== "" && searchResults.length === 0 && (
          <span className="panel-subtitle">Ничего не нашлось, заполните поля руками</span>
        )}
        {searchResults.length > 0 && (
          <ul className="manual-stop-suggestions">
            {searchResults.map((point) => (
              <li key={point.id}>
                <button type="button" onClick={() => applyPoint(point)}>
                  <strong>{point.point_name || point.client_name}</strong>
                  <span>{point.address}</span>
                  {!point.coordinates && <span className="warn">Без координат, впишите руками</span>}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="manual-stop-fields">
        <label className="half">
          <span>Клиент</span>
          <input value={values.client_name} onChange={(event) => set("client_name", event.target.value)} />
        </label>
        <label className="half">
          <span>Название точки</span>
          <input value={values.point_name} onChange={(event) => set("point_name", event.target.value)} />
        </label>
        <label className="wide">
          <span>Адрес</span>
          <input value={values.address} onChange={(event) => set("address", event.target.value)} />
        </label>
        <label className="wide">
          <span>Координаты</span>
          <input
            value={values.coordinates}
            placeholder="41.311081, 69.240562"
            onChange={(event) => set("coordinates", event.target.value)}
          />
        </label>
        <label>
          <span>Представитель</span>
          <input value={values.representative} onChange={(event) => set("representative", event.target.value)} />
        </label>
        <label>
          <span>Блоки</span>
          <input
            type="number"
            min={0}
            step={1}
            value={values.blocks}
            onChange={(event) => set("blocks", event.target.value)}
          />
        </label>
        <label>
          <span>Окно с</span>
          <input type="time" value={values.delivery_from} onChange={(event) => set("delivery_from", event.target.value)} />
        </label>
        <label>
          <span>Окно по</span>
          <input type="time" value={values.delivery_to} onChange={(event) => set("delivery_to", event.target.value)} />
        </label>
        <label className="wide">
          <span>Комментарий</span>
          <input value={values.comment} onChange={(event) => set("comment", event.target.value)} />
        </label>
      </div>

      <label className="manual-stop-checkbox">
        <input
          type="checkbox"
          checked={values.save_to_directory}
          onChange={(event) => set("save_to_directory", event.target.checked)}
        />
        <span>Сохранить в справочник точек</span>
      </label>

      <p className="panel-subtitle">
        Ручная точка едет только в маршрутный лист: заказ на складе не создаётся,
        заявка в СкладБот не уходит, КИЗы не нужны
      </p>

      {touched && error && <p className="alert-bar" role="alert">{error}</p>}

      <div className="action-buttons">
        <button className="primary-button" type="submit" disabled={busy}>
          {busy ? <Loader2 className="spin" size={16} /> : editingId ? <Save size={16} /> : <Plus size={16} />}
          {"Сохранить точку"}
        </button>
        <button className="ghost-button" type="button" onClick={onCancel} disabled={busy}>Отмена</button>
      </div>
    </form>
  );
}
