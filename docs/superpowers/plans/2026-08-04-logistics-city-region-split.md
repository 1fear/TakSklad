# Разделение отчёта логистики на городской и областной, план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** отчёт логистики уходит в Telegram двумя файлами, городским и областным,
разделение идёт по справочнику областных точек, спорные заказы не отправляются
и попадают в алерт админу

**Architecture:** новая таблица `logistics_region_points` хранит справочник,
новый модуль `logistics_zone_service.py` отвечает на единственный вопрос
«город это или область», `logistics_service.py` делает один проход по заказам
и собирает два XLSX вместо одного
Маршрут, чат и расписание не меняются, меняется только текст артефакта и его хеш

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, openpyxl, unittest, FastAPI

**Спека:** `docs/superpowers/specs/2026-08-04-logistics-city-region-split-design.md`

## Global Constraints

- Тесты запускаются через unittest, не pytest:
  `PYTHONPATH=. python -m unittest discover -s tests`
- Все коммиты только в `main`, локальные хуки блокируют коммит из другой ветки
- Репозиторий `1fear/TakSklad` публичный: имена контрагентов, координаты точек
  и содержимое адресной программы не попадают в код, тесты, фикстуры, логи,
  сообщения об ошибках и документы
  В тестах используются вымышленные имена вида `Тест Клиент 1`
- Client-facing тексты согласованы и менять их нельзя:
  файлы `TakSklad_логистика_город_ДД.ММ.ГГГГ.xlsx` и
  `TakSklad_логистика_область_ДД.ММ.ГГГГ.xlsx`,
  подписи `Отчет логистики город ДД.ММ.ГГГГ` и `Отчет логистики область ДД.ММ.ГГГГ`
- Состав `message_kinds` в `telegram_routing_manifest.json` и enum
  `TelegramMessageKind` не меняются, меняется только `text_policy_sha256`
- Городской документ отправляется первым, областной вторым
- Зона без заказов не даёт файла и не отправляется

## Структура файлов

| Файл | Ответственность |
|------|-----------------|
| `backend/app/models.py` | модель `LogisticsRegionPoint`, только схема |
| `backend/migrations/versions/20260804_0021_logistics_region_points.py` | создание таблицы |
| `backend/app/logistics_zone_service.py` | новый, только классификация: геометрия, поиск в справочнике, решение город/область |
| `backend/app/logistics_service.py` | сборка XLSX, теперь по зонам |
| `backend/app/telegram_output_contract.py` | имена файлов и подписи с зоной |
| `backend/app/telegram_routing_manifest.json` | новый хеш, новый алиас алерта |
| `backend/app/main.py` | параметр `zone` у эндпоинта отчёта |
| `backend/app/telegram_report_processor.py` | ручная отправка двух документов |
| `backend/app/smartup_auto_import.py` | автоматическая отправка двух документов и алерт |
| `tools/seed_logistics_region_points.py` | новый, загрузка справочника из xlsx |
| `tools/logistics_zone_dry_run.py` | новый, подсчёт разделения без отправки |
| `tests/test_logistics_zone_service.py` | новый, классификация |
| `tests/test_logistics_report_split.py` | новый, сборка двух отчётов |

---

### Task 1: Таблица и модель справочника областных точек

**Files:**
- Modify: `backend/app/models.py:1-6` (импорты), новый класс после `ClientPoint`
- Create: `backend/migrations/versions/20260804_0021_logistics_region_points.py`
- Test: `tests/test_logistics_zone_service.py`

**Interfaces:**
- Consumes: ничего
- Produces: `LogisticsRegionPoint` с полями `id`, `client_name`, `normalized_client`,
  `latitude`, `longitude`, `agent`, `is_active`, `raw_payload`, `created_at`, `updated_at`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_logistics_zone_service.py`:

```python
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, LogisticsRegionPoint


class LogisticsRegionPointModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_region_point_stores_name_coordinates_and_agent(self):
        db = self.SessionLocal()
        db.add(LogisticsRegionPoint(
            client_name="Тест Клиент 1",
            normalized_client="тестклиент1",
            latitude=41.018778,
            longitude=70.083423,
            agent="Агент 3",
        ))
        db.commit()
        point = db.execute(select(LogisticsRegionPoint)).scalar_one()
        self.assertEqual(point.client_name, "Тест Клиент 1")
        self.assertEqual(point.normalized_client, "тестклиент1")
        self.assertAlmostEqual(float(point.latitude), 41.018778, places=6)
        self.assertAlmostEqual(float(point.longitude), 70.083423, places=6)
        self.assertEqual(point.agent, "Агент 3")
        self.assertTrue(point.is_active)
        db.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_service -v`
Expected: FAIL, `ImportError: cannot import name 'LogisticsRegionPoint'`

- [ ] **Step 3: Добавить `Numeric` в импорты models.py**

В `backend/app/models.py:3` заменить строку импорта на:

```python
from sqlalchemy import JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Uuid, UniqueConstraint, event, func, text
```

- [ ] **Step 4: Добавить модель после класса `ClientPoint`**

В `backend/app/models.py`, сразу после класса `ClientPoint` и перед `LogisticsCalendarDay`:

```python
class LogisticsRegionPoint(Base):
    __tablename__ = "logistics_region_points"
    __table_args__ = (
        UniqueConstraint(
            "normalized_client", "latitude", "longitude",
            name="uq_logistics_region_points_client_coordinates",
        ),
        Index("idx_logistics_region_points_client", "normalized_client"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_logistics_region_points_latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_logistics_region_points_longitude"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_client: Mapped[str] = mapped_column(String(255), nullable=False)
    latitude: Mapped[object] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[object] = mapped_column(Numeric(9, 6), nullable=False)
    agent: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

- [ ] **Step 5: Запустить тест, убедиться что проходит**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_service -v`
Expected: PASS

- [ ] **Step 6: Написать миграцию**

Создать `backend/migrations/versions/20260804_0021_logistics_region_points.py`:

```python
"""Add logistics region points directory for city/region report split."""

import sqlalchemy as sa
from alembic import op


revision = "20260804_0021"
down_revision = "20260719_0020"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        "logistics_region_points",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=False),
        sa.Column("normalized_client", sa.String(length=255), nullable=False),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("longitude", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_logistics_region_points_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_logistics_region_points_longitude"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_client", "latitude", "longitude",
            name="uq_logistics_region_points_client_coordinates",
        ),
    )
    op.create_index(
        "idx_logistics_region_points_client",
        "logistics_region_points",
        ["normalized_client"],
    )


def downgrade():
    op.drop_index("idx_logistics_region_points_client", table_name="logistics_region_points")
    op.drop_table("logistics_region_points")
```

- [ ] **Step 7: Проверить, что Alembic видит одну голову**

Run: `PYTHONPATH=. python -m alembic -c backend/alembic.ini heads`
Expected: одна строка, `20260804_0021 (head)`

- [ ] **Step 8: Прогнать матрицу миграций**

Run: `./tools/run_postgres_tests.sh migrations`
Expected: PASS
Если Docker или Postgres недоступны локально, отметить это явно в отчёте
и не выдавать шаг за пройденный

- [ ] **Step 9: Коммит**

```bash
git add backend/app/models.py backend/migrations/versions/20260804_0021_logistics_region_points.py tests/test_logistics_zone_service.py
git commit -m "feat(logistics): справочник областных точек, таблица и модель

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Геометрия зоны, разбор координат и граница города

**Files:**
- Create: `backend/app/logistics_zone_service.py`
- Test: `tests/test_logistics_zone_service.py` (дополняется)

**Interfaces:**
- Consumes: ничего
- Produces:
  - `parse_coordinates(value) -> tuple[float, float] | None`
  - `haversine_meters(latitude_a, longitude_a, latitude_b, longitude_b) -> float`
  - `point_in_city(latitude, longitude) -> bool`
  - `TASHKENT_CITY_POLYGON`, `CITY_BUFFER_METERS`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_logistics_zone_service.py`, перед `if __name__`:

```python
from backend.app.logistics_zone_service import (
    haversine_meters,
    parse_coordinates,
    point_in_city,
)


class CoordinateParsingTests(unittest.TestCase):
    def test_parses_comma_separated_pair(self):
        self.assertEqual(parse_coordinates("41.311081,69.240562"), (41.311081, 69.240562))

    def test_parses_decimal_comma_and_spaces(self):
        self.assertEqual(parse_coordinates(" 41,311081 ; 69,240562 "), (41.311081, 69.240562))

    def test_rejects_single_number(self):
        self.assertIsNone(parse_coordinates("41.311081"))

    def test_rejects_empty_and_none(self):
        self.assertIsNone(parse_coordinates(""))
        self.assertIsNone(parse_coordinates(None))

    def test_rejects_out_of_range(self):
        self.assertIsNone(parse_coordinates("91.0,69.2"))
        self.assertIsNone(parse_coordinates("41.3,181.0"))


class HaversineTests(unittest.TestCase):
    def test_zero_distance_for_same_point(self):
        self.assertAlmostEqual(haversine_meters(41.3, 69.2, 41.3, 69.2), 0.0, places=3)

    def test_one_degree_of_latitude_is_about_111_km(self):
        distance = haversine_meters(41.0, 69.2, 42.0, 69.2)
        self.assertAlmostEqual(distance, 111195.0, delta=200.0)

    def test_short_distance_is_symmetric(self):
        forward = haversine_meters(41.311081, 69.240562, 41.312081, 69.240562)
        backward = haversine_meters(41.312081, 69.240562, 41.311081, 69.240562)
        self.assertAlmostEqual(forward, backward, places=6)
        self.assertAlmostEqual(forward, 111.2, delta=1.0)


class CityBoundaryTests(unittest.TestCase):
    def test_known_city_points_are_inside(self):
        for name, latitude, longitude in (
            ("центр Ташкента", 41.3200, 69.2400),
            ("Сергели", 41.2200, 69.2200),
            ("Юнусабад верх", 41.3750, 69.2800),
        ):
            with self.subTest(point=name):
                self.assertTrue(point_in_city(latitude, longitude))

    def test_known_region_points_are_outside(self):
        for name, latitude, longitude in (
            ("Чирчик", 41.4700, 69.5800),
            ("Ангрен", 41.0180, 70.0830),
            ("Олмалик", 40.8470, 69.5980),
            ("Зангиата", 41.1900, 69.1300),
        ):
            with self.subTest(point=name):
                self.assertFalse(point_in_city(latitude, longitude))

    def test_buffer_admits_point_just_outside_polygon(self):
        # 500 м западнее западной вершины полигона, внутри буфера 1 км
        self.assertTrue(point_in_city(41.2800, 69.1340))

    def test_buffer_rejects_point_far_outside_polygon(self):
        # около 8 км западнее западной вершины, за пределами буфера
        self.assertFalse(point_in_city(41.2800, 69.0450))
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_service -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'backend.app.logistics_zone_service'`

- [ ] **Step 3: Создать модуль с геометрией**

Создать `backend/app/logistics_zone_service.py`:

```python
"""Decides whether a logistics order belongs to Tashkent city or to the region."""

from __future__ import annotations

import math
import re


EARTH_RADIUS_METERS = 6371008.8

# Приближённая административная граница Ташкента, пары (широта, долгота)
TASHKENT_CITY_POLYGON = (
    (41.3900, 69.2400),
    (41.3800, 69.3100),
    (41.3700, 69.3600),
    (41.3400, 69.3900),
    (41.3000, 69.4000),
    (41.2600, 69.3700),
    (41.2200, 69.3200),
    (41.2000, 69.2600),
    (41.2100, 69.2100),
    (41.2400, 69.1700),
    (41.2800, 69.1400),
    (41.3200, 69.1500),
    (41.3600, 69.1900),
)

# Запас наружу от границы: ошибка в сторону города дешевле выпавшего заказа
CITY_BUFFER_METERS = 1000.0

_COORDINATE_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_coordinates(value) -> tuple[float, float] | None:
    """Return (latitude, longitude) parsed from a free-form order payload value."""
    text = str(value or "").strip()
    if not text:
        return None
    numbers = _COORDINATE_RE.findall(text)
    if len(numbers) < 2:
        return None
    try:
        latitude = float(numbers[0].replace(",", "."))
        longitude = float(numbers[1].replace(",", "."))
    except ValueError:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def haversine_meters(latitude_a, longitude_a, latitude_b, longitude_b) -> float:
    lat_a = math.radians(float(latitude_a))
    lat_b = math.radians(float(latitude_b))
    delta_latitude = lat_b - lat_a
    delta_longitude = math.radians(float(longitude_b) - float(longitude_a))
    factor = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_longitude / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(factor))


def point_in_city(latitude, longitude) -> bool:
    """True when the point is inside the city polygon or within the buffer around it."""
    latitude = float(latitude)
    longitude = float(longitude)
    if _point_in_polygon(latitude, longitude, TASHKENT_CITY_POLYGON):
        return True
    return _distance_to_polygon_meters(latitude, longitude, TASHKENT_CITY_POLYGON) <= CITY_BUFFER_METERS


def _point_in_polygon(latitude, longitude, polygon) -> bool:
    inside = False
    count = len(polygon)
    for index in range(count):
        latitude_a, longitude_a = polygon[index]
        latitude_b, longitude_b = polygon[(index + 1) % count]
        if (longitude_a > longitude) != (longitude_b > longitude):
            ratio = (longitude - longitude_a) / (longitude_b - longitude_a)
            if latitude_a + ratio * (latitude_b - latitude_a) > latitude:
                inside = not inside
    return inside


def _distance_to_polygon_meters(latitude, longitude, polygon) -> float:
    count = len(polygon)
    return min(
        _distance_to_segment_meters(latitude, longitude, polygon[index], polygon[(index + 1) % count])
        for index in range(count)
    )


def _distance_to_segment_meters(latitude, longitude, start, end) -> float:
    """Distance to a segment using a local equirectangular projection in metres."""
    metres_per_degree_latitude = 111320.0
    metres_per_degree_longitude = metres_per_degree_latitude * math.cos(math.radians(latitude))
    point_x = longitude * metres_per_degree_longitude
    point_y = latitude * metres_per_degree_latitude
    start_x = start[1] * metres_per_degree_longitude
    start_y = start[0] * metres_per_degree_latitude
    end_x = end[1] * metres_per_degree_longitude
    end_y = end[0] * metres_per_degree_latitude
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    if delta_x == 0 and delta_y == 0:
        return math.hypot(point_x - start_x, point_y - start_y)
    ratio = ((point_x - start_x) * delta_x + (point_y - start_y) * delta_y) / (delta_x ** 2 + delta_y ** 2)
    ratio = max(0.0, min(1.0, ratio))
    nearest_x = start_x + ratio * delta_x
    nearest_y = start_y + ratio * delta_y
    return math.hypot(point_x - nearest_x, point_y - nearest_y)
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_service -v`
Expected: PASS, все тесты классов `CoordinateParsingTests`, `HaversineTests`, `CityBoundaryTests`

- [ ] **Step 5: Коммит**

```bash
git add backend/app/logistics_zone_service.py tests/test_logistics_zone_service.py
git commit -m "feat(logistics): геометрия зоны, разбор координат и граница города

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Поиск клиента в справочнике, три уровня

**Files:**
- Modify: `backend/app/logistics_zone_service.py` (дополняется)
- Test: `tests/test_logistics_zone_service.py` (дополняется)

**Interfaces:**
- Consumes: `haversine_meters` из Task 2, `LogisticsRegionPoint` из Task 1
- Produces:
  - `normalize_client_key(value) -> str`
  - `name_tokens(value) -> frozenset[str]`
  - `RegionPoint` (dataclass: `client_name`, `normalized_client`, `latitude`, `longitude`, `tokens`)
  - `RegionIndex.find(client_name, latitude, longitude) -> RegionPoint | None`
  - `RegionIndex.match_level(client_name, latitude, longitude) -> str | None`
    возвращает `"name"`, `"coordinates"`, `"fuzzy"` или `None`
  - `load_region_index(db) -> RegionIndex`
  - `REGION_MATCH_METERS`, `FUZZY_NAME_THRESHOLD`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_logistics_zone_service.py`:

```python
from backend.app.logistics_zone_service import (
    RegionIndex,
    RegionPoint,
    load_region_index,
    name_tokens,
    normalize_client_key,
)


def region_index_fixture():
    return RegionIndex([
        RegionPoint.build("Тест Клиент Один", 41.018778, 70.083423),
        RegionPoint.build('"ТЕСТ БЕТА САВДО" MCHJ', 40.847219, 69.620199),
    ])


class NameNormalizationTests(unittest.TestCase):
    def test_key_ignores_case_quotes_and_spaces(self):
        self.assertEqual(normalize_client_key('  "Тест Клиент Один"  '), "тестклиентодин")
        self.assertEqual(normalize_client_key("ТЕСТ КЛИЕНТ ОДИН"), "тестклиентодин")

    def test_key_maps_yo_to_ye(self):
        self.assertEqual(normalize_client_key("Тёст"), "тест")

    def test_tokens_drop_legal_forms_and_single_letters(self):
        self.assertEqual(name_tokens('"ТЕСТ БЕТА САВДО" MCHJ'), frozenset({"тест", "бета", "савдо"}))
        self.assertEqual(name_tokens('ООО "Тест Гамма"'), frozenset({"тест", "гамма"}))


class RegionIndexTests(unittest.TestCase):
    def setUp(self):
        self.index = region_index_fixture()

    def test_exact_name_matches_regardless_of_coordinates(self):
        found = self.index.find("тест клиент один", None, None)
        self.assertIsNotNone(found)
        self.assertEqual(self.index.match_level("тест клиент один", None, None), "name")

    def test_coordinates_match_when_name_differs(self):
        # то же место, имя написано иначе
        self.assertEqual(
            self.index.match_level("Совсем Другое Написание", 41.018800, 70.083400),
            "coordinates",
        )

    def test_coordinates_do_not_match_beyond_threshold(self):
        # около 900 м от точки справочника
        self.assertIsNone(self.index.match_level("Совсем Другое Написание", 41.026800, 70.083423))

    def test_fuzzy_name_matches_on_shared_tokens(self):
        self.assertEqual(
            self.index.match_level('"ТЕСТ БЕТА САВДО" YTT (филиал)', None, None),
            "fuzzy",
        )

    def test_unrelated_name_without_coordinates_is_not_found(self):
        self.assertIsNone(self.index.find("Незнакомая Точка Дельта", None, None))

    def test_unrelated_name_with_far_coordinates_is_not_found(self):
        self.assertIsNone(self.index.find("Незнакомая Точка Дельта", 41.3200, 69.2400))


class LoadRegionIndexTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_loads_only_active_points(self):
        db = self.SessionLocal()
        db.add(LogisticsRegionPoint(
            client_name="Тест Клиент Один",
            normalized_client="тестклиентодин",
            latitude=41.018778,
            longitude=70.083423,
            is_active=True,
        ))
        db.add(LogisticsRegionPoint(
            client_name="Тест Клиент Два",
            normalized_client="тестклиентдва",
            latitude=40.847219,
            longitude=69.620199,
            is_active=False,
        ))
        db.commit()
        index = load_region_index(db)
        self.assertIsNotNone(index.find("Тест Клиент Один", None, None))
        self.assertIsNone(index.find("Тест Клиент Два", None, None))
        db.close()
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_service -v`
Expected: FAIL, `ImportError: cannot import name 'RegionIndex'`

- [ ] **Step 3: Дописать модуль**

В `backend/app/logistics_zone_service.py` добавить импорты в начало файла:

```python
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LogisticsRegionPoint
```

и константы рядом с `CITY_BUFFER_METERS`:

```python
# Совпадение по координатам: тот же магазин, имя написано иначе
REGION_MATCH_METERS = 150.0

# Доля общих значимых токенов от более короткого имени
FUZZY_NAME_THRESHOLD = 0.7

LEGAL_FORM_TOKENS = frozenset({
    "mchj", "ytt", "xk", "ooo", "chp", "sp",
    "сп", "чп", "ооо", "мчж", "ытт", "хк",
})

_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-zа-я]+")
_KEY_STRIP_RE = re.compile(r"[^0-9a-zа-я]+")
```

и в конец файла:

```python
def normalize_client_key(value) -> str:
    """Same normalisation as client_points_service.point_key, kept dependency-free."""
    text = str(value or "").strip().casefold().replace("ё", "е")
    return _KEY_STRIP_RE.sub("", text)


def name_tokens(value) -> frozenset[str]:
    text = str(value or "").strip().casefold().replace("ё", "е")
    parts = _TOKEN_SPLIT_RE.split(text)
    return frozenset(
        part for part in parts
        if len(part) > 1 and part not in LEGAL_FORM_TOKENS
    )


def fuzzy_name_ratio(tokens_a: frozenset[str], tokens_b: frozenset[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))


@dataclass(frozen=True)
class RegionPoint:
    client_name: str
    normalized_client: str
    latitude: float
    longitude: float
    tokens: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def build(cls, client_name, latitude, longitude) -> "RegionPoint":
        return cls(
            client_name=str(client_name or ""),
            normalized_client=normalize_client_key(client_name),
            latitude=float(latitude),
            longitude=float(longitude),
            tokens=name_tokens(client_name),
        )


class RegionIndex:
    """Directory of region delivery points with three lookup levels."""

    def __init__(self, points):
        self._points = tuple(points)
        self._by_key = {}
        for point in self._points:
            self._by_key.setdefault(point.normalized_client, point)

    def __len__(self):
        return len(self._points)

    def find(self, client_name, latitude=None, longitude=None) -> RegionPoint | None:
        point, _level = self._lookup(client_name, latitude, longitude)
        return point

    def match_level(self, client_name, latitude=None, longitude=None) -> str | None:
        _point, level = self._lookup(client_name, latitude, longitude)
        return level

    def _lookup(self, client_name, latitude, longitude):
        exact = self._by_key.get(normalize_client_key(client_name))
        if exact is not None:
            return exact, "name"
        if latitude is not None and longitude is not None:
            nearest = self._nearest(latitude, longitude)
            if nearest is not None:
                return nearest, "coordinates"
        fuzzy = self._fuzzy(client_name)
        if fuzzy is not None:
            return fuzzy, "fuzzy"
        return None, None

    def _nearest(self, latitude, longitude) -> RegionPoint | None:
        best_point = None
        best_distance = REGION_MATCH_METERS
        for point in self._points:
            distance = haversine_meters(latitude, longitude, point.latitude, point.longitude)
            if distance <= best_distance:
                best_point = point
                best_distance = distance
        return best_point

    def _fuzzy(self, client_name) -> RegionPoint | None:
        tokens = name_tokens(client_name)
        if not tokens:
            return None
        best_point = None
        best_ratio = FUZZY_NAME_THRESHOLD
        for point in self._points:
            ratio = fuzzy_name_ratio(tokens, point.tokens)
            if ratio >= best_ratio:
                best_point = point
                best_ratio = ratio
        return best_point


def load_region_index(db: Session) -> RegionIndex:
    points = db.execute(
        select(LogisticsRegionPoint).where(LogisticsRegionPoint.is_active.is_(True))
    ).scalars().all()
    return RegionIndex([
        RegionPoint.build(point.client_name, point.latitude, point.longitude)
        for point in points
    ])
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_service -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add backend/app/logistics_zone_service.py tests/test_logistics_zone_service.py
git commit -m "feat(logistics): поиск точки в справочнике области, три уровня

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Правило классификации заказа

**Files:**
- Modify: `backend/app/logistics_zone_service.py` (дополняется)
- Test: `tests/test_logistics_zone_service.py` (дополняется)

**Interfaces:**
- Consumes: `parse_coordinates`, `point_in_city`, `RegionIndex` из Task 2 и Task 3
- Produces:
  - `ZONE_CITY = "city"`, `ZONE_REGION = "region"`, `ZONE_UNASSIGNED = "unassigned"`
  - `classify_order(client_name, coordinates_value, index) -> str`

- [ ] **Step 1: Написать падающие тесты, по тесту на строку правила**

Добавить в `tests/test_logistics_zone_service.py`:

```python
from backend.app.logistics_zone_service import (
    ZONE_CITY,
    ZONE_REGION,
    ZONE_UNASSIGNED,
    classify_order,
)


class ClassifyOrderTests(unittest.TestCase):
    def setUp(self):
        self.index = region_index_fixture()

    def test_rule_1_known_client_goes_to_region(self):
        self.assertEqual(
            classify_order("Тест Клиент Один", "41.018778,70.083423", self.index),
            ZONE_REGION,
        )

    def test_rule_2_unknown_client_inside_city_goes_to_city(self):
        self.assertEqual(
            classify_order("Незнакомая Точка Дельта", "41.3200,69.2400", self.index),
            ZONE_CITY,
        )

    def test_rule_3_unknown_client_outside_city_is_unassigned(self):
        self.assertEqual(
            classify_order("Незнакомая Точка Дельта", "41.4700,69.5800", self.index),
            ZONE_UNASSIGNED,
        )

    def test_rule_4_unknown_client_without_coordinates_goes_to_city(self):
        self.assertEqual(
            classify_order("Незнакомая Точка Дельта", "", self.index),
            ZONE_CITY,
        )

    def test_rule_5_known_client_without_coordinates_goes_to_region(self):
        self.assertEqual(
            classify_order("Тест Клиент Один", "", self.index),
            ZONE_REGION,
        )

    def test_known_client_wins_over_city_coordinates(self):
        # клиент в справочнике области, но точка физически в черте города
        index = RegionIndex([RegionPoint.build("Тест Городская Область", 41.3200, 69.2400)])
        self.assertEqual(
            classify_order("Тест Городская Область", "41.3200,69.2400", index),
            ZONE_REGION,
        )

    def test_broken_coordinates_are_treated_as_missing(self):
        self.assertEqual(
            classify_order("Незнакомая Точка Дельта", "не координаты", self.index),
            ZONE_CITY,
        )
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_service -v`
Expected: FAIL, `ImportError: cannot import name 'classify_order'`

- [ ] **Step 3: Дописать классификацию**

В конец `backend/app/logistics_zone_service.py`:

```python
ZONE_CITY = "city"
ZONE_REGION = "region"
ZONE_UNASSIGNED = "unassigned"


def classify_order(client_name, coordinates_value, index: RegionIndex) -> str:
    """Rule order matters: the directory wins, coordinates only decide unknown clients."""
    point = parse_coordinates(coordinates_value)
    latitude, longitude = point if point is not None else (None, None)
    if index.find(client_name, latitude, longitude) is not None:
        return ZONE_REGION
    if point is None:
        return ZONE_CITY
    if point_in_city(latitude, longitude):
        return ZONE_CITY
    return ZONE_UNASSIGNED
```

- [ ] **Step 4: Запустить весь файл тестов**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_service -v`
Expected: PASS, все классы тестов

- [ ] **Step 5: Коммит**

```bash
git add backend/app/logistics_zone_service.py tests/test_logistics_zone_service.py
git commit -m "feat(logistics): правило классификации заказа город, область, неопределённый

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Имена файлов, подписи и хеш текстовой политики

**Files:**
- Modify: `backend/app/telegram_output_contract.py:60-65`, `105-151`
- Modify: `backend/app/telegram_routing_manifest.json:26-33`
- Test: `tests/test_telegram_routing_contract.py` (дополняется)

**Interfaces:**
- Consumes: ничего
- Produces:
  - `LOGISTICS_ZONE_CITY = "city"`, `LOGISTICS_ZONE_REGION = "region"`
  - `logistics_report_caption(report_date, zone) -> str`
  - `logistics_report_filename(report_date, zone) -> str`

Это client-facing изменение, тексты согласованы 2026-08-04 и менять их нельзя

- [ ] **Step 1: Написать падающий тест на точные тексты**

Добавить в `tests/test_telegram_routing_contract.py` внутрь класса
`TelegramRoutingContractTests`:

```python
    def test_logistics_artifact_carries_both_zone_outputs(self):
        from backend.app.telegram_output_contract import (
            LOGISTICS_ZONE_CITY,
            LOGISTICS_ZONE_REGION,
            logistics_report_caption,
            logistics_report_filename,
        )

        self.assertEqual(
            logistics_report_caption("2030-01-02", LOGISTICS_ZONE_CITY),
            "Отчет логистики город 02.01.2030",
        )
        self.assertEqual(
            logistics_report_caption("2030-01-02", LOGISTICS_ZONE_REGION),
            "Отчет логистики область 02.01.2030",
        )
        self.assertEqual(
            logistics_report_filename("2030-01-02", LOGISTICS_ZONE_CITY),
            "TakSklad_логистика_город_02.01.2030.xlsx",
        )
        self.assertEqual(
            logistics_report_filename("2030-01-02", LOGISTICS_ZONE_REGION),
            "TakSklad_логистика_область_02.01.2030.xlsx",
        )
        with self.assertRaises(ValueError):
            logistics_report_filename("2030-01-02", "unknown")

        artifact = runtime_output_artifacts()[
            TelegramMessageKind.SMARTUP_LOGISTICS_REPORT.value
        ]
        self.assertEqual(
            set(artifact),
            {"city_caption", "city_filename", "region_caption", "region_filename"},
        )
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `PYTHONPATH=. python -m unittest tests.test_telegram_routing_contract.TelegramRoutingContractTests.test_logistics_artifact_carries_both_zone_outputs -v`
Expected: FAIL, `ImportError: cannot import name 'LOGISTICS_ZONE_CITY'`

- [ ] **Step 3: Изменить builders**

В `backend/app/telegram_output_contract.py` заменить строки 60-65 на:

```python
LOGISTICS_ZONE_CITY = "city"
LOGISTICS_ZONE_REGION = "region"

_LOGISTICS_ZONE_LABELS = {
    LOGISTICS_ZONE_CITY: "город",
    LOGISTICS_ZONE_REGION: "область",
}


def _logistics_zone_label(zone: Any) -> str:
    label = _LOGISTICS_ZONE_LABELS.get(_text(zone))
    if not label:
        raise ValueError(f"Unsupported logistics zone: {zone!r}")
    return label


def logistics_report_caption(report_date: Any, zone: Any) -> str:
    return f"Отчет логистики {_logistics_zone_label(zone)} {_display_date(report_date)}"


def logistics_report_filename(report_date: Any, zone: Any) -> str:
    return f"TakSklad_логистика_{_logistics_zone_label(zone)}_{_display_date(report_date)}.xlsx"
```

- [ ] **Step 4: Изменить артефакт**

В `backend/app/telegram_output_contract.py` в `runtime_output_artifacts()`
заменить блок `"smartup_logistics_report"` на:

```python
        "smartup_logistics_report": {
            "city_caption": logistics_report_caption(sample_date, LOGISTICS_ZONE_CITY),
            "city_filename": logistics_report_filename(sample_date, LOGISTICS_ZONE_CITY),
            "region_caption": logistics_report_caption(sample_date, LOGISTICS_ZONE_REGION),
            "region_filename": logistics_report_filename(sample_date, LOGISTICS_ZONE_REGION),
        },
```

- [ ] **Step 5: Получить новый хеш**

Run:

```bash
PYTHONPATH=. python -c "from backend.app.telegram_output_contract import runtime_output_policy_hashes; print(runtime_output_policy_hashes()['smartup_logistics_report'])"
```

Expected: 64 шестнадцатеричных символа, отличных от `7e61d7429c921cb1bc0972886e52f0793a23c47412f00d5a400b7a212cae8dea`

- [ ] **Step 6: Вписать хеш и алиас в манифест**

В `backend/app/telegram_routing_manifest.json` в блоке `smartup_logistics_report`
заменить значение `text_policy_sha256` на полученное в предыдущем шаге
Состав `message_kinds` не менять

В блоке `notification_kind_aliases` добавить строку:

```json
    "logistics_zone_unassigned_order": "admin_error",
```

- [ ] **Step 7: Запустить контрактные тесты**

Run: `PYTHONPATH=. python -m unittest tests.test_telegram_routing_contract -v`
Expected: PASS, включая `test_text_policies_are_exact_hashes_of_runtime_builder_outputs`

- [ ] **Step 8: Прогнать no-send verifier**

Run: `PYTHONPATH=. python tools/verify_telegram_routing_contract.py`
Expected: exit code 0, ни одного отправленного сообщения

- [ ] **Step 9: Коммит**

```bash
git add backend/app/telegram_output_contract.py backend/app/telegram_routing_manifest.json tests/test_telegram_routing_contract.py
git commit -m "feat(logistics): имена файлов и подписи отчёта с зоной, новый хеш политики

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Сборка двух отчётов вместо одного

**Files:**
- Modify: `backend/app/logistics_service.py:110-176`, `182-196`
- Test: `tests/test_logistics_report_split.py` (создаётся)

**Interfaces:**
- Consumes: `classify_order`, `load_region_index`, `parse_coordinates`, `ZONE_*` из Task 4,
  `logistics_report_filename(report_date, zone)` из Task 5
- Produces:
  - `build_logistics_reports(db, shipment_date) -> dict`
    ключи `"city"` и `"region"`: `tuple[bytes, str] | None`, ключ `"unassigned"`: `list[Order]`
  - `build_logistics_report_xlsx(db, shipment_date, zone) -> tuple[bytes, str]`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_logistics_report_split.py`:

```python
import unittest
from datetime import date
from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.logistics_service import build_logistics_report_xlsx, build_logistics_reports
from backend.app.models import Base, LogisticsRegionPoint, Order, OrderItem
from backend.app.orders_service import ApiError


SHIPMENT_DATE = date(2030, 1, 2)


class LogisticsReportSplitTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        self.db.add(LogisticsRegionPoint(
            client_name="Тест Клиент Область",
            normalized_client="тестклиентобласть",
            latitude=41.018778,
            longitude=70.083423,
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_order(self, client, coordinates, address="Тестовый адрес"):
        order = Order(
            payment_type="Наличные",
            client=client,
            address=address,
            status="not_completed",
            order_date=SHIPMENT_DATE,
            raw_payload={"coordinates": coordinates},
        )
        order.items.append(OrderItem(product="Тестовый товар", quantity_blocks=3))
        self.db.add(order)
        self.db.commit()
        return order

    def sheet_rows(self, payload):
        workbook = load_workbook(BytesIO(payload))
        return [
            [cell for cell in row]
            for row in workbook["Orders"].iter_rows(min_row=2, values_only=True)
        ]

    def test_orders_split_between_city_and_region_files(self):
        self.add_order("Тест Клиент Область", "41.018778,70.083423")
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())

        self.assertIsNotNone(reports["city"])
        self.assertIsNotNone(reports["region"])
        self.assertEqual(reports["unassigned"], [])

        city_payload, city_filename = reports["city"]
        region_payload, region_filename = reports["region"]
        self.assertEqual(city_filename, "TakSklad_логистика_город_02.01.2030.xlsx")
        self.assertEqual(region_filename, "TakSklad_логистика_область_02.01.2030.xlsx")

        city_clients = {row[3] for row in self.sheet_rows(city_payload)}
        region_clients = {row[3] for row in self.sheet_rows(region_payload)}
        self.assertEqual(city_clients, {"Тест Клиент Город"})
        self.assertEqual(region_clients, {"Тест Клиент Область"})

    def test_unknown_client_outside_city_is_unassigned_and_absent_from_both_files(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        self.add_order("Незнакомый Загород", "41.4700,69.5800")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())

        self.assertIsNone(reports["region"])
        self.assertEqual(len(reports["unassigned"]), 1)
        self.assertEqual(reports["unassigned"][0].client, "Незнакомый Загород")

        city_clients = {row[3] for row in self.sheet_rows(reports["city"][0])}
        self.assertEqual(city_clients, {"Тест Клиент Город"})

    def test_zone_without_orders_produces_no_file(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())
        self.assertIsNone(reports["region"])
        self.assertIsNotNone(reports["city"])

    def test_unknown_client_without_coordinates_lands_in_city_problem_sheet(self):
        self.add_order("Незнакомый Без Координат", "")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())
        payload, _filename = reports["city"]
        workbook = load_workbook(BytesIO(payload))
        self.assertIn("Требуют координаты", workbook.sheetnames)
        problem_clients = [
            row[0] for row in workbook["Требуют координаты"].iter_rows(min_row=2, values_only=True)
        ]
        self.assertEqual(problem_clients, ["Незнакомый Без Координат"])

    def test_known_client_without_coordinates_lands_in_region_problem_sheet(self):
        self.add_order("Тест Клиент Область", "")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())
        self.assertIsNone(reports["city"])
        payload, _filename = reports["region"]
        workbook = load_workbook(BytesIO(payload))
        problem_clients = [
            row[0] for row in workbook["Требуют координаты"].iter_rows(min_row=2, values_only=True)
        ]
        self.assertEqual(problem_clients, ["Тест Клиент Область"])

    def test_single_zone_builder_raises_404_for_empty_zone(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        payload, filename = build_logistics_report_xlsx(self.db, SHIPMENT_DATE.isoformat(), "city")
        self.assertTrue(payload)
        self.assertEqual(filename, "TakSklad_логистика_город_02.01.2030.xlsx")
        with self.assertRaises(ApiError) as raised:
            build_logistics_report_xlsx(self.db, SHIPMENT_DATE.isoformat(), "region")
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_report_split -v`
Expected: FAIL, `ImportError: cannot import name 'build_logistics_reports'`

- [ ] **Step 3: Переиспользовать разбор координат из модуля зоны**

В `backend/app/logistics_service.py` заменить тело `normalize_coordinates`
(строки 182-196) на вызов общей функции, чтобы regex не жил в двух местах:

```python
def normalize_coordinates(value):
    point = parse_coordinates(value)
    if point is None:
        return ""
    latitude, longitude = point
    return f"{format_coordinate(latitude)},{format_coordinate(longitude)}"
```

и добавить импорт после строки 17:

```python
from .logistics_zone_service import (
    ZONE_CITY,
    ZONE_REGION,
    ZONE_UNASSIGNED,
    classify_order,
    load_region_index,
    parse_coordinates,
)
```

Импорт `import re` в начале файла остаётся: `re` используется
в `normalize_lookup_text` и `delivery_window_datetime`

- [ ] **Step 4: Разделить сборку на выбор заказов и рисование книги**

В `backend/app/logistics_service.py` заменить `build_logistics_report_xlsx`
(строки 110-176) на три функции:

```python
def build_logistics_reports(db: Session, shipment_date: str):
    """Split candidate orders into city and region reports in a single pass."""
    report_date = parse_report_date(shipment_date)
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.order_date == report_date)
        .order_by(Order.client.asc(), Order.created_at.asc())
    ).scalars().all()
    if not orders:
        raise ApiError(404, f"No orders for shipment date {report_date.isoformat()}")
    candidate_orders = [order for order in orders if is_logistics_candidate_order(order)]
    if not candidate_orders:
        raise ApiError(404, f"No logistics delivery orders for shipment date {report_date.isoformat()}")

    region_index = load_region_index(db)
    zone_orders = {ZONE_CITY: [], ZONE_REGION: []}
    unassigned_orders = []
    for order in candidate_orders:
        zone = classify_order(
            order.client,
            (order.raw_payload or {}).get("coordinates"),
            region_index,
        )
        if zone == ZONE_UNASSIGNED:
            unassigned_orders.append(order)
        else:
            zone_orders[zone].append(order)

    reports = {ZONE_CITY: None, ZONE_REGION: None, ZONE_UNASSIGNED: unassigned_orders}
    for zone in (ZONE_CITY, ZONE_REGION):
        if zone_orders[zone]:
            reports[zone] = build_zone_report_xlsx(db, report_date, zone, zone_orders[zone])
    return reports


def build_logistics_report_xlsx(db: Session, shipment_date: str, zone: str):
    reports = build_logistics_reports(db, shipment_date)
    report = reports.get(zone)
    if report is None:
        report_date = parse_report_date(shipment_date)
        raise ApiError(
            404,
            f"No {zone} logistics delivery orders for shipment date {report_date.isoformat()}",
        )
    return report


def build_zone_report_xlsx(db: Session, report_date, zone: str, zone_orders):
    delivery_orders = [order for order in zone_orders if is_logistics_delivery_order(order)]
    coordinate_problem_orders = [order for order in zone_orders if not is_logistics_delivery_order(order)]
    delivery_slots = client_point_delivery_slot_map(db, delivery_orders)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Orders"
    sheet.append(LOGISTICS_HEADERS)
    apply_orders_template_style(sheet)

    for order in delivery_orders:
        coordinates = normalize_coordinates((order.raw_payload or {}).get("coordinates"))
        latitude, longitude = split_coordinates(coordinates)
        delivery_from, delivery_to = delivery_slot_for_order(order, delivery_slots)
        for item in sorted(order.items, key=lambda value: (value.product, str(value.id))):
            quantity_blocks = item_quantity_blocks(item)
            row = [""] * len(LOGISTICS_HEADERS)
            set_cell(row, 1, "delivery")
            set_cell(row, 2, logistics_external_id(order, item))
            set_cell(row, 4, order.client)
            set_cell(row, 7, order.representative or "")
            set_cell(row, 17, latitude)
            set_cell(row, 18, longitude)
            set_cell(row, 19, order.address)
            set_cell(row, 20, delivery_window_datetime(report_date, delivery_from))
            set_cell(row, 21, delivery_window_datetime(report_date, delivery_to))
            set_cell(row, 27, item.product)
            set_cell(row, 29, 0)
            set_cell(row, 30, 0)
            set_cell(row, 31, quantity_blocks)
            sheet.append(row)
            apply_orders_row_style(sheet, sheet.max_row)

    if coordinate_problem_orders:
        problem_sheet = workbook.create_sheet("Требуют координаты")
        problem_sheet.append(LOGISTICS_COORDINATE_PROBLEM_HEADERS)
        apply_header_style(problem_sheet)
        for order in coordinate_problem_orders:
            problem_sheet.append([
                order.client,
                order.address,
                logistics_external_id(order),
                logistics_coordinate_problem_reason(order),
                order_product_summary(order),
                order.payment_type,
                report_date.strftime("%d.%m.%Y"),
                (order.raw_payload or {}).get("skladbot_request_number") or "",
            ])
        autosize_columns(problem_sheet)
    buffer = BytesIO()
    force_workbook_text_literals(workbook)
    workbook.save(buffer)
    return buffer.getvalue(), logistics_report_filename(report_date, zone)
```

- [ ] **Step 5: Запустить тесты, убедиться что проходят**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_report_split -v`
Expected: PASS, все 6 тестов

- [ ] **Step 6: Коммит**

```bash
git add backend/app/logistics_service.py tests/test_logistics_report_split.py
git commit -m "feat(logistics): сборка городского и областного отчёта одним проходом

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Параметр зоны у HTTP-эндпоинта отчёта

**Files:**
- Modify: `backend/app/main.py:1735-1748`
- Test: `tests/test_logistics_report_split.py` (дополняется)

**Interfaces:**
- Consumes: `build_logistics_report_xlsx(db, shipment_date, zone)` из Task 6
- Produces: `GET /api/v1/logistics/report?shipment_date=<iso>&zone=<city|region>`

- [ ] **Step 1: Написать падающий тест на валидацию зоны**

Добавить в `tests/test_logistics_report_split.py` в класс `LogisticsReportSplitTests`:

```python
    def test_unknown_zone_is_rejected_with_422(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        with self.assertRaises(ApiError) as raised:
            build_logistics_report_xlsx(self.db, SHIPMENT_DATE.isoformat(), "moon")
        self.assertEqual(raised.exception.status_code, 422)
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_report_split.LogisticsReportSplitTests.test_unknown_zone_is_rejected_with_422 -v`
Expected: FAIL, поднимается `ApiError` с кодом 404, а не 422

- [ ] **Step 3: Добавить валидацию зоны в builder**

В `backend/app/logistics_service.py` в начало `build_logistics_report_xlsx` добавить:

```python
    if zone not in (ZONE_CITY, ZONE_REGION):
        raise ApiError(422, f"Unsupported logistics zone: {zone}")
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_report_split -v`
Expected: PASS

- [ ] **Step 5: Пробросить параметр в эндпоинт**

В `backend/app/main.py` заменить строки 1735-1738 на:

```python
@api.get("/logistics/report")
def logistics_report(shipment_date: str, zone: str, db=Depends(get_db)):
    try:
        content, filename = build_logistics_report_xlsx(db, shipment_date, zone)
```

Остальное тело функции не менять

- [ ] **Step 6: Проверить, что модуль компилируется**

Run: `PYTHONPATH=. python -m compileall -q backend/app`
Expected: без вывода, код возврата 0

- [ ] **Step 7: Коммит**

```bash
git add backend/app/main.py backend/app/logistics_service.py tests/test_logistics_report_split.py
git commit -m "feat(logistics): обязательный параметр zone у эндпоинта отчёта

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Ручная отправка двух документов из бота

**Files:**
- Modify: `backend/app/telegram_report_processor.py:15`, `205-232`
- Test: `tests/test_logistics_telegram_split.py` (создаётся)

**Interfaces:**
- Consumes: `logistics_report_caption(date, zone)`, `logistics_report_filename(date, zone)` из Task 5
- Produces: `TelegramReportProcessor.send_logistics_report(chat_id, shipment_date) -> bool`
  отправляет от нуля до двух документов, городской первым

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_logistics_telegram_split.py`:

```python
import unittest
from unittest import mock

import httpx

from backend.app.telegram_report_processor import TelegramReportProcessor


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class LogisticsTelegramSplitTests(unittest.TestCase):
    def setUp(self):
        self.processor = TelegramReportProcessor.__new__(TelegramReportProcessor)
        self.sent_documents = []
        self.sent_messages = []
        self.processor.safe_send_document = lambda chat_id, content, filename, caption=None: (
            self.sent_documents.append((chat_id, content, filename, caption))
        )
        self.processor.safe_send_message = lambda chat_id, text, **kwargs: (
            self.sent_messages.append((chat_id, text))
        )

    def stub_backend(self, available_zones):
        def backend_get_bytes(path, params=None):
            zone = (params or {}).get("zone")
            if zone not in available_zones:
                raise httpx.HTTPStatusError(
                    "not found",
                    request=mock.Mock(),
                    response=FakeResponse(404),
                )
            return b"payload-" + zone.encode(), {}

        self.processor.backend_get_bytes = backend_get_bytes

    def test_sends_city_first_then_region(self):
        self.stub_backend({"city", "region"})
        result = self.processor.send_logistics_report(555, "2030-01-02")
        self.assertTrue(result)
        self.assertEqual(
            [item[2] for item in self.sent_documents],
            [
                "TakSklad_логистика_город_02.01.2030.xlsx",
                "TakSklad_логистика_область_02.01.2030.xlsx",
            ],
        )
        self.assertEqual(
            [item[3] for item in self.sent_documents],
            ["Отчет логистики город 02.01.2030", "Отчет логистики область 02.01.2030"],
        )

    def test_empty_zone_is_skipped_without_error_message(self):
        self.stub_backend({"city"})
        result = self.processor.send_logistics_report(555, "2030-01-02")
        self.assertTrue(result)
        self.assertEqual(len(self.sent_documents), 1)
        self.assertEqual(self.sent_messages, [])

    def test_both_zones_empty_reports_once(self):
        self.stub_backend(set())
        result = self.processor.send_logistics_report(555, "2030-01-02")
        self.assertFalse(result)
        self.assertEqual(self.sent_documents, [])
        self.assertEqual(len(self.sent_messages), 1)
        self.assertIn("02.01.2030", self.sent_messages[0][1])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_telegram_split -v`
Expected: FAIL, документы приходят со старым именем без зоны

- [ ] **Step 3: Переписать `send_logistics_report`**

В `backend/app/telegram_report_processor.py` заменить строку 15 на:

```python
from .telegram_output_contract import (
    LOGISTICS_ZONE_CITY,
    LOGISTICS_ZONE_REGION,
    logistics_report_caption,
    logistics_report_filename,
)
```

и заменить метод `send_logistics_report` (строки 205-232) на:

```python
    def send_logistics_report(self, chat_id, shipment_date):
        iso_date = iso_date_from_display(shipment_date)
        if not iso_date:
            self.safe_send_message(chat_id, "Не понял дату. Используйте формат 29.05.2026.")
            return False
        report_date = datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")
        sent = 0
        for zone in (LOGISTICS_ZONE_CITY, LOGISTICS_ZONE_REGION):
            try:
                content, headers = self.backend_get_bytes(
                    "/api/v1/logistics/report",
                    params={"shipment_date": iso_date, "zone": zone},
                )
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    continue
                self.safe_send_message(
                    chat_id,
                    f"Не удалось выгрузить отчёт логистики за {report_date}: {backend_http_error_detail(exc)}",
                )
                return False
            except httpx.HTTPError as exc:
                self.safe_send_message(
                    chat_id,
                    f"Не удалось выгрузить отчёт логистики за {report_date}: backend временно недоступен ({exc.__class__.__name__})",
                )
                return False
            self.safe_send_document(
                chat_id,
                content,
                logistics_report_filename(iso_date, zone),
                caption=logistics_report_caption(iso_date, zone),
            )
            sent += 1
        if not sent:
            self.safe_send_message(chat_id, f"Нет заказов логистики за {report_date}.")
            return False
        return True
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_telegram_split -v`
Expected: PASS, все 3 теста

- [ ] **Step 5: Коммит**

```bash
git add backend/app/telegram_report_processor.py tests/test_logistics_telegram_split.py
git commit -m "feat(logistics): кнопка /logistics отдаёт городской и областной отчёт

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Автоматическая отправка в 17:50 и алерт по неопределённым заказам

**Files:**
- Modify: `backend/app/smartup_auto_import.py:34`, `63`, `75-76`, `1942-1994`
- Test: `tests/test_smartup_auto_import.py` (обновляются 7 существующих мест)

**Interfaces:**
- Consumes: `build_logistics_reports(db, shipment_date)` из Task 6,
  `logistics_report_caption(date, zone)` из Task 5,
  паттерн постановки алерта из `queue_smartup_duplicate_deal_alert` (`smartup_auto_import.py:848`)
- Produces:
  - `LOGISTICS_ZONE_ALERT_KIND = "logistics_zone_unassigned_order"`
  - `queue_logistics_zone_unassigned_alert(db, delivery_date, orders) -> PendingEvent | None`

- [ ] **Step 1: Найти все места, где тест мокает сборку отчёта**

Run: `grep -n "build_logistics_report_xlsx" tests/test_smartup_auto_import.py`
Expected: 7 строк (2155, 2210, 2276, 2330, 2383, 2466, 2505)
Каждая из них заменится на `build_logistics_reports` с новым возвращаемым значением

- [ ] **Step 2: Написать падающий тест на две отправки и алерт**

Добавить в `tests/test_smartup_auto_import.py` в класс `SmartupAutoImportTests`,
рядом с существующими тестами логистики
Обвязка та же, что в `test_route_change_does_not_resend_confirmed_delivery`:
`FakeTelegramSender` копит документы в `sender.documents` кортежами
`(chat_id, content, filename, caption)`

```python
    def test_logistics_slot_sends_both_zone_documents_and_alerts_unassigned(self):
        sender = FakeTelegramSender()
        config = self.config("/tmp", logistics_chat_id="-1001002")
        unassigned_order = mock.Mock()
        unassigned_order.client = "Незнакомый Загород"
        unassigned_order.address = "Тестовый адрес"
        unassigned_order.payment_type = "Наличные"
        unassigned_order.order_date = date(2026, 6, 26)
        unassigned_order.raw_payload = {
            "coordinates": "41.4700,69.5800",
            "skladbot_request_number": "TEST-1",
        }
        unassigned_order.items = []

        with mock.patch(
            "backend.app.smartup_auto_import.build_logistics_reports",
            return_value={
                "city": (b"city-bytes", "TakSklad_логистика_город_26.06.2026.xlsx"),
                "region": (b"region-bytes", "TakSklad_логистика_область_26.06.2026.xlsx"),
                "unassigned": [unassigned_order],
            },
        ):
            with self.SessionLocal() as db:
                send_final_logistics_reports(
                    db,
                    config,
                    export_date=date(2026, 6, 25),
                    extra_delivery_dates=["2026-06-26"],
                    telegram_sender=sender,
                )
                alerts = db.execute(
                    select(PendingEvent).where(
                        PendingEvent.event_type == "telegram_notification"
                    )
                ).scalars().all()

        self.assertEqual(
            [document[2] for document in sender.documents],
            [
                "TakSklad_логистика_город_26.06.2026.xlsx",
                "TakSklad_логистика_область_26.06.2026.xlsx",
            ],
        )
        self.assertEqual(
            [document[3] for document in sender.documents],
            ["Отчет логистики город 26.06.2026", "Отчет логистики область 26.06.2026"],
        )
        self.assertIn(
            "logistics_zone_unassigned_order",
            [event.payload.get("kind") for event in alerts],
        )

    def test_logistics_slot_skips_empty_zone(self):
        sender = FakeTelegramSender()
        config = self.config("/tmp", logistics_chat_id="-1001002")
        with mock.patch(
            "backend.app.smartup_auto_import.build_logistics_reports",
            return_value={
                "city": (b"city-bytes", "TakSklad_логистика_город_26.06.2026.xlsx"),
                "region": None,
                "unassigned": [],
            },
        ):
            with self.SessionLocal() as db:
                send_final_logistics_reports(
                    db,
                    config,
                    export_date=date(2026, 6, 25),
                    extra_delivery_dates=["2026-06-26"],
                    telegram_sender=sender,
                )
        self.assertEqual(
            [document[2] for document in sender.documents],
            ["TakSklad_логистика_город_26.06.2026.xlsx"],
        )
```

- [ ] **Step 3: Запустить тест, убедиться что падает**

Run: `PYTHONPATH=. python -m unittest tests.test_smartup_auto_import -v -k logistics`
Expected: FAIL, `AttributeError: build_logistics_reports`

- [ ] **Step 4: Обновить импорты и константу**

В `backend/app/smartup_auto_import.py:34` заменить на:

```python
from .logistics_service import build_logistics_reports, list_logistics_dates
```

В блоке импортов из `telegram_output_contract` (строка 63) добавить:

```python
    LOGISTICS_ZONE_CITY,
    LOGISTICS_ZONE_REGION,
```

Рядом со строкой 75 добавить:

```python
LOGISTICS_ZONE_ALERT_KIND = "logistics_zone_unassigned_order"
LOGISTICS_ZONE_ALERT_ORDER_LIMIT = 20
```

- [ ] **Step 5: Добавить постановку алерта**

В `backend/app/smartup_auto_import.py` сразу после функции
`queue_smartup_duplicate_deal_alert` добавить:

```python
def queue_logistics_zone_unassigned_alert(
    db: Session,
    delivery_date: date,
    orders: list,
) -> PendingEvent | None:
    """Queue one admin-only alert listing orders that fit neither city nor region."""
    if not orders:
        return None
    key = f"telegram:notification:v1:logistics_zone_unassigned:{delivery_date.isoformat()}"
    existing = db.execute(
        select(PendingEvent).where(PendingEvent.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    shown = orders[:LOGISTICS_ZONE_ALERT_ORDER_LIMIT]
    hidden = len(orders) - len(shown)
    lines = [
        "TakSklad: заказы вне городской и областной зоны не отправлены",
        f"Дата отгрузки: {format_display_date(delivery_date)}",
        f"Заказов: {len(orders)}",
        "",
    ]
    for order in shown:
        raw_payload = order.raw_payload or {}
        products = "; ".join(
            f"{item.product} - {item.quantity_blocks or 0} блоков"
            for item in sorted(order.items, key=lambda value: (value.product, str(value.id)))
        )
        lines.extend([
            f"Клиент: {order.client}",
            f"Адрес: {order.address}",
            f"Координаты: {normalize_text(raw_payload.get('coordinates')) or '-'}",
            f"Складская заявка: {normalize_text(raw_payload.get('skladbot_request_number')) or '-'}",
            f"Товары: {products or '-'}",
            "",
        ])
    if hidden > 0:
        lines.append(f"и ещё {hidden}")
    event = PendingEvent(
        event_type="telegram_notification",
        status="pending",
        idempotency_key=key,
        payload={
            "kind": LOGISTICS_ZONE_ALERT_KIND,
            "route_role": "admin",
            "delivery_date": delivery_date.isoformat(),
            "orders_count": len(orders),
            "text": "\n".join(lines).strip(),
        },
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return db.execute(
            select(PendingEvent).where(PendingEvent.idempotency_key == key)
        ).scalar_one()
    db.refresh(event)
    return event
```

- [ ] **Step 6: Переписать блок сборки и отправки**

В `backend/app/smartup_auto_import.py` заменить строки 1942-1972
(блок `try: content, filename = build_logistics_report_xlsx(...)` и первую отправку)
на:

```python
        try:
            reports = build_logistics_reports(db, delivery_date)
        except Exception as exc:
            result = {
                "status": "failed",
                "provenance": "auto_smartup",
                "route_role": "logistics",
                "route_fingerprint": route_fingerprint,
                "delivery_date": delivery_date,
                "error": sanitize_automation_error_text(exc, limit=500),
                "delivery_started": False,
            }
            mark_smartup_logistics_report_failed(db, config, event.id, result, now=current_time)
            notify_smartup_automation_error(
                db,
                config,
                export_date=export_date,
                slot_label=f"logistics:{normalized_delivery_date}",
                exc=SmartupAutoImportError(
                    f"Logistics report build failed: {sanitize_automation_error_text(exc, limit=500)}"
                ),
                telegram_sender=telegram_sender,
            )
        else:
            sent_filenames = []
            try:
                for zone in (LOGISTICS_ZONE_CITY, LOGISTICS_ZONE_REGION):
                    report = reports.get(zone)
                    if report is None:
                        continue
                    content, filename = report
                    sender.send_document(
                        config.logistics_chat_id,
                        content,
                        filename,
                        caption=logistics_report_caption(delivery_date, zone),
                    )
                    sent_filenames.append(filename)
```

Дальше идёт существующий блок `except Exception as exc:` со статусом `ambiguous`,
его не менять: частичная отправка по-прежнему блокирует автоповтор

- [ ] **Step 7: Поправить успешный результат и поставить алерт**

Существующая ветка `else` (строки 1995-2004) пишет `"filename": filename`,
при двух файлах эта переменная неоднозначна
Заменить ветку на:

```python
            else:
                queue_logistics_zone_unassigned_alert(
                    db,
                    parsed_delivery_date,
                    reports.get("unassigned") or [],
                )
                result = {
                    "status": "sent",
                    "provenance": "auto_smartup",
                    "route_role": "logistics",
                    "route_fingerprint": route_fingerprint,
                    "delivery_date": delivery_date,
                    "filenames": sent_filenames,
                    "unassigned_orders": len(reports.get("unassigned") or []),
                }
                mark_smartup_logistics_report_completed(db, event.id, result)
```

Ключ `filename` заменён на `filenames`, это внутренний payload события и аудита,
client-facing вывод он не затрагивает
Существующие тесты читают `audit["filename"]` только для выгрузки Smartup
(`Терминал ... Часть 1.xlsx`), логистику это не задевает

Алерт ставится после обеих успешных отправок и не влияет на статус события

- [ ] **Step 8: Обновить 7 существующих моков в тестах**

В `tests/test_smartup_auto_import.py` в каждой из 7 строк заменить
`"backend.app.smartup_auto_import.build_logistics_report_xlsx"` на
`"backend.app.smartup_auto_import.build_logistics_reports"`, а `return_value`
с `(b"...", "имя.xlsx")` на словарь:

```python
            return_value={
                "city": (b"logistics-bytes", "TakSklad_логистика_город_02.01.2030.xlsx"),
                "region": None,
                "unassigned": [],
            },
```

Даты в именах файлов подставить те, что использует конкретный тест

- [ ] **Step 9: Запустить тесты**

Run: `PYTHONPATH=. python -m unittest tests.test_smartup_auto_import -v`
Expected: PASS, включая два новых теста и 7 обновлённых

- [ ] **Step 10: Прогнать весь набор тестов**

Run: `PYTHONPATH=. python -m unittest discover -s tests`
Expected: PASS, ни одного FAIL и ERROR

- [ ] **Step 11: Коммит**

```bash
git add backend/app/smartup_auto_import.py tests/test_smartup_auto_import.py
git commit -m "feat(logistics): автоотправка двух отчётов и алерт по заказам вне зон

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Скрипт загрузки справочника из адресной программы

**Files:**
- Create: `tools/seed_logistics_region_points.py`
- Test: `tests/test_seed_logistics_region_points.py` (создаётся)

**Interfaces:**
- Consumes: `LogisticsRegionPoint` из Task 1, `normalize_client_key` из Task 3
- Produces:
  - `read_region_rows(path) -> list[dict]` ключи `client_name`, `latitude`, `longitude`, `agent`
  - `plan_changes(db, rows) -> dict` ключи `existing`, `insert`, `update`, `unchanged`, `total_after`
  - `apply_changes(db, rows) -> dict`
  - CLI: `python tools/seed_logistics_region_points.py <путь> [--dry-run]`

По рабочему контракту скрипт сначала печатает числа, потом пишет

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_seed_logistics_region_points.py`:

```python
import unittest
from pathlib import Path
import tempfile

from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, LogisticsRegionPoint
from tools.seed_logistics_region_points import apply_changes, plan_changes, read_region_rows


def write_source(path, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "База клиентов"
    sheet.append(["Клиент", "Широта", "Долгота", "Агент"])
    for row in rows:
        sheet.append(row)
    workbook.save(path)


class SeedLogisticsRegionPointsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        self.tempdir = tempfile.TemporaryDirectory()
        self.source = Path(self.tempdir.name) / "source.xlsx"

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_reads_rows_and_drops_full_duplicates(self):
        write_source(self.source, [
            ["Тест Клиент Один", 41.018778, 70.083423, "Агент 3"],
            ["Тест Клиент Один", 41.018778, 70.083423, "Агент 3"],
            ["Тест Клиент Два", 40.847219, 69.620199, "Агент 2"],
        ])
        rows = read_region_rows(self.source)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["client_name"], "Тест Клиент Один")
        self.assertEqual(rows[0]["agent"], "Агент 3")

    def test_skips_rows_without_name_or_coordinates(self):
        write_source(self.source, [
            ["Тест Клиент Один", 41.018778, 70.083423, "Агент 3"],
            ["", 41.0, 70.0, "Агент 1"],
            ["Тест Без Координат", None, None, "Агент 1"],
        ])
        self.assertEqual(len(read_region_rows(self.source)), 1)

    def test_plan_reports_counts_before_writing(self):
        write_source(self.source, [
            ["Тест Клиент Один", 41.018778, 70.083423, "Агент 3"],
            ["Тест Клиент Два", 40.847219, 69.620199, "Агент 2"],
        ])
        rows = read_region_rows(self.source)
        plan = plan_changes(self.db, rows)
        self.assertEqual(plan["existing"], 0)
        self.assertEqual(plan["insert"], 2)
        self.assertEqual(plan["update"], 0)
        self.assertEqual(plan["total_after"], 2)
        self.assertEqual(self.db.execute(select(LogisticsRegionPoint)).scalars().all(), [])

    def test_apply_is_idempotent(self):
        write_source(self.source, [
            ["Тест Клиент Один", 41.018778, 70.083423, "Агент 3"],
        ])
        rows = read_region_rows(self.source)
        first = apply_changes(self.db, rows)
        self.assertEqual(first["insert"], 1)
        second = apply_changes(self.db, rows)
        self.assertEqual(second["insert"], 0)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(len(self.db.execute(select(LogisticsRegionPoint)).scalars().all()), 1)

    def test_apply_updates_agent_without_creating_duplicate(self):
        write_source(self.source, [["Тест Клиент Один", 41.018778, 70.083423, "Агент 3"]])
        apply_changes(self.db, read_region_rows(self.source))
        write_source(self.source, [["Тест Клиент Один", 41.018778, 70.083423, "Агент 1"]])
        result = apply_changes(self.db, read_region_rows(self.source))
        self.assertEqual(result["update"], 1)
        point = self.db.execute(select(LogisticsRegionPoint)).scalar_one()
        self.assertEqual(point.agent, "Агент 1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `PYTHONPATH=. python -m unittest tests.test_seed_logistics_region_points -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'tools.seed_logistics_region_points'`

- [ ] **Step 3: Написать скрипт**

Создать `tools/seed_logistics_region_points.py`:

```python
"""Load the region address programme into logistics_region_points.

The source workbook stays outside the repository: it holds counterparty names
and coordinates, and this repository is public.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.logistics_zone_service import normalize_client_key  # noqa: E402
from backend.app.models import LogisticsRegionPoint  # noqa: E402


EXPECTED_HEADERS = ("Клиент", "Широта", "Долгота", "Агент")


def read_region_rows(path) -> list[dict]:
    workbook = load_workbook(path, data_only=True, read_only=True)
    sheet = workbook.worksheets[0]
    rows = []
    seen = set()
    for index, values in enumerate(sheet.iter_rows(values_only=True)):
        if index == 0:
            continue
        client_name = str(values[0] or "").strip()
        latitude = values[1] if len(values) > 1 else None
        longitude = values[2] if len(values) > 2 else None
        agent = str(values[3] or "").strip() if len(values) > 3 else ""
        if not client_name:
            continue
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            continue
        latitude = round(float(latitude), 6)
        longitude = round(float(longitude), 6)
        key = (normalize_client_key(client_name), latitude, longitude)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "client_name": client_name,
            "normalized_client": key[0],
            "latitude": latitude,
            "longitude": longitude,
            "agent": agent or None,
        })
    workbook.close()
    return rows


def existing_by_key(db: Session) -> dict:
    points = db.execute(select(LogisticsRegionPoint)).scalars().all()
    return {
        (point.normalized_client, round(float(point.latitude), 6), round(float(point.longitude), 6)): point
        for point in points
    }


def plan_changes(db: Session, rows: list[dict]) -> dict:
    current = existing_by_key(db)
    insert = 0
    update = 0
    unchanged = 0
    for row in rows:
        key = (row["normalized_client"], row["latitude"], row["longitude"])
        point = current.get(key)
        if point is None:
            insert += 1
        elif point.client_name != row["client_name"] or point.agent != row["agent"] or not point.is_active:
            update += 1
        else:
            unchanged += 1
    return {
        "existing": len(current),
        "insert": insert,
        "update": update,
        "unchanged": unchanged,
        "total_after": len(current) + insert,
    }


def apply_changes(db: Session, rows: list[dict]) -> dict:
    plan = plan_changes(db, rows)
    current = existing_by_key(db)
    for row in rows:
        key = (row["normalized_client"], row["latitude"], row["longitude"])
        point = current.get(key)
        if point is None:
            db.add(LogisticsRegionPoint(
                client_name=row["client_name"],
                normalized_client=row["normalized_client"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                agent=row["agent"],
                is_active=True,
                raw_payload={"source": "address_programme"},
            ))
            continue
        point.client_name = row["client_name"]
        point.agent = row["agent"]
        point.is_active = True
        point.raw_payload = {**(point.raw_payload or {}), "source": "address_programme"}
    db.commit()
    return plan


def print_plan(plan: dict) -> None:
    print(f"было записей:      {plan['existing']}")
    print(f"будет добавлено:   {plan['insert']}")
    print(f"будет обновлено:   {plan['update']}")
    print(f"без изменений:     {plan['unchanged']}")
    print(f"станет записей:    {plan['total_after']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Load region address programme")
    parser.add_argument("source", help="path to the address programme xlsx")
    parser.add_argument("--dry-run", action="store_true", help="only print the plan")
    args = parser.parse_args(argv)

    from backend.app.db import SessionLocal

    rows = read_region_rows(args.source)
    print(f"строк в файле после дедупликации: {len(rows)}")
    db = SessionLocal()
    try:
        plan = plan_changes(db, rows)
        print_plan(plan)
        if args.dry_run:
            print("режим --dry-run, запись не выполнялась")
            return 0
        apply_changes(db, rows)
        print("записано")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Закрыть адресную программу от случайного коммита**

Файл лежит вне репозитория, но защита нужна на случай копии внутрь
Добавить в `.gitignore` строки:

```gitignore
# Адресная программа контрагентов, клиентская база, в публичный репозиторий не идёт
Адресная_программа*.xlsx
*адресная_программа*.xlsx
```

Проверить: `git check-ignore -v Адресная_программа_контрагенты_Таш_обл_.xlsx`
Expected: строка с указанием правила из `.gitignore`

Фабрика сессий называется `SessionLocal` и определена в `backend/app/db.py:52`,
импорт в `main()` менять не нужно

- [ ] **Step 5: Запустить тесты, убедиться что проходят**

Run: `PYTHONPATH=. python -m unittest tests.test_seed_logistics_region_points -v`
Expected: PASS, все 5 тестов

- [ ] **Step 6: Проверить чтение настоящего файла без подключения к БД**

`main()` открывает боевую сессию, поэтому локально проверяется только парсер:

```bash
PYTHONPATH=. python -c "
from tools.seed_logistics_region_points import read_region_rows
rows = read_region_rows('/Users/anton/Documents/Telegram/Адресная_программа_контрагенты_Таш_обл_.xlsx')
print('строк после дедупликации:', len(rows))
print('без агента:', sum(1 for row in rows if not row['agent']))
"
```

Expected: `строк после дедупликации: 398`
Имена контрагентов не печатать, только числа

Полный прогон скрипта с `--dry-run` делается на сервере в разделе «Приёмка»

- [ ] **Step 7: Коммит**

```bash
git add tools/seed_logistics_region_points.py tests/test_seed_logistics_region_points.py .gitignore
git commit -m "feat(logistics): загрузка справочника областных точек из адресной программы

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Проверка разделения на боевых данных без отправки

**Files:**
- Create: `tools/logistics_zone_dry_run.py`
- Test: `tests/test_logistics_zone_dry_run.py` (создаётся)

**Interfaces:**
- Consumes: `build_logistics_reports(db, shipment_date)` из Task 6
- Produces:
  - `summarize(db, shipment_date) -> dict` ключи `city_rows`, `region_rows`,
    `unassigned`, `unassigned_clients`
  - CLI: `python tools/logistics_zone_dry_run.py <ISO-дата> [<ISO-дата> ...]`

Скрипт ничего не отправляет и ничего не пишет в БД

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_logistics_zone_dry_run.py`:

```python
import unittest
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, LogisticsRegionPoint, Order, OrderItem
from tools.logistics_zone_dry_run import summarize


SHIPMENT_DATE = date(2030, 1, 2)


class LogisticsZoneDryRunTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        self.db.add(LogisticsRegionPoint(
            client_name="Тест Клиент Область",
            normalized_client="тестклиентобласть",
            latitude=41.018778,
            longitude=70.083423,
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_order(self, client, coordinates):
        order = Order(
            payment_type="Наличные",
            client=client,
            address="Тестовый адрес",
            status="not_completed",
            order_date=SHIPMENT_DATE,
            raw_payload={"coordinates": coordinates},
        )
        order.items.append(OrderItem(product="Тестовый товар", quantity_blocks=1))
        self.db.add(order)
        self.db.commit()

    def test_summary_counts_rows_and_names_unassigned(self):
        self.add_order("Тест Клиент Область", "41.018778,70.083423")
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        self.add_order("Незнакомый Загород", "41.4700,69.5800")
        summary = summarize(self.db, SHIPMENT_DATE.isoformat())
        self.assertEqual(summary["city_rows"], 1)
        self.assertEqual(summary["region_rows"], 1)
        self.assertEqual(summary["unassigned"], 1)
        self.assertEqual(summary["unassigned_clients"], ["Незнакомый Загород"])

    def test_summary_reports_zeros_for_missing_zone(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        summary = summarize(self.db, SHIPMENT_DATE.isoformat())
        self.assertEqual(summary["region_rows"], 0)
        self.assertEqual(summary["unassigned"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_dry_run -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'tools.logistics_zone_dry_run'`

- [ ] **Step 3: Написать скрипт**

Создать `tools/logistics_zone_dry_run.py`:

```python
"""Report how a shipment date would split between city and region, without sending."""

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
import sys

from openpyxl import load_workbook
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.logistics_service import build_logistics_reports  # noqa: E402


def count_rows(report) -> int:
    if report is None:
        return 0
    payload, _filename = report
    workbook = load_workbook(BytesIO(payload), read_only=True)
    count = workbook["Orders"].max_row - 1
    workbook.close()
    return max(0, count)


def summarize(db: Session, shipment_date: str) -> dict:
    reports = build_logistics_reports(db, shipment_date)
    unassigned = reports.get("unassigned") or []
    return {
        "shipment_date": shipment_date,
        "city_rows": count_rows(reports.get("city")),
        "region_rows": count_rows(reports.get("region")),
        "unassigned": len(unassigned),
        "unassigned_clients": [order.client for order in unassigned],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run the logistics zone split")
    parser.add_argument("dates", nargs="+", help="shipment dates in YYYY-MM-DD")
    args = parser.parse_args(argv)

    from backend.app.db import SessionLocal

    db = SessionLocal()
    try:
        for shipment_date in args.dates:
            try:
                summary = summarize(db, shipment_date)
            except Exception as exc:
                print(f"{shipment_date}: пропущено, {exc.__class__.__name__}")
                continue
            print(f"{shipment_date}:")
            print(f"  город строк:        {summary['city_rows']}")
            print(f"  область строк:      {summary['region_rows']}")
            print(f"  вне зон заказов:    {summary['unassigned']}")
            for client in summary["unassigned_clients"]:
                print(f"    - {client}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `PYTHONPATH=. python -m unittest tests.test_logistics_zone_dry_run -v`
Expected: PASS, оба теста

- [ ] **Step 5: Прогнать весь набор тестов и компиляцию**

Run:

```bash
PYTHONPATH=. python -m compileall -q backend/app backend/migrations tools tests && PYTHONPATH=. python -m unittest discover -s tests
```

Expected: PASS, ни одного FAIL и ERROR

- [ ] **Step 6: Коммит**

```bash
git add tools/logistics_zone_dry_run.py tests/test_logistics_zone_dry_run.py
git commit -m "feat(logistics): dry-run подсчёт разделения по зонам без отправки

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Приёмка на боевых данных

Это делается после всех задач, до включения автоматической отправки
Шаги требуют доступа к боевой БД и выполняются с разрешения Антона

- [ ] Применить миграцию на сервере, проверить `alembic heads`
- [ ] Прогнать сид с `--dry-run`, сверить: строк 398, добавится 398
- [ ] Прогнать сид без флага, убедиться что в таблице 398 записей
- [ ] Прогнать `tools/logistics_zone_dry_run.py` на 3 прошедших датах отгрузки
- [ ] Показать Антону числа: город, область, вне зон, и кто именно вне зон
- [ ] Заказов вне зон больше нуля значит правим справочник или полигон
      до включения, а не после
