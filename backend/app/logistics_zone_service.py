"""Decides whether a logistics order belongs to Tashkent city or to the region."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LogisticsRegionPoint


# Приближённая административная граница Ташкента, пары (широта, долгота)
# Восточная и южная стороны расширены 05.08.2026 по боевой сверке: заказы
# Городка Тракторостроителей, Тукайтепа, Богдорчилик и Янгихаётского района
# имеют городские адреса, но лежали за прежней границей и выпадали из обоих
# отчётов. Проверено, что Чирчик, Ангрен, Олмалик, Зангиата и Пскент остаются
# снаружи
TASHKENT_CITY_POLYGON = (
    (41.3900, 69.2400),
    (41.3800, 69.3100),
    (41.3760, 69.3700),
    (41.3680, 69.4120),
    (41.3300, 69.4320),
    (41.2900, 69.4450),
    (41.2480, 69.4480),
    (41.2250, 69.3800),
    (41.1950, 69.2900),
    (41.1880, 69.2050),
    (41.2100, 69.1750),
    (41.2400, 69.1600),
    (41.2800, 69.1400),
    (41.3200, 69.1500),
    (41.3600, 69.1900),
)

# Запас наружу от границы: ошибка в сторону города дешевле выпавшего заказа
CITY_BUFFER_METERS = 1000.0

_COORDINATE_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_KEY_STRIP_RE = re.compile(r"[^0-9a-zа-я]+")


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


def normalize_client_key(value) -> str:
    """Same normalisation as client_points_service.point_key, kept dependency-free."""
    text = str(value or "").strip().casefold().replace("ё", "е")
    return _KEY_STRIP_RE.sub("", text)


@dataclass(frozen=True)
class RegionPoint:
    client_name: str
    normalized_client: str
    latitude: float
    longitude: float

    @classmethod
    def build(cls, client_name, latitude, longitude) -> "RegionPoint":
        return cls(
            client_name=str(client_name or ""),
            normalized_client=normalize_client_key(client_name),
            latitude=float(latitude),
            longitude=float(longitude),
        )


class RegionIndex:
    """Directory of region delivery points, matched by exact normalised name only."""

    def __init__(self, points):
        self._points = tuple(points)
        self._by_key = {}
        for point in self._points:
            self._by_key.setdefault(point.normalized_client, point)

    def __len__(self):
        return len(self._points)

    def find(self, client_name) -> RegionPoint | None:
        return self._by_key.get(normalize_client_key(client_name))


def load_region_index(db: Session) -> RegionIndex:
    points = db.execute(
        select(LogisticsRegionPoint).where(LogisticsRegionPoint.is_active.is_(True))
    ).scalars().all()
    return RegionIndex([
        RegionPoint.build(point.client_name, point.latitude, point.longitude)
        for point in points
    ])


ZONE_CITY = "city"
ZONE_REGION = "region"
# Классификатор эту зону больше не возвращает, см. classify_order. Константа и
# её обработка в logistics_service оставлены как страховка на случай возврата
# к правилу «неизвестный клиент за городом требует ручного разбора»
ZONE_UNASSIGNED = "unassigned"


def classify_order(client_name, coordinates_value, index: RegionIndex) -> str:
    """Rule order matters: the directory wins, geography decides everyone else.

    Совпадение со справочником только по точному имени. Прежние догадки, точка
    в 150 метрах и 70% общих значимых слов, уводили в область городские заказы:
    по сверке 06-10.08.2026 так уехали семь чужих, чаще всего городской филиал
    подтягивался за областным однофамильцем из справочника
    """
    if index.find(client_name) is not None:
        return ZONE_REGION
    point = parse_coordinates(coordinates_value)
    if point is None:
        return ZONE_CITY
    if point_in_city(*point):
        return ZONE_CITY
    # Неизвестный клиент за городской границей едет в область, а не выпадает
    return ZONE_REGION
