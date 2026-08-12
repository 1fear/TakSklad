# Расширенный поиск клиентов и Smartup ID из шаблона: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** на вкладке «Клиенты» одно поле находит точку по имени, адресу, координатам,
Smartup ID и SkladBot ID, а Smartup ID подтягивается из колонки «ИД заказа»
шаблона отправки заказов на склад

**Architecture:** бэкенд собирает идентификаторы заказов по тому же `point_key`,
что и остальные агрегаты клиентской точки, канонизирует их через готовые контракты
`skladbot_contracts` и отдаёт полем `search_identifiers`. Фронт добавляет это поле
в уже существующий локальный фильтр и учит его сравнивать координаты без пробелов.
Excel-импорт читает колонку «ИД заказа» и кладёт сделку в личность заказа,
не трогая ни `order_key`, ни ключ склейки позиций

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Pydantic v2, PostgreSQL и SQLite,
React 19, TypeScript, Vite, Vitest

## Global Constraints

- Спец: `docs/superpowers/specs/2026-08-12-client-search-and-smartup-id-design.md`
- Ветка `feat/client-search-smartup-id` в worktree `/tmp/taksklad-client-search`,
  коммит и push только с префиксом `ALLOW_NON_MAIN_BRANCH=1`
- Добавлять в коммит только свои файлы поимённо, `git add -A` и `git add <каталог>` запрещены
- `row["source_order_id"]`, `order_key` и `smartup_deal_id` не меняются ни в одной задаче
- Тексты без длинных тире и без точек в конце предложений
- Проверка: `PYTHONPATH=. python -m unittest discover -s tests`,
  `./tools/run_postgres_tests.sh all`,
  `npm --prefix frontend run lint`, `typecheck`, `test`, `test:coverage`

---

### Task 1: Канонизация идентификаторов клиентской точки

**Files:**
- Modify: `backend/app/client_points_service.py`
- Test: `tests/test_import_client_point_prefetch.py`

**Interfaces:**
- Produces: `canonical_search_identifiers(value) -> str`, принимает сырую склейку
  значений из raw_payload и возвращает дедуплицированную строку канонических
  идентификаторов через пробел

- [ ] **Step 1: Написать падающий тест**

В конец `tests/test_import_client_point_prefetch.py` добавить класс:

```python
class ClientPointSearchIdentifierTests(unittest.TestCase):
    def test_canonical_identifiers_keep_known_shapes_and_drop_import_hashes(self):
        raw = " ".join([
            "smartup:266627707",
            "WH-R-2026-0001",
            "1002",
            "WR-RET-1",
            "9f2c1b" + "0" * 58,
            '["266968926", "267807389"]',
            "",
        ])

        self.assertEqual(
            canonical_search_identifiers(raw),
            "266627707 WH-R-2026-0001 1002 WR-RET-1 266968926 267807389",
        )

    def test_canonical_identifiers_deduplicate_and_survive_empty_input(self):
        self.assertEqual(canonical_search_identifiers(None), "")
        self.assertEqual(
            canonical_search_identifiers("smartup:731 731 smartup:731"),
            "731",
        )
```

И дописать импорт в существующий блок импорта из `client_points_service`:

```python
from backend.app.client_points_service import (
    canonical_search_identifiers,
    list_client_points,
    prefetch_client_points_for_import,
    sync_client_point_from_import_row_cached,
)
```

- [ ] **Step 2: Запустить и убедиться, что тест падает**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest tests.test_import_client_point_prefetch -v`
Expected: FAIL с `ImportError: cannot import name 'canonical_search_identifiers'`

- [ ] **Step 3: Реализовать канонизацию**

В `backend/app/client_points_service.py` расширить импорт из `.skladbot_contracts`:

```python
from .skladbot_contracts import (
    canonical_remote_request_id,
    canonical_skladbot_request_number,
    format_internal_smartup_ids,
    internal_smartup_id_from_source,
)
```

и добавить после `DEFAULT_DELIVERY_TO`:

```python
SEARCH_IDENTIFIER_TOKEN_RE = re.compile(r"[^0-9A-Za-z:_-]+")


def canonical_search_identifiers(value) -> str:
    """Канонические идентификаторы точки: сделки Smartup и заявки SkladBot.

    Сырая склейка приходит из raw_payload заказов и позиций и содержит мусор:
    синтетические sha256-хеши Excel-импорта и JSON-скобки списков. Всё, что не
    опознано контрактами, отбрасывается, иначе поиск ловит случайные совпадения,
    а ответ API распухает на 64 символа с каждого заказа
    """
    ordered = []
    seen = set()
    for token in SEARCH_IDENTIFIER_TOKEN_RE.split(normalize_text(value)):
        canonical = canonical_search_identifier(token)
        if canonical and canonical not in seen:
            seen.add(canonical)
            ordered.append(canonical)
    return " ".join(ordered)


def canonical_search_identifier(token) -> str:
    smartup_id = internal_smartup_id_from_source(token)
    if smartup_id:
        return smartup_id
    request_number = canonical_skladbot_request_number(token)
    if request_number:
        return request_number
    return canonical_remote_request_id(token)
```

- [ ] **Step 4: Запустить тест повторно**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest tests.test_import_client_point_prefetch -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
cd /tmp/taksklad-client-search && ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(clients): канонизация идентификаторов клиентской точки" backend/app/client_points_service.py tests/test_import_client_point_prefetch.py
```

---

### Task 2: Агрегат идентификаторов в списке клиентских точек

**Files:**
- Modify: `backend/app/client_points_service.py:60-181`, `:353-458`, `:644-666`
- Modify: `backend/app/schemas.py:524-541`
- Test: `tests/test_backend_api_persistence.py`

**Interfaces:**
- Consumes: `canonical_search_identifiers` из Task 1
- Produces: поле `search_identifiers` в `ClientPointRead` и в словарях
  `list_client_points`, `client_point_to_read`, `build_order_point_meta`;
  функция `order_identifier_cte(db)` возвращает CTE с колонками
  `point_key` и `identifiers`

- [ ] **Step 1: Написать падающий тест**

В `tests/test_backend_api_persistence.py` рядом с
`test_admin_client_points_lists_order_points_and_updates_timeslot` добавить:

```python
    def test_admin_client_points_expose_and_search_order_identifiers(self):
        with self.SessionLocal() as db:
            order = Order(
                payment_type="cash",
                client="Identifier Client",
                address="Identifier Address",
                representative="Rep",
                order_date=date(2026, 8, 13),
                status="not_completed",
                raw_payload={
                    "source": "telegram",
                    "coordinates": "41.296549, 69.277177",
                    "source_order_id": "smartup:266627707",
                    "skladbot_request_number": "WH-R-2026-0001",
                    "skladbot_request_id": "1002",
                },
            )
            item = OrderItem(
                order=order,
                product="Chapman Brown OP 20",
                quantity_pieces=20,
                quantity_blocks=2,
                pieces_per_block=10,
                scanned_blocks=0,
                requires_kiz=True,
                status="not_completed",
                raw_payload={
                    "source_order_id": "b" * 64,
                    "smartup_order_ids": ["266968926"],
                },
            )
            db.add_all([order, item])
            db.commit()

        listed = self.client.get("/api/v1/admin/client-points")

        self.assertEqual(listed.status_code, 200)
        identifiers = listed.json()[0]["search_identifiers"].split()
        self.assertIn("266627707", identifiers)
        self.assertIn("266968926", identifiers)
        self.assertIn("WH-R-2026-0001", identifiers)
        self.assertIn("1002", identifiers)
        self.assertNotIn("b" * 64, identifiers)

        for query in ("266627707", "266968926", "WH-R-2026-0001", "41.296549,69.277177"):
            with self.subTest(query=query):
                found = self.client.get("/api/v1/admin/client-points", params={"query": query})
                self.assertEqual(found.status_code, 200)
                self.assertEqual([point["client_name"] for point in found.json()], ["Identifier Client"])

        missing = self.client.get("/api/v1/admin/client-points", params={"query": "266627708"})
        self.assertEqual(missing.json(), [])

    def test_client_point_timeslot_response_carries_search_identifiers(self):
        with self.SessionLocal() as db:
            order = Order(
                payment_type="cash",
                client="Slot Client",
                address="Slot Address",
                order_date=date(2026, 8, 13),
                status="not_completed",
                raw_payload={"source": "telegram", "source_order_id": "smartup:268031619"},
            )
            db.add(order)
            db.commit()

        updated = self.client.post(
            "/api/v1/admin/client-points/timeslot",
            json={
                "client_name": "Slot Client",
                "address": "Slot Address",
                "delivery_from": "09:30",
                "delivery_to": "12:00",
                "actor": "web",
                "reason": "синтетическая проверка",
            },
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["search_identifiers"], "268031619")
```

- [ ] **Step 2: Запустить и убедиться, что тест падает**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_expose_and_search_order_identifiers -v`
Expected: FAIL с `KeyError: 'search_identifiers'`

- [ ] **Step 3: Добавить поле в схему**

В `backend/app/schemas.py` в `ClientPointRead` после `has_custom_timeslot`:

```python
    search_identifiers: str = ""
```

- [ ] **Step 4: Реализовать агрегат и расширить поиск**

В `backend/app/client_points_service.py` добавить рядом с `sql_search_text`:

```python
def sql_compact_text(value):
    return func.replace(value, " ", "")


def sql_join_with_space(parts):
    joined = parts[0]
    for part in parts[1:]:
        joined = joined + literal(" ") + part
    return joined


def sql_group_concat(db: Session, column):
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        return func.string_agg(column, literal(" "), type_=String())
    return func.group_concat(column, " ", type_=String())


def json_text(column, key):
    return func.coalesce(column[key].as_string(), "")


ORDER_IDENTIFIER_KEYS = (
    "source_order_id",
    "skladbot_request_number",
    "skladbot_request_id",
    "skladbot_return_request_number",
    "skladbot_return_request_id",
)
ITEM_IDENTIFIER_KEYS = ("source_order_id", "smartup_order_ids")


def order_identifier_cte(db: Session):
    """Сырые идентификаторы заказов, сгруппированные по ключу клиентской точки"""
    order_key = sql_point_key(db, Order.client)
    identifiers = sql_join_with_space([
        *(json_text(Order.raw_payload, key) for key in ORDER_IDENTIFIER_KEYS),
        *(json_text(OrderItem.raw_payload, key) for key in ITEM_IDENTIFIER_KEYS),
    ])
    return (
        select(
            order_key.label("point_key"),
            sql_group_concat(db, identifiers).label("identifiers"),
        )
        .select_from(Order)
        .outerjoin(OrderItem, OrderItem.order_id == Order.id)
        .where(order_key != "")
        .group_by(order_key)
        .cte("client_point_order_identifiers")
    )
```

Расширить импорт SQLAlchemy в шапке файла:

```python
from sqlalchemy import String, case, func, literal, or_, select, union
```

(`String` и `literal` уже импортированы, менять строку не нужно)

В `list_client_points` после `order_aggregate, order_display, ... = order_point_ctes(db)` добавить:

```python
    order_identifiers = order_identifier_cte(db)
```

Заменить блок `searchable = (...)` на:

```python
    identifiers = func.coalesce(order_identifiers.c.identifiers, "")
    searchable = sql_join_with_space([
        client_name,
        point_name,
        address,
        representative,
        coordinates,
        # Координаты без пробелов: импорт хранит «41.296549, 69.277177»,
        # а в шаблоне и в буфере обмена они выглядят как «41.296549,69.277177»
        sql_compact_text(coordinates),
        identifiers,
    ])
```

В `select(...)` добавить колонку после `has_custom_timeslot.label("has_custom_timeslot")`:

```python
            identifiers.label("order_identifiers"),
```

В цепочку `outerjoin` добавить:

```python
        .outerjoin(order_identifiers, order_identifiers.c.point_key == point_keys.c.point_key)
```

Заменить блок фильтра по запросу на:

```python
    normalized_query = normalize_search_text(query)
    compact_query = compact_search_text(query)
    if normalized_query:
        matches = [sql_search_text(db, searchable).contains(normalized_query, autoescape=True)]
        if compact_query and compact_query != normalized_query:
            matches.append(sql_search_text(db, searchable).contains(compact_query, autoescape=True))
        statement = statement.where(or_(*matches))
```

В словарь строки после `"has_custom_timeslot"` добавить:

```python
            "search_identifiers": canonical_search_identifiers(row["order_identifiers"]),
```

Рядом с `normalize_search_text` добавить:

```python
def compact_search_text(value):
    return re.sub(r"\s+", "", normalize_search_text(value))
```

В `build_order_point_meta` добавить `identifiers = order_identifier_cte(db)`,
в `statement` дописать `.outerjoin(identifiers, identifiers.c.point_key == aggregate.c.point_key)`
и колонку `identifiers.c.identifiers`, а в словарь `meta_by_key[key]` добавить:

```python
            "search_identifiers": canonical_search_identifiers(row["identifiers"]),
```

В `client_point_to_read` добавить в возвращаемый словарь:

```python
        "search_identifiers": meta.get("search_identifiers") or "",
```

- [ ] **Step 5: Запустить тесты**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest tests.test_backend_api_persistence tests.test_import_client_point_prefetch -v`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
cd /tmp/taksklad-client-search && ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(clients): поиск клиентов по Smartup ID, заявкам SkladBot и координатам" backend/app/client_points_service.py backend/app/schemas.py tests/test_backend_api_persistence.py
```

---

### Task 3: Excel-импорт читает колонку «ИД заказа»

**Files:**
- Modify: `backend/app/excel_importer.py:121-184`, `:815-846`
- Modify: `backend/app/schemas.py:18-33`
- Test: `tests/test_backend_telegram_import.py`

**Interfaces:**
- Produces: ключ строки импорта `"Smartup ИД заказа"` с сырым текстом ячейки

- [ ] **Step 1: Написать падающий тест**

В `tests/test_backend_telegram_import.py` рядом с другими тестами
`excel_file_to_import_payload` добавить метод класса `BackendTelegramImportTests`:

```python
    def test_excel_file_to_import_payload_reads_smartup_order_id_column(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "shipment_template.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "Конструктор отчётов по продажам"
            sheet.append([
                "ИД заказа",
                "Клиент",
                "GPS-координаты клиента",
                "Торговый представитель",
                "Тип оплаты",
                "Статус",
                "ТМЦ",
                "Количество заказа",
                "Дата доставки",
            ])
            sheet.append([
                "266627707",
                "JASUR-DIYOR UNIVERSAL XK",
                "41.296549,69.277177",
                "ТП5 Авазов Азиз Бегжонович",
                "Перечисление",
                "В обработке",
                "Chapman Brown OP 20",
                10,
                "13.08.2026",
            ])
            workbook.save(path)

            payload = excel_file_to_import_payload(
                path,
                file_name=path.name,
                source="telegram",
                shipment_date="13.08.2026",
            )

        row = payload["rows"][0]
        self.assertEqual(row["Smartup ИД заказа"], "266627707")
        self.assertEqual(row["Координаты"], "41.296549, 69.277177")
        self.assertEqual(row["Клиент"], "JASUR-DIYOR UNIVERSAL XK")
```

- [ ] **Step 2: Запустить и убедиться, что тест падает**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest tests.test_backend_telegram_import.BackendTelegramImportTests.test_excel_file_to_import_payload_reads_smartup_order_id_column -v`
Expected: FAIL с `KeyError: 'Smartup ИД заказа'`

- [ ] **Step 3: Реализовать чтение колонки**

В `backend/app/excel_importer.py` в `OPTIONAL_ALIASES` после блока
`"skladbot_request_id"` добавить:

```python
    # Латинская «ID заказа» сюда не берётся: так называется колонка с UUID
    # в собственном экспорте заказов TakSklad
    "smartup_order_id": [
        "ИД заказа",
        "Идентификатор заказа",
        "Smartup ИД заказа",
        "ID заказа Smartup",
        "Smartup ID",
    ],
```

В словарь `rows.append({...})` после `"ID заявки SkladBot"` добавить:

```python
                "Smartup ИД заказа": get_cell(row, columns.get("smartup_order_id")),
```

В `backend/app/schemas.py` в `ImportFieldName` в строку со Smartup-полями добавить
`"Smartup ИД заказа", "smartup_order_id",`

- [ ] **Step 4: Запустить тест повторно**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest tests.test_backend_telegram_import.BackendTelegramImportTests.test_excel_file_to_import_payload_reads_smartup_order_id_column -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
cd /tmp/taksklad-client-search && ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(import): Excel-импорт читает колонку ИД заказа из шаблона склада" backend/app/excel_importer.py backend/app/schemas.py tests/test_backend_telegram_import.py
```

---

### Task 4: Smartup ID становится личностью заказа

**Files:**
- Modify: `backend/app/imports_service.py:47-56`, `:236-290`, `:844-880`, `:958-1003`, `:1088-1180`
- Test: `tests/test_import_client_point_prefetch.py`

**Interfaces:**
- Consumes: ключ `"Smartup ИД заказа"` из Task 3
- Produces: `normalize_smartup_order_id(value) -> str`, поле строки
  `row["smartup_order_id"]`, ключ `raw_payload["smartup_order_ids"]` у позиции,
  значение `smartup:<id>` в `orders.raw_payload["source_order_id"]`

- [ ] **Step 1: Написать падающий тест**

В `tests/test_import_client_point_prefetch.py` добавить класс:

```python
class ImportSmartupOrderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    def template_row(self, smartup_order_id, product, import_id):
        return {
            "Дата отгрузки": "13.08.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "JASUR-DIYOR UNIVERSAL XK",
            "Адрес": "Ташкент, Чиланзар 10",
            "Координаты": "41.296549, 69.277177",
            "Торговый представитель": "ТП5",
            "Товары": product,
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "ID импорта": import_id,
            "Smartup ИД заказа": smartup_order_id,
        }

    def run_import(self, rows, filename):
        skladbot_result = {
            "status": "synthetic_stub",
            "ready": 0,
            "blocked": 0,
            "already_linked": 0,
            "linked_mismatch": 0,
            "event_id": "",
        }
        with (
            self.SessionLocal() as db,
            patch(
                "backend.app.imports_service.create_skladbot_dry_run_for_import",
                return_value=skladbot_result,
            ),
        ):
            return create_import(db, ImportCreate(source="telegram", filename=filename, rows=rows))

    def test_template_order_id_becomes_smartup_identity_without_changing_grouping(self):
        self.run_import(
            [
                self.template_row("266627707", "Chapman Brown OP 20", "row-1"),
                self.template_row("266627707", "Chapman Green OP 20", "row-2"),
            ],
            "template.xlsx",
        )

        with self.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(OrderItem)), 2)
            order = db.execute(select(Order)).scalar_one()
            self.assertEqual(order.raw_payload["source_order_id"], "smartup:266627707")
            for item in db.execute(select(OrderItem)).scalars():
                self.assertEqual(item.raw_payload["smartup_order_ids"], ["266627707"])
                self.assertEqual(len(item.raw_payload["source_order_id"]), 64)

    def test_second_deal_merged_into_one_position_keeps_both_identifiers(self):
        self.run_import(
            [
                self.template_row("266627707", "Chapman Brown OP 20", "row-1"),
                self.template_row("266968926", "Chapman Brown OP 20", "row-2"),
            ],
            "two-deals.xlsx",
        )

        with self.SessionLocal() as db:
            item = db.execute(select(OrderItem)).scalar_one()
            self.assertEqual(item.quantity_blocks, 2)
            self.assertEqual(
                sorted(item.raw_payload["smartup_order_ids"]),
                ["266627707", "266968926"],
            )
            points = list_client_points(db, query="266968926")
            self.assertEqual([point["client_name"] for point in points], ["JASUR-DIYOR UNIVERSAL XK"])
            self.assertIn("266968926", points[0]["search_identifiers"].split())

    def test_row_without_template_order_id_keeps_synthetic_identity(self):
        row = self.template_row("", "Chapman RED OP 20", "row-1")
        self.run_import([row], "no-order-id.xlsx")

        with self.SessionLocal() as db:
            order = db.execute(select(Order)).scalar_one()
            self.assertEqual(len(order.raw_payload["source_order_id"]), 64)
            item = db.execute(select(OrderItem)).scalar_one()
            self.assertEqual(item.raw_payload["smartup_order_ids"], [])
```

Дописать импорты в шапку файла:

```python
from sqlalchemy.pool import StaticPool

from backend.app.imports_service import create_import, normalize_smartup_order_id
```

(`StaticPool`, `create_import`, `patch`, `func`, `select` уже импортированы,
добавить нужно только `normalize_smartup_order_id` в существующий импорт
из `backend.app.imports_service`)

- [ ] **Step 2: Запустить и убедиться, что тест падает**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest tests.test_import_client_point_prefetch.ImportSmartupOrderIdentityTests -v`
Expected: FAIL с `ImportError: cannot import name 'normalize_smartup_order_id'`

- [ ] **Step 3: Реализовать личность заказа**

В `backend/app/imports_service.py` после `SMARTUP_DEAL_FIELDS` добавить:

```python
SMARTUP_ORDER_ID_FIELDS = ("Smartup ИД заказа", "smartup_order_id")
```

Рядом с `normalize_text` добавить:

```python
def normalize_smartup_order_id(value):
    """Сделка Smartup из шаблона отправки заказов, иначе пустая строка.

    Формат проверяет тот же контракт, что и автоимпорт Smartup, поэтому
    ячейка-число «266627707.0» и мусор в колонке отсекаются одинаково
    """
    text = normalize_text(value)
    if text.endswith(".0"):
        text = text[:-2]
    return internal_smartup_id_from_source(f"smartup:{text}")
```

В `normalize_import_row` рядом с `smartup_deal_id` добавить:

```python
    smartup_order_id = normalize_smartup_order_id(first_value(raw_row, SMARTUP_ORDER_ID_FIELDS))
```

и в возвращаемый словарь после `"smartup_deal_id"`:

```python
        "smartup_order_id": smartup_order_id,
```

В `build_order_raw_payload` заменить значение `"source_order_id"`:

```python
    smartup_order_id = normalize_text(row.get("smartup_order_id"))
    payload = {
        "order_key": order_key,
        "skladbot_request_number": row["skladbot_request_number"],
        "skladbot_request_id": row["skladbot_request_id"],
        "coordinates": row["coordinates"],
        "source": import_source,
        # Личность заказа это сделка Smartup из шаблона, если файл её принёс.
        # row["source_order_id"] остаётся синтетическим хешем и живёт у позиции:
        # он держит склейку позиций и публичный внешний идентификатор логистики
        "source_order_id": f"smartup:{smartup_order_id}" if smartup_order_id else row["source_order_id"],
        "source_import_id": row["source_import_id"],
        "source_batch_key": row["source_batch_key"],
    }
```

В словарь `raw_payload` создаваемой позиции после `"source_order_id"` добавить:

```python
                "smartup_order_ids": [row["smartup_order_id"]] if row["smartup_order_id"] else [],
```

В `merge_import_row_into_item` после блока `source_import_ids` добавить:

```python
    smartup_order_ids = list(raw_payload.get("smartup_order_ids") or [])
    new_smartup_order_id = normalize_text(row.get("smartup_order_id"))
    if new_smartup_order_id and new_smartup_order_id not in smartup_order_ids:
        smartup_order_ids.append(new_smartup_order_id)
    raw_payload["smartup_order_ids"] = smartup_order_ids
```

В блоке, где заказ уже существует, расширить условие сохранения личности:

```python
        elif import_job.source == SMARTUP_AUTO_IMPORT_SOURCE:
            preserve_order_smartup_identity(order, row.get("source_order_id"))
        elif row.get("smartup_order_id"):
            # Дозаказ и повторный импорт заполняют пустую личность и никогда
            # не перетирают уже сохранённую сделку
            preserve_order_smartup_identity(order, f"smartup:{row['smartup_order_id']}")
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest tests.test_import_client_point_prefetch -v`
Expected: PASS

- [ ] **Step 5: Проверить, что логистика и склейка не поехали**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest tests.test_postgres_import_identity tests.test_telegram_import_idempotency tests.test_smartup_auto_import -v`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
cd /tmp/taksklad-client-search && ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(import): Smartup ID из шаблона становится личностью заказа" backend/app/imports_service.py tests/test_import_client_point_prefetch.py
```

---

### Task 5: Фронтенд ищет по идентификаторам и координатам без пробелов

**Files:**
- Modify: `frontend/src/api.ts:429-447`
- Modify: `frontend/src/workspace/AdminWorkspace.tsx:2970-2986`, поле поиска в `ClientsPanel`
- Modify: `frontend/src/__tests__/fixtures.ts:205-224`
- Test: `frontend/src/__tests__/App.characterization.test.tsx`

**Interfaces:**
- Consumes: поле `search_identifiers` из Task 2

- [ ] **Step 1: Написать падающий тест**

В `frontend/src/__tests__/App.characterization.test.tsx` добавить тест рядом
с `filters client points, expands order history and saves a timeslot`:

```tsx
  it("finds a client point by Smartup ID, SkladBot number and spaced coordinates", async () => {
    const { user } = await renderAuthenticatedAdminApp();
    await user.click(screen.getByRole("button", { name: "Клиенты" }));
    expect(await screen.findByRole("heading", { name: "Клиенты и таймслоты" })).toBeInTheDocument();

    const clientSearch = screen.getByRole("searchbox", { name: "Поиск клиентов" });
    for (const query of ["266627707", "WH-R-TEST-1", "41.296549,69.277177"]) {
      await user.clear(clientSearch);
      await user.type(clientSearch, query);
      expect(screen.getByText("Клиент Альфа")).toBeInTheDocument();
    }

    await user.clear(clientSearch);
    await user.type(clientSearch, "266627708");
    expect(screen.getByText("Нет данных")).toBeInTheDocument();
  });
```

В `frontend/src/__tests__/fixtures.ts` в объекте `clientPoint` задать координаты
с пробелом и идентификаторы:

```ts
  coordinates: "41.296549, 69.277177",
  search_identifiers: "266627707 WH-R-TEST-1 1002",
```

- [ ] **Step 2: Запустить и убедиться, что тест падает**

Run: `cd /tmp/taksklad-client-search && npm --prefix frontend run test -- App.characterization`
Expected: FAIL, поиск по `266627707` не находит точку и `search_identifiers` не проходит типизацию

- [ ] **Step 3: Расширить тип и фильтр**

В `frontend/src/api.ts` в тип `ClientPoint` после `has_custom_timeslot` добавить:

```ts
  search_identifiers: string;
```

В `frontend/src/workspace/AdminWorkspace.tsx` заменить `filterClientPoints`:

```tsx
function filterClientPoints(points: ClientPoint[], search: string, timeslotFilter: ClientTimeslotFilter) {
  const query = search.trim().toLowerCase();
  const compactQuery = query.replace(/\s+/g, "");
  return points.filter((point) => {
    if (timeslotFilter === "custom" && !point.has_custom_timeslot) return false;
    if (timeslotFilter === "default" && point.has_custom_timeslot) return false;
    if (!query) return true;
    return [
      point.client_name,
      point.point_name,
      point.address,
      point.coordinates,
      point.representative,
      point.delivery_from,
      point.delivery_to,
      point.search_identifiers,
    ].some((value) => matchesClientPointQuery(value, query, compactQuery));
  });
}

// Координаты приходят как «41.296549, 69.277177», а из шаблона и буфера обмена
// их вставляют как «41.296549,69.277177»: сравниваем обе формы в обе стороны
function matchesClientPointQuery(value: string, query: string, compactQuery: string) {
  const text = (value || "").toLowerCase();
  if (text.includes(query)) return true;
  return Boolean(compactQuery) && text.replace(/\s+/g, "").includes(compactQuery);
}
```

В `ClientsPanel` заменить placeholder поля поиска, `aria-label` не трогать:

```tsx
          <input type="search" value={search} onChange={(event) => onSearchChange(event.target.value)} placeholder="Клиент, адрес, координаты, Smartup ID, SkladBot ID" aria-label="Поиск клиентов" />
```

- [ ] **Step 4: Запустить тесты и линт**

Run: `cd /tmp/taksklad-client-search && npm --prefix frontend run lint && npm --prefix frontend run typecheck && npm --prefix frontend run test`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
cd /tmp/taksklad-client-search && ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(web): поиск клиентов по Smartup ID, SkladBot ID и координатам" frontend/src/api.ts frontend/src/workspace/AdminWorkspace.tsx frontend/src/__tests__/fixtures.ts frontend/src/__tests__/App.characterization.test.tsx
```

---

### Task 6: Полный прогон и доставка

**Files:**
- Modify: нет, только проверка и PR

- [ ] **Step 1: Прогнать бэкенд целиком**

Run: `cd /tmp/taksklad-client-search && PYTHONPATH=. python -m unittest discover -s tests`
Expected: OK без FAIL и ERROR

- [ ] **Step 2: Прогнать матрицу PostgreSQL**

Run: `cd /tmp/taksklad-client-search && ./tools/run_postgres_tests.sh all`
Expected: OK

- [ ] **Step 3: Прогнать фронтенд с покрытием**

Run: `cd /tmp/taksklad-client-search && npm --prefix frontend run lint && npm --prefix frontend run typecheck && npm --prefix frontend run test && npm --prefix frontend run test:coverage`
Expected: PASS, порог покрытия не пробит

- [ ] **Step 4: Проверить на настоящем шаблоне**

Прогнать `excel_file_to_import_payload` на
`/Users/anton/Documents/Telegram/Шаблон_отправки_заказов_на_склад_13_08_2026.xlsx`
и убедиться, что все 99 строк получили `Smartup ИД заказа` и что уникальных
значений ровно 30

- [ ] **Step 5: Сверить состав коммитов и запушить**

```bash
cd /tmp/taksklad-client-search && git show --stat HEAD && git log --oneline origin/main..HEAD && ALLOW_NON_MAIN_BRANCH=1 git push -u origin feat/client-search-smartup-id
```

- [ ] **Step 6: Открыть PR и дождаться зелёного Release gate**

```bash
gh pr create --repo 1fear/TakSklad --base main --head feat/client-search-smartup-id --title "feat(clients): расширенный поиск клиентов и Smartup ID из шаблона склада" --body "..."
```

- [ ] **Step 7: Squash-мерж и уборка worktree**

```bash
gh pr merge --repo 1fear/TakSklad --squash --delete-branch <номер>
git -C /Users/anton/Documents/work/TakSklad worktree remove /tmp/taksklad-client-search
```
