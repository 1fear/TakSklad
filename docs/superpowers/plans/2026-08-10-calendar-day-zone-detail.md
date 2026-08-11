# Детализация дня в календаре логистики, план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** клик по дню в календаре логистики раскрывает детализацию с разбивкой
город и область и списком заказов дня на двух вкладках

**Architecture:** зона нигде не хранится, её считает существующая `classify_order`
из `logistics_zone_service.py`, поэтому месячная сводка получает зональные счётчики,
а построчный список заказов дня отдаётся новым эндпоинтом и новым сервисом
Фронт получает отдельный компонент детализации, панель календаря теряет боковую
колонку и отдаёт её место сетке

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, FastAPI, Pydantic v2, unittest,
React 18, TypeScript, Vitest, Testing Library, MSW

**Спека:** `docs/superpowers/specs/2026-08-10-calendar-day-zone-detail-design.md`

## Global Constraints

- Тесты бэкенда запускаются через unittest, не pytest:
  `PYTHONPATH=. python -m unittest discover -s tests`
- Фронт проверяется тремя командами:
  `npm --prefix frontend run lint`, `npm --prefix frontend run typecheck`,
  `npm --prefix frontend run test`
- Доставка только веткой от `origin/main` и squash-мержем PR, прямой push в `main`
  отклоняется. Коммит и push из ветки требуют префикса `ALLOW_NON_MAIN_BRANCH=1`
- Репозиторий публичный: имена контрагентов, координаты, содержимое адресной
  программы не попадают в код, тесты, фикстуры, логи и документы
  В тестах используются вымышленные имена вида `Тест Клиент 1`
- Классификация зоны только через `classify_order` и `load_region_index` из
  `backend/app/logistics_zone_service.py`, копия правил запрещена
- XLSX-отчёт логистики, его имя, содержимое, Telegram-маршруты и расписания
  не меняются
- Client-facing изменение веб-UI согласовано по макетам от 2026-08-10, состав
  колонок и вкладок менять без нового согласования нельзя
- Права: чтение календаря по `client_points:read`, запись дня остаётся за
  `require_admin_write_permission`, новых write-эндпоинтов нет

## Структура файлов

| Файл | Ответственность |
|------|-----------------|
| `backend/app/logistics_calendar_service.py` | месячная сводка, теперь считает зональные счётчики |
| `backend/app/logistics_calendar_orders_service.py` | новый, построчный список заказов дня с зоной |
| `backend/app/schemas.py` | схемы ответов календаря и списка заказов дня |
| `backend/app/main.py` | новый GET-роут списка заказов дня |
| `frontend/src/api.ts` | типы `LogisticsCalendarDay`, `LogisticsCalendarDayOrders`, вызовы, скачивание отчёта |
| `frontend/src/features/logistics/CalendarDayDetail.tsx` | новый, шапка дня, карточки зон, вкладки, списки |
| `frontend/src/workspace/AdminWorkspace.tsx` | панель календаря без боковой колонки, загрузка заказов дня |
| `frontend/src/styles.css` | стили детализации, вкладок и таблицы |
| `tests/test_logistics_calendar_zone_summary.py` | новый, инварианты сумм и страховка пустого справочника |
| `tests/test_logistics_calendar_day_orders.py` | новый, контракт списка заказов дня |
| `frontend/src/__tests__/CalendarDayDetail.test.tsx` | новый, поведение детализации |

---

### Task 1: Зональные счётчики в месячной сводке

**Files:**
- Modify: `backend/app/logistics_calendar_service.py:26-60` (`list_logistics_calendar`), `:163-202` (`calendar_order_summary`)
- Modify: `backend/app/schemas.py:827-847`
- Test: `tests/test_logistics_calendar_zone_summary.py`

**Interfaces:**
- Consumes: `classify_order`, `load_region_index`, `ZONE_CITY`, `ZONE_REGION` из `backend/app/logistics_zone_service.py`
- Produces: в каждом дне ответа появляются `city_orders`, `region_orders`,
  `city_returns`, `region_returns`, `city_blocks`, `region_blocks`,
  `excluded_orders`, на уровне календаря `region_directory_empty: bool`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_logistics_calendar_zone_summary.py`:

```python
import unittest
import uuid
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.logistics_calendar_service import list_logistics_calendar
from backend.app.logistics_zone_service import normalize_client_key
from backend.app.models import Base, LogisticsRegionPoint, Order, OrderItem


def make_order(db, *, client, address, coordinates, blocks, status="not_completed", returned=False):
    order = Order(
        id=uuid.uuid4(),
        source="test",
        order_date=date(2026, 8, 7),
        payment_type="Наличные",
        client=client,
        address=address,
        status="returned" if returned else status,
        raw_payload={"coordinates": coordinates},
    )
    order.items = [OrderItem(id=uuid.uuid4(), product="Тест Товар", quantity_blocks=blocks)]
    db.add(order)
    return order


class LogisticsCalendarZoneSummaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_zone_counters_split_orders_returns_and_blocks(self):
        with self.Session() as db:
            db.add(LogisticsRegionPoint(
                id=uuid.uuid4(),
                client_name="Тест Клиент 2",
                normalized_client=normalize_client_key("Тест Клиент 2"),
                latitude=41.2,
                longitude=69.9,
                is_active=True,
            ))
            make_order(db, client="Тест Клиент 1", address="Ташкент, дом 1", coordinates="41.3200,69.2400", blocks=10)
            make_order(db, client="Тест Клиент 2", address="Область, дом 2", coordinates="41.3200,69.2400", blocks=4)
            make_order(db, client="Тест Клиент 3", address="Ташкент, дом 3", coordinates="41.3200,69.2400", blocks=6, returned=True)
            db.commit()

            calendar = list_logistics_calendar(db, "2026-08")
            day = next(item for item in calendar["days"] if item["date"] == date(2026, 8, 7))

            self.assertEqual(day["city_orders"], 1)
            self.assertEqual(day["region_orders"], 1)
            self.assertEqual(day["city_returns"], 1)
            self.assertEqual(day["region_returns"], 0)
            self.assertEqual(day["city_blocks"], 10)
            self.assertEqual(day["region_blocks"], 4)
            self.assertEqual(day["city_orders"] + day["region_orders"], day["orders_count"])
            self.assertEqual(day["city_returns"] + day["region_returns"], day["returned_orders"])
            self.assertEqual(day["city_blocks"] + day["region_blocks"], day["planned_blocks"])
            self.assertFalse(calendar["region_directory_empty"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

```bash
PYTHONPATH=. python -m unittest tests.test_logistics_calendar_zone_summary -v
```

Ожидается: KeyError на `city_orders`

- [ ] **Step 3: Реализовать зональный подсчёт**

В `backend/app/logistics_calendar_service.py` дописать импорт:

```python
from .logistics_zone_service import ZONE_CITY, ZONE_REGION, classify_order, load_region_index
```

Переписать `calendar_order_summary`, добавив классификацию и возврат флага справочника:

```python
def calendar_order_summary(db: Session, first_day: date, last_day: date) -> tuple[dict[date, dict[str, Any]], bool]:
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.order_date >= first_day)
        .where(Order.order_date <= last_day)
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    ).scalars().all()
    region_index = load_region_index(db)
    region_directory_empty = len(region_index) == 0
    summary: dict[date, dict[str, Any]] = {}
    for order in orders:
        returned_order = is_returned_order(order)
        service_date = order.order_date
        if not isinstance(service_date, date):
            continue
        day = summary.setdefault(service_date, empty_calendar_day_summary())
        if not returned_order and not is_logistics_candidate_order(order):
            day["excluded_orders"] += 1
            continue
        zone = ZONE_CITY if region_directory_empty else classify_order(
            order.client,
            (order.raw_payload or {}).get("coordinates"),
            region_index,
        )
        prefix = "city" if zone == ZONE_CITY else "region"
        if returned_order:
            day["returned_orders"] += 1
            day[f"{prefix}_returns"] += 1
            if order.client and order.client not in day["clients"]:
                day["clients"].append(order.client)
            continue
        day["orders_count"] += 1
        day[f"{prefix}_orders"] += 1
        if order.status == "completed":
            day["completed_orders"] += 1
        else:
            day["active_orders"] += 1
        blocks = sum(int(item.quantity_blocks or 0) for item in order.items)
        day["planned_blocks"] += blocks
        day[f"{prefix}_blocks"] += blocks
        if order.client and order.client not in day["clients"]:
            day["clients"].append(order.client)
    for day in summary.values():
        day["clients"] = day["clients"][:6]
    return summary, region_directory_empty


def empty_calendar_day_summary() -> dict[str, Any]:
    return {
        "orders_count": 0,
        "active_orders": 0,
        "completed_orders": 0,
        "returned_orders": 0,
        "planned_blocks": 0,
        "city_orders": 0,
        "region_orders": 0,
        "city_returns": 0,
        "region_returns": 0,
        "city_blocks": 0,
        "region_blocks": 0,
        "excluded_orders": 0,
        "clients": [],
    }
```

В `list_logistics_calendar` принять кортеж и разложить новые поля в день:

```python
    order_summary, region_directory_empty = calendar_order_summary(db, first_day, last_day)
```

в теле цикла после `"clients": summary.get("clients") or [],` добавить:

```python
            "city_orders": int(summary.get("city_orders") or 0),
            "region_orders": int(summary.get("region_orders") or 0),
            "city_returns": int(summary.get("city_returns") or 0),
            "region_returns": int(summary.get("region_returns") or 0),
            "city_blocks": int(summary.get("city_blocks") or 0),
            "region_blocks": int(summary.get("region_blocks") or 0),
            "excluded_orders": int(summary.get("excluded_orders") or 0),
```

и в возвращаемый словарь добавить `"region_directory_empty": region_directory_empty,`

- [ ] **Step 4: Расширить схемы**

В `backend/app/schemas.py` в `LogisticsCalendarDayRead` после `planned_blocks` добавить:

```python
    city_orders: int = 0
    region_orders: int = 0
    city_returns: int = 0
    region_returns: int = 0
    city_blocks: int = 0
    region_blocks: int = 0
    excluded_orders: int = 0
```

В `LogisticsCalendarRead` после `default_non_working_weekdays` добавить:

```python
    region_directory_empty: bool = False
```

- [ ] **Step 5: Запустить тест и убедиться, что он проходит**

```bash
PYTHONPATH=. python -m unittest tests.test_logistics_calendar_zone_summary -v
```

Ожидается: OK

- [ ] **Step 6: Написать тест страховки на пустой справочник**

Дописать в тот же файл:

```python
    def test_empty_region_directory_keeps_everything_in_city(self):
        with self.Session() as db:
            make_order(db, client="Тест Клиент 4", address="Область, дом 4", coordinates="41.0180,70.0830", blocks=5)
            db.commit()

            calendar = list_logistics_calendar(db, "2026-08")
            day = next(item for item in calendar["days"] if item["date"] == date(2026, 8, 7))

            self.assertTrue(calendar["region_directory_empty"])
            self.assertEqual(day["city_orders"], 1)
            self.assertEqual(day["region_orders"], 0)
            self.assertEqual(day["city_blocks"], 5)
```

- [ ] **Step 7: Написать тест самовывоза**

Дописать в тот же файл:

```python
    def test_pickup_order_counts_as_excluded_and_not_in_zones(self):
        with self.Session() as db:
            db.add(LogisticsRegionPoint(
                id=uuid.uuid4(),
                client_name="Тест Клиент 5",
                normalized_client=normalize_client_key("Тест Клиент 5"),
                latitude=41.2,
                longitude=69.9,
                is_active=True,
            ))
            make_order(db, client="Тест Клиент 6", address="Самовывоз со склада", coordinates="41.3200,69.2400", blocks=3)
            db.commit()

            day = next(
                item for item in list_logistics_calendar(db, "2026-08")["days"]
                if item["date"] == date(2026, 8, 7)
            )

            self.assertEqual(day["excluded_orders"], 1)
            self.assertEqual(day["orders_count"], 0)
            self.assertEqual(day["city_orders"], 0)
            self.assertEqual(day["region_orders"], 0)
```

- [ ] **Step 8: Прогнать оба новых теста и весь backend-набор**

```bash
PYTHONPATH=. python -m unittest tests.test_logistics_calendar_zone_summary -v
```

```bash
PYTHONPATH=. python -m unittest discover -s tests
```

Ожидается: OK в обоих, существующий
`tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_logistics_calendar_lists_orders_and_saves_non_working_day`
не падает

- [ ] **Step 9: Закоммитить**

```bash
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(calendar): зональная разбивка в месячной сводке логистики" backend/app/logistics_calendar_service.py backend/app/schemas.py tests/test_logistics_calendar_zone_summary.py
```

---

### Task 2: Эндпоинт списка заказов дня

**Files:**
- Create: `backend/app/logistics_calendar_orders_service.py`
- Modify: `backend/app/schemas.py` (после `LogisticsCalendarDayUpdate`)
- Modify: `backend/app/main.py:1066-1081` (рядом с существующими роутами календаря)
- Test: `tests/test_logistics_calendar_day_orders.py`

**Interfaces:**
- Consumes: `classify_order`, `load_region_index`, `is_returned_order`,
  `is_logistics_candidate_order`, `client_point_delivery_slot_map`, `delivery_slot_for_order`
- Produces: `list_logistics_calendar_day_orders(db, service_date) -> dict` и роут
  `GET /api/v1/admin/logistics-calendar/day/{service_date}/orders`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_logistics_calendar_day_orders.py`:

```python
import unittest
import uuid
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.logistics_calendar_orders_service import list_logistics_calendar_day_orders
from backend.app.logistics_zone_service import normalize_client_key
from backend.app.models import Base, LogisticsRegionPoint, Order, OrderItem


class LogisticsCalendarDayOrdersTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_orders_carry_zone_products_and_blocks(self):
        with self.Session() as db:
            db.add(LogisticsRegionPoint(
                id=uuid.uuid4(),
                client_name="Тест Клиент 2",
                normalized_client=normalize_client_key("Тест Клиент 2"),
                latitude=41.2,
                longitude=69.9,
                is_active=True,
            ))
            city = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=date(2026, 8, 7),
                payment_type="Наличные",
                client="Тест Клиент 1",
                address="Ташкент, дом 1",
                representative="Тест Представитель",
                status="not_completed",
                raw_payload={"coordinates": "41.3200,69.2400", "skladbot_request_number": "SB-1"},
            )
            city.items = [OrderItem(
                id=uuid.uuid4(),
                product="Тест Товар А",
                quantity_blocks=10,
                scanned_blocks=4,
                raw_payload={"line_total": 240000},
            )]
            region = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=date(2026, 8, 7),
                payment_type="Наличные",
                client="Тест Клиент 2",
                address="Область, дом 2",
                status="not_completed",
                raw_payload={"coordinates": "41.3200,69.2400"},
            )
            region.items = [OrderItem(id=uuid.uuid4(), product="Тест Товар Б", quantity_blocks=3)]
            db.add_all([city, region])
            db.commit()

            payload = list_logistics_calendar_day_orders(db, date(2026, 8, 7))
            by_client = {row["client"]: row for row in payload["orders"]}

            self.assertEqual(payload["date"], date(2026, 8, 7))
            self.assertFalse(payload["region_directory_empty"])
            self.assertEqual(by_client["Тест Клиент 1"]["zone"], "city")
            self.assertEqual(by_client["Тест Клиент 1"]["products"], "Тест Товар А")
            self.assertEqual(by_client["Тест Клиент 1"]["quantity_blocks"], 10)
            self.assertEqual(by_client["Тест Клиент 1"]["scanned_blocks"], 4)
            self.assertEqual(by_client["Тест Клиент 1"]["remaining_blocks"], 6)
            self.assertEqual(by_client["Тест Клиент 1"]["representative"], "Тест Представитель")
            self.assertEqual(by_client["Тест Клиент 1"]["skladbot_request_number"], "SB-1")
            self.assertEqual(by_client["Тест Клиент 1"]["line_total"], 240000)
            self.assertFalse(by_client["Тест Клиент 1"]["is_returned"])
            self.assertEqual(by_client["Тест Клиент 2"]["zone"], "region")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

```bash
PYTHONPATH=. python -m unittest tests.test_logistics_calendar_day_orders -v
```

Ожидается: ModuleNotFoundError на `logistics_calendar_orders_service`

- [ ] **Step 3: Создать сервис**

Создать `backend/app/logistics_calendar_orders_service.py`:

```python
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .client_points_service import client_point_delivery_slot_map, delivery_slot_for_order
from .logistics_service import is_logistics_candidate_order, is_returned_order
from .logistics_zone_service import ZONE_CITY, classify_order, load_region_index
from .models import Order
from .skladbot_contracts import canonical_skladbot_request_number


def list_logistics_calendar_day_orders(db: Session, service_date: date) -> dict[str, Any]:
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.order_date == service_date)
        .order_by(Order.client.asc(), Order.created_at.asc())
    ).scalars().all()
    region_index = load_region_index(db)
    region_directory_empty = len(region_index) == 0
    listed = [order for order in orders if is_returned_order(order) or is_logistics_candidate_order(order)]
    delivery_slots = client_point_delivery_slot_map(db, listed)
    rows = []
    for order in listed:
        returned_order = is_returned_order(order)
        zone = ZONE_CITY if region_directory_empty else classify_order(
            order.client,
            (order.raw_payload or {}).get("coordinates"),
            region_index,
        )
        delivery_from, delivery_to = delivery_slot_for_order(order, delivery_slots)
        quantity_blocks = sum(int(item.quantity_blocks or 0) for item in order.items)
        scanned_blocks = sum(int(item.scanned_blocks or 0) for item in order.items)
        raw_payload = order.raw_payload or {}
        rows.append({
            "order_id": str(order.id),
            "zone": zone,
            "is_returned": returned_order,
            "client": order.client or "",
            "address": order.address or "",
            "representative": order.representative or "",
            "products": order_products_text(order),
            "source_file": str(raw_payload.get("source_file") or ""),
            "quantity_blocks": quantity_blocks,
            "scanned_blocks": scanned_blocks,
            "remaining_blocks": max(0, quantity_blocks - scanned_blocks),
            "status": order.status or "",
            "delivery_from": str(delivery_from or ""),
            "delivery_to": str(delivery_to or ""),
            "skladbot_request_number": canonical_skladbot_request_number(
                raw_payload.get("skladbot_request_number")
            ) or "",
            "smartup_id": str(raw_payload.get("source_order_id") or ""),
            "line_total": sum(int((item.raw_payload or {}).get("line_total") or 0) for item in order.items),
        })
    return {
        "date": service_date,
        "generated_at": datetime.now(timezone.utc),
        "region_directory_empty": region_directory_empty,
        "orders": rows,
    }


def order_products_text(order: Order) -> str:
    names = []
    for item in sorted(order.items, key=lambda value: (value.product, str(value.id))):
        if item.product and item.product not in names:
            names.append(item.product)
    return "; ".join(names)
```

`canonical_skladbot_request_number` определена в
`backend/app/skladbot_contracts.py:343`, проверено 2026-08-10

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

```bash
PYTHONPATH=. python -m unittest tests.test_logistics_calendar_day_orders -v
```

Ожидается: OK

- [ ] **Step 5: Добавить схемы ответа**

В `backend/app/schemas.py` после `LogisticsCalendarDayUpdate` добавить:

```python
class LogisticsCalendarDayOrderRead(BaseModel):
    order_id: str
    zone: str
    is_returned: bool = False
    client: str = ""
    address: str = ""
    representative: str = ""
    products: str = ""
    source_file: str = ""
    quantity_blocks: int = 0
    scanned_blocks: int = 0
    remaining_blocks: int = 0
    status: str = ""
    delivery_from: str = ""
    delivery_to: str = ""
    skladbot_request_number: str = ""
    smartup_id: str = ""
    line_total: int = 0


class LogisticsCalendarDayOrdersRead(BaseModel):
    date: date
    generated_at: datetime
    region_directory_empty: bool = False
    orders: list[LogisticsCalendarDayOrderRead] = Field(default_factory=list)
```

- [ ] **Step 6: Добавить роут**

В `backend/app/main.py` рядом с существующими роутами календаря добавить:

```python
@api.get(
    "/admin/logistics-calendar/day/{service_date}/orders",
    response_model=LogisticsCalendarDayOrdersRead,
)
def admin_logistics_calendar_day_orders(service_date: date, db=Depends(get_db)):
    return list_logistics_calendar_day_orders_in_db(db, service_date)
```

Импорты дописать к существующему блоку импортов календаря:

```python
from .logistics_calendar_orders_service import (
    list_logistics_calendar_day_orders as list_logistics_calendar_day_orders_in_db,
)
```

и добавить `LogisticsCalendarDayOrdersRead` в импорт схем

- [ ] **Step 7: Написать тест роута**

Дописать в `tests/test_backend_api_persistence.py` в класс `BackendApiPersistenceTests`
рядом с `test_admin_logistics_calendar_lists_orders_and_saves_non_working_day`:

```python
    def test_admin_logistics_calendar_day_orders_returns_zoned_rows(self):
        self.seed_order(client="Тест Клиент 1", address="Ташкент, дом 1", order_date="2026-08-07")
        response = self.client.get("/api/v1/admin/logistics-calendar/day/2026-08-07/orders")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["date"], "2026-08-07")
        self.assertTrue(all(row["zone"] in {"city", "region"} for row in payload["orders"]))
```

Перед написанием посмотреть, как соседние тесты класса готовят заказы, и повторить
их приём, а не изобретать `seed_order`:

```bash
sed -n '860,950p' tests/test_backend_api_persistence.py
```

- [ ] **Step 8: Прогнать тесты**

```bash
PYTHONPATH=. python -m unittest tests.test_logistics_calendar_day_orders tests.test_backend_api_persistence -v
```

Ожидается: OK

- [ ] **Step 9: Закоммитить**

```bash
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(calendar): эндпоинт списка заказов дня с зоной" backend/app/logistics_calendar_orders_service.py backend/app/schemas.py backend/app/main.py tests/test_logistics_calendar_day_orders.py tests/test_backend_api_persistence.py
```

---

### Task 3: Типы и вызовы на фронте

**Files:**
- Modify: `frontend/src/api.ts:336-360` (типы), `:611-620` (вызовы), конец файла (скачивание отчёта)
- Modify: `frontend/src/__tests__/server.ts:93` (MSW-хендлеры)
- Modify: `frontend/src/__tests__/fixtures.ts` (фикстура календаря)
- Test: `frontend/src/__tests__/api.contracts.test.ts`

**Interfaces:**
- Consumes: поля из Task 1 и Task 2
- Produces: `getLogisticsCalendarDayOrders(config, date, signal?)`,
  `downloadLogisticsReport(config, shipmentDate, zone)`, типы
  `LogisticsCalendarDayOrder`, `LogisticsCalendarDayOrders`

- [ ] **Step 1: Написать падающий тест контракта**

В `frontend/src/__tests__/api.contracts.test.ts` добавить:

```ts
  it("getLogisticsCalendarDayOrders отдаёт строки с зоной", async () => {
    const result = await api.getLogisticsCalendarDayOrders(cookieConfig, "2026-08-07");
    expect(result.orders.every((row) => row.zone === "city" || row.zone === "region")).toBe(true);
  });
```

- [ ] **Step 2: Запустить и убедиться, что падает**

```bash
npm --prefix frontend run test -- api.contracts
```

Ожидается: FAIL, `getLogisticsCalendarDayOrders is not a function`

- [ ] **Step 3: Расширить типы**

В `frontend/src/api.ts` в `LogisticsCalendarDay` после `planned_blocks` добавить:

```ts
  city_orders: number;
  region_orders: number;
  city_returns: number;
  region_returns: number;
  city_blocks: number;
  region_blocks: number;
  excluded_orders: number;
```

В `LogisticsCalendar` после `default_non_working_weekdays` добавить:

```ts
  region_directory_empty: boolean;
```

Ниже добавить новые типы:

```ts
export type LogisticsCalendarDayOrder = {
  order_id: string;
  zone: "city" | "region";
  is_returned: boolean;
  client: string;
  address: string;
  representative: string;
  products: string;
  source_file: string;
  quantity_blocks: number;
  scanned_blocks: number;
  remaining_blocks: number;
  status: string;
  delivery_from: string;
  delivery_to: string;
  skladbot_request_number: string;
  smartup_id: string;
  line_total: number;
};

export type LogisticsCalendarDayOrders = {
  date: string;
  generated_at: string;
  region_directory_empty: boolean;
  orders: LogisticsCalendarDayOrder[];
};
```

- [ ] **Step 4: Добавить вызовы**

Рядом с `getLogisticsCalendar`:

```ts
export function getLogisticsCalendarDayOrders(config: ApiConfig, serviceDate: string, signal?: AbortSignal) {
  return apiRequest<LogisticsCalendarDayOrders>(
    config,
    `/api/v1/admin/logistics-calendar/day/${encodeURIComponent(serviceDate)}/orders`,
    { signal },
  );
}
```

Скачивание отчёта по образцу `downloadAdminOrders`:

```ts
export async function downloadLogisticsReport(config: ApiConfig, shipmentDate: string, zone: "city" | "region") {
  const apiUrl = config.apiUrl.replace(/\/$/, "");
  ensureCookieApiIsSameOrigin(apiUrl, Boolean(config.token));
  const query = new URLSearchParams({ shipment_date: shipmentDate, zone });
  const response = await fetch(`${apiUrl}/api/v1/logistics/report?${query.toString()}`, {
    credentials: config.token ? "omit" : "same-origin",
    headers: config.token ? { Authorization: `Bearer ${config.token}` } : {},
  });
  if (!response.ok) throw new ApiRequestError(response.status, response.statusText, "Не удалось выгрузить отчёт логистики");
  return {
    blob: await response.blob(),
    filename: decodeURIComponent(response.headers.get("X-TakSklad-Filename") || "TakSklad_логистика.xlsx"),
  };
}
```

- [ ] **Step 5: Добавить MSW-хендлер и фикстуру**

В `frontend/src/__tests__/server.ts` рядом со строкой 93 добавить:

```ts
  http.get("/api/v1/admin/logistics-calendar/day/:date/orders", () => HttpResponse.json(logisticsCalendarDayOrders)),
```

В `frontend/src/__tests__/fixtures.ts` добавить фикстуру и дописать новые поля в
существующую `logisticsCalendar`, чтобы она соответствовала типу:

```ts
export const logisticsCalendarDayOrders = {
  date: "2026-08-07",
  generated_at: "2026-08-10T07:41:00Z",
  region_directory_empty: false,
  orders: [
    {
      order_id: "order-1",
      zone: "city",
      is_returned: false,
      client: "Тест Клиент 1",
      address: "Ташкент, дом 1",
      representative: "Тест Представитель",
      products: "Тест Товар А",
      source_file: "zakaz.xlsx",
      quantity_blocks: 10,
      scanned_blocks: 4,
      remaining_blocks: 6,
      status: "not_completed",
      delivery_from: "09:00",
      delivery_to: "13:00",
      skladbot_request_number: "SB-1",
      smartup_id: "SM-1",
      line_total: 240000,
    },
    {
      order_id: "order-2",
      zone: "region",
      is_returned: true,
      client: "Тест Клиент 2",
      address: "Область, дом 2",
      representative: "",
      products: "Тест Товар Б",
      source_file: "",
      quantity_blocks: 3,
      scanned_blocks: 0,
      remaining_blocks: 3,
      status: "returned",
      delivery_from: "",
      delivery_to: "",
      skladbot_request_number: "",
      smartup_id: "",
      line_total: 0,
    },
  ],
};
```

- [ ] **Step 6: Прогнать тесты и типы**

```bash
npm --prefix frontend run typecheck && npm --prefix frontend run test -- api.contracts
```

Ожидается: типы сходятся, тест PASS

- [ ] **Step 7: Закоммитить**

```bash
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(web): типы и вызовы детализации дня календаря" frontend/src/api.ts frontend/src/__tests__/server.ts frontend/src/__tests__/fixtures.ts frontend/src/__tests__/api.contracts.test.ts
```

---

### Task 4: Компонент детализации, шапка и карточки зон

**Files:**
- Create: `frontend/src/features/logistics/CalendarDayDetail.tsx`
- Test: `frontend/src/__tests__/CalendarDayDetail.test.tsx`

**Interfaces:**
- Consumes: `LogisticsCalendarDay`, `LogisticsCalendarDayOrders` из `api.ts`
- Produces: компонент

```ts
export function CalendarDayDetail(props: {
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
}): JSX.Element
```

- [ ] **Step 1: Написать падающий тест**

Создать `frontend/src/__tests__/CalendarDayDetail.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CalendarDayDetail } from "../features/logistics/CalendarDayDetail";
import { logisticsCalendarDayOrders } from "./fixtures";

const day = {
  date: "2026-08-07",
  weekday: 4,
  is_weekend: false,
  is_non_working: false,
  is_manual: false,
  reason: "",
  source: "default",
  orders_count: 139,
  active_orders: 27,
  completed_orders: 112,
  returned_orders: 10,
  planned_blocks: 809,
  city_orders: 96,
  region_orders: 43,
  city_returns: 7,
  region_returns: 3,
  city_blocks: 612,
  region_blocks: 197,
  excluded_orders: 5,
  clients: ["Тест Клиент 1"],
};

const noop = () => {};

describe("CalendarDayDetail", () => {
  it("показывает разбивку город и область", () => {
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={logisticsCalendarDayOrders as never}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite
        busyAction=""
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("group", { name: /Город/ })).toHaveTextContent("96");
    expect(screen.getByRole("group", { name: /Область/ })).toHaveTextContent("43");
    expect(screen.getByText(/вне логистики/i)).toHaveTextContent("5");
  });

  it("предупреждает о пустом справочнике областных точек", () => {
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={null}
        loading={false}
        regionDirectoryEmpty
        canAdminWrite={false}
        busyAction=""
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(/справочник областных точек пуст/i);
  });
});
```

- [ ] **Step 2: Запустить и убедиться, что падает**

```bash
npm --prefix frontend run test -- CalendarDayDetail
```

Ожидается: FAIL, модуль не найден

- [ ] **Step 3: Написать компонент, шапку и карточки**

Создать `frontend/src/features/logistics/CalendarDayDetail.tsx`

Форматтеры в проекте локальные для каждого файла, `formatDate` не экспортируется
из `AdminWorkspace.tsx`, а копируется (см. `features/smartup/SmartupAutoImportPanel.tsx:106`)
Повторить этот приём, а не выносить их в общий модуль

```tsx
import { useEffect, useState } from "react";
import { CheckCircle2, ChevronLeft, ChevronRight, Loader2, Lock } from "lucide-react";

import type { LogisticsCalendarDay, LogisticsCalendarDayOrders } from "../../api";

const WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"];

function formatDate(value: string) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString("ru-RU");
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
```

`dayOrders`, `loading` и `onDownload` объявлены сразу, но задействуются в Task 5
и Task 6. Полный прогон линтера идёт в Task 7, к этому моменту все три уже в деле

- [ ] **Step 4: Прогнать тест**

```bash
npm --prefix frontend run test -- CalendarDayDetail
```

Ожидается: PASS

- [ ] **Step 5: Закоммитить**

```bash
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(web): карточки зон в детализации дня" frontend/src/features/logistics/CalendarDayDetail.tsx frontend/src/__tests__/CalendarDayDetail.test.tsx
```

---

### Task 5: Вкладки со списками заказов и фильтром

**Files:**
- Modify: `frontend/src/features/logistics/CalendarDayDetail.tsx`
- Test: `frontend/src/__tests__/CalendarDayDetail.test.tsx`

**Interfaces:**
- Consumes: `dayOrders.orders` из Task 3
- Produces: вкладки `role="tab"` с `aria-selected`, панель `role="tabpanel"`,
  фильтр `Все / Заказы / Возвраты`

- [ ] **Step 1: Написать падающий тест**

Дописать в `CalendarDayDetail.test.tsx`:

```tsx
  it("переключает вкладку на область и фильтрует возвраты", async () => {
    const user = userEvent.setup();
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={logisticsCalendarDayOrders as never}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite={false}
        busyAction=""
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("row", { name: /Тест Клиент 1/ })).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /Тест Клиент 2/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Область/ }));
    expect(screen.getByRole("row", { name: /Тест Клиент 2/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Заказы" }));
    expect(screen.queryByRole("row", { name: /Тест Клиент 2/ })).not.toBeInTheDocument();
  });
```

Импорт `userEvent` дописать в шапку файла:

```tsx
import userEvent from "@testing-library/user-event";
```

- [ ] **Step 2: Запустить и убедиться, что падает**

```bash
npm --prefix frontend run test -- CalendarDayDetail
```

Ожидается: FAIL, вкладок нет

- [ ] **Step 3: Реализовать вкладки и таблицу**

В компонент добавить состояние:

```tsx
const [zone, setZone] = useState<"city" | "region">("city");
const [rowFilter, setRowFilter] = useState<"all" | "orders" | "returns">("all");
const rows = (dayOrders?.orders ?? []).filter((row) => {
  if (row.zone !== zone) return false;
  if (rowFilter === "orders") return !row.is_returned;
  if (rowFilter === "returns") return row.is_returned;
  return true;
});
```

Разметка вкладок, фильтра и таблицы:

```tsx
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
```

- [ ] **Step 4: Прогнать тест**

```bash
npm --prefix frontend run test -- CalendarDayDetail
```

Ожидается: PASS

- [ ] **Step 5: Закоммитить**

```bash
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(web): вкладки город и область со списком заказов дня" frontend/src/features/logistics/CalendarDayDetail.tsx frontend/src/__tests__/CalendarDayDetail.test.tsx
```

---

### Task 6: Выгрузка XLSX активной вкладки

**Files:**
- Modify: `frontend/src/features/logistics/CalendarDayDetail.tsx`
- Modify: `frontend/src/workspace/AdminWorkspace.tsx` (обработчик скачивания)
- Test: `frontend/src/__tests__/CalendarDayDetail.test.tsx`

**Interfaces:**
- Consumes: `downloadLogisticsReport` из Task 3
- Produces: кнопка выгрузки, вызывающая `onDownload(zone)` активной вкладки

- [ ] **Step 1: Написать падающий тест**

```tsx
  it("выгружает XLSX активной вкладки", async () => {
    const user = userEvent.setup();
    const onDownload = vi.fn();
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={logisticsCalendarDayOrders as never}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite={false}
        busyAction=""
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={onDownload}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Выгрузить XLSX город/ }));
    expect(onDownload).toHaveBeenCalledWith("city");

    await user.click(screen.getByRole("tab", { name: /Область/ }));
    await user.click(screen.getByRole("button", { name: /Выгрузить XLSX область/ }));
    expect(onDownload).toHaveBeenCalledWith("region");
  });
```

- [ ] **Step 2: Запустить и убедиться, что падает**

```bash
npm --prefix frontend run test -- CalendarDayDetail
```

Ожидается: FAIL, кнопки нет

- [ ] **Step 3: Добавить кнопку и обработчик**

В компоненте кнопка меняет подпись по активной вкладке и зовёт `onDownload(zone)`

В `AdminWorkspace.tsx` обработчик по образцу `exportOrders` из
`frontend/src/features/imports/ExcelImportControls.tsx:76-92`:

```tsx
async function downloadCalendarReport(serviceDate: string, zone: "city" | "region") {
  setBusyAction(`calendar-report:${serviceDate}:${zone}`);
  try {
    const result = await downloadLogisticsReport(config, serviceDate, zone);
    const href = URL.createObjectURL(result.blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = result.filename;
    anchor.click();
    URL.revokeObjectURL(href);
  } catch (error) {
    showActionError(error, "Не удалось выгрузить отчёт логистики");
  } finally {
    setBusyAction("");
  }
}
```

`showActionError` объявлена в `frontend/src/workspace/AdminWorkspace.tsx:314`,
она же используется соседними действиями панели, проверено 2026-08-10

- [ ] **Step 4: Прогнать тест**

```bash
npm --prefix frontend run test -- CalendarDayDetail
```

Ожидается: PASS

- [ ] **Step 5: Закоммитить**

```bash
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(web): выгрузка XLSX из детализации дня" frontend/src/features/logistics/CalendarDayDetail.tsx frontend/src/workspace/AdminWorkspace.tsx frontend/src/__tests__/CalendarDayDetail.test.tsx
```

---

### Task 7: Встраивание в панель календаря, стили, доступность

**Files:**
- Modify: `frontend/src/workspace/AdminWorkspace.tsx:1652-1805` (`LogisticsCalendarPanel`), `:437-438` (загрузка панели), `:3056-3066` (`panelResourcesForTab`)
- Modify: `frontend/src/styles.css:1771-1927` (блок календаря)
- Test: `frontend/src/__tests__/App.characterization.test.tsx`, `frontend/src/__tests__/App.a11y.test.tsx`

**Interfaces:**
- Consumes: `CalendarDayDetail` из Task 4-6, `getLogisticsCalendarDayOrders` из Task 3
- Produces: панель календаря без боковой колонки, заказы дня грузятся по выбору дня

- [ ] **Step 1: Написать падающий тест**

В `frontend/src/__tests__/App.characterization.test.tsx` добавить проверку, что
после открытия вкладки календаря и клика по дню видны вкладки зон:

```tsx
  it("клик по дню календаря раскрывает детализацию с вкладками зон", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Календарь" }));
    await user.click(await screen.findByRole("button", { name: /заказов/ }));
    expect(await screen.findByRole("tab", { name: /Город/ })).toBeInTheDocument();
  });
```

Перед написанием посмотреть, как соседние тесты файла входят в приложение и какие
имена кнопок используют, и повторить их приём:

```bash
grep -n "renderApp\|findByRole(\"button\"" frontend/src/__tests__/App.characterization.test.tsx | head
```

- [ ] **Step 2: Запустить и убедиться, что падает**

```bash
npm --prefix frontend run test -- App.characterization
```

Ожидается: FAIL, вкладок нет

- [ ] **Step 3: Перестроить панель календаря**

В `LogisticsCalendarPanel` убрать `<aside className="calendar-detail">` целиком,
`calendar-layout` оставить одноколоночным, под сеткой отрендерить `CalendarDayDetail`
Поле причины и кнопки статуса дня переезжают внутрь `CalendarDayDetail`

Добавить в `AdminWorkspace` состояние и загрузку заказов дня:

```tsx
const [calendarDayOrders, setCalendarDayOrders] = useState<LogisticsCalendarDayOrders | null>(null);
const [calendarDayLoading, setCalendarDayLoading] = useState(false);
```

Выбранная дата уже хранится в `selectedCalendarDate`
(`frontend/src/workspace/AdminWorkspace.tsx:183`), новое состояние для неё не нужно

Загрузка при смене выбранного дня через существующий `loadCachedPanel` с ключом
ресурса `calendar-day:${selectedCalendarDate}`, чтобы работали кэш и координатор
запросов:

```tsx
    } else if (activeTab === "calendar" && has("client_points:read")) {
      await loadCachedPanel(`calendar:${calendarMonth}`, (signal) => getLogisticsCalendar(activeConfig, calendarMonth, signal), setLogisticsCalendar, force);
      await loadCachedPanel(
        `calendar-day:${selectedCalendarDate}`,
        (signal) => getLogisticsCalendarDayOrders(activeConfig, selectedCalendarDate, signal),
        setCalendarDayOrders,
        force,
      );
    }
```

`panelResourcesForTab` принимает второй аргумент, поэтому её сигнатуру расширить
третьим и передать дату из вызова на строке 583:

```tsx
function panelResourcesForTab(value: Tab, calendarMonth: string, selectedCalendarDate: string) {
  if (value === "calendar") return [`calendar:${calendarMonth}`, `calendar-day:${selectedCalendarDate}`];
```

Эффект на строке 589 получает `selectedCalendarDate` в список зависимостей

- [ ] **Step 4: Дописать стили**

В `frontend/src/styles.css:1781-1786` у `.calendar-layout` убрать двухколоночную
сетку, оставив одну колонку:

```css
.calendar-layout {
  display: grid;
  gap: 14px;
  padding: 14px;
}
```

Дубль в медиазапросе на строке 2397 (`.calendar-layout { grid-template-columns: 1fr; }`)
после этого лишний, удалить его
Правила `.calendar-detail` и `.calendar-client-list` (строки 1892-1922) удалить
вместе с разметкой боковой колонки

После блока `.calendar-day small` добавить:

```css
.day-detail {
  border-top: 1px solid #eceee8;
  background: #fbfcfa;
}

.day-detail-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid #eceee8;
  padding: 14px;
  background: #ffffff;
}

.day-detail-head h3 {
  margin: 0 0 2px;
  font-size: 17px;
}

.day-detail-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.day-nav {
  display: flex;
  gap: 6px;
}

.day-nav button {
  min-width: 40px;
  min-height: 40px;
  border: 1px solid #d7dad1;
  border-radius: 8px;
  background: #ffffff;
  color: #31524e;
  cursor: pointer;
}

.day-detail-controls {
  display: grid;
  gap: 10px;
  padding: 0 14px 14px;
}

.alert-bar {
  margin: 14px 14px 0;
  border: 1px solid #e2c9a6;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fdf6e8;
  color: #7a5b12;
  font-size: 13px;
  line-height: 1.5;
}

.zone-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
  padding: 14px;
}

.zone-card {
  border: 1px solid #e0e2dc;
  border-left: 4px solid #16635b;
  border-radius: 8px;
  padding: 14px;
  background: #ffffff;
}

.zone-card.region {
  border-left-color: #b06913;
}

.zone-card.total {
  border-left-color: #5b6360;
  background: #fbfcfa;
}

.zone-card h4 {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin: 0 0 10px;
  font-size: 14px;
}

.zone-card h4 em {
  color: #6d7168;
  font-size: 12px;
  font-style: normal;
  font-weight: 400;
}

.zone-figures {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.zone-figures div {
  border-radius: 8px;
  padding: 8px 10px;
  background: #fbfcfa;
}

.zone-card.total .zone-figures div {
  background: #ffffff;
}

.zone-figures b {
  display: block;
  font-size: 22px;
  line-height: 1.1;
}

.zone-figures b.ret {
  color: #8a5300;
}

.zone-figures small {
  color: #6d7168;
  font-size: 12px;
}

.zone-foot {
  margin: 10px 0 0;
  color: #6d7168;
  font-size: 12px;
  line-height: 1.5;
}

.day-detail .list-panel {
  margin: 0 14px 14px;
  border: 1px solid #e0e2dc;
  border-radius: 8px;
  background: #ffffff;
  overflow: hidden;
}

.list-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  border-bottom: 1px solid #eceee8;
  padding: 0 8px;
  background: #fbfcfa;
}

.list-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 0;
  border-bottom: 2px solid transparent;
  padding: 13px 14px 11px;
  background: transparent;
  color: #6d7168;
  font-size: 15px;
  font-weight: 700;
  cursor: pointer;
}

.list-tabs button[aria-selected="true"] {
  border-bottom-color: #16635b;
  background: #ffffff;
  color: #202326;
}

.list-tabs .tab-count {
  color: #6d7168;
  font-size: 13px;
  font-weight: 400;
}

.list-tabs button[aria-selected="true"] .tab-count {
  color: #16635b;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  background: #16635b;
}

.dot.region {
  background: #b06913;
}

.list-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border-bottom: 1px solid #eceee8;
  padding: 12px 14px;
}

.list-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.chips {
  display: flex;
  gap: 6px;
}

.chips button {
  border: 1px solid #d7dad1;
  border-radius: 999px;
  padding: 5px 11px;
  background: #ffffff;
  color: #4f5b4d;
  font-size: 12px;
  cursor: pointer;
}

.chips button[aria-pressed="true"] {
  border-color: #16635b;
  background: #f0f4ee;
  color: #16635b;
  font-weight: 700;
}

.ghost-button.sm {
  min-height: 34px;
  padding: 0 11px;
  font-size: 12px;
}

.day-detail .table-scroll {
  max-height: 340px;
  overflow: auto;
}

.data-table tr.ret-row {
  background: #fffaf2;
}

.status-badge.ret {
  background: #fdf0dc;
  color: #8a5300;
}
```

Классы `.data-table` (1148), `.cell-title` и `.cell-sub` (1397-1409),
`.numeric-cell` (1424), `.empty-state` (1017) уже есть в файле, повторно их не
объявлять, в разметке использовать именно `numeric-cell`, а не `numeric`

Имена `.day-detail`, `.zone-*`, `.list-tabs`, `.list-head`, `.chips`, `.dot`,
`.table-scroll`, `.alert-bar` в `styles.css` свободны, проверено 2026-08-10:

```bash
grep -n "^\.list-head\|^\.chips\|^\.dot\b\|^\.table-scroll\|^\.alert-bar" frontend/src/styles.css
```

- [ ] **Step 5: Прогнать тесты, линт и типы**

```bash
npm --prefix frontend run lint && npm --prefix frontend run typecheck && npm --prefix frontend run test
```

Ожидается: всё зелёное, включая `App.a11y`

- [ ] **Step 6: Прогнать бэкенд целиком**

```bash
PYTHONPATH=. python -m unittest discover -s tests
```

Ожидается: OK

- [ ] **Step 7: Закоммитить**

```bash
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(web): детализация дня в панели календаря логистики" frontend/src/workspace/AdminWorkspace.tsx frontend/src/styles.css frontend/src/__tests__/App.characterization.test.tsx
```

---

### Task 8: Сверка разбивки с XLSX-отчётом на реальных данных

**Files:**
- Test: ручная проверка, артефакты в репозиторий не коммитятся

**Interfaces:**
- Consumes: всё, что сделано в Task 1-7
- Produces: доказательство, что разбивка календаря совпадает с XLSX-отчётом

- [ ] **Step 1: Поднять локальную среду по docs/local-development-setup.md**

```bash
sed -n '1,60p' docs/local-development-setup.md
```

- [ ] **Step 2: Сверить числа на одной дате**

На той же дате сравнить три числа: `city_orders` и `region_orders` из
`GET /api/v1/admin/logistics-calendar`, число строк в `orders` с каждой зоной из
`GET /api/v1/admin/logistics-calendar/day/<дата>/orders`, и число заказов в
городском и областном XLSX из `build_logistics_reports`

Расхождение допустимо только на заказах без позиций, всё остальное это дефект

- [ ] **Step 3: Записать результат сверки в описание PR**

Числами: сколько заказов в городе и области по календарю, сколько по отчёту

- [ ] **Step 4: Открыть PR**

```bash
gh pr create --fill
```

Дождаться зелёного CI, обязателен контекст `Release gate`, затем

```bash
gh pr merge --squash --delete-branch
```

---

## Порядок работы с ветками

Перед Task 1:

```bash
git fetch origin main
```

```bash
git worktree add --detach /tmp/calendar-day-detail origin/main
```

```bash
git -C /tmp/calendar-day-detail checkout -b feature/calendar-day-zone-detail
```

Все коммиты и push делать из `/tmp/calendar-day-detail` с префиксом
`ALLOW_NON_MAIN_BRANCH=1`, файлы добавлять поимённо, `git add -A` запрещён

После мержа:

```bash
git worktree remove /tmp/calendar-day-detail
```
