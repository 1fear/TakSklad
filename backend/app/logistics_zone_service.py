"""Decides whether a logistics order belongs to Tashkent city or to the region."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LogisticsRegionPoint


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

# Совпадение по координатам: тот же магазин, имя написано иначе
REGION_MATCH_METERS = 150.0

# Доля общих значимых токенов от более короткого имени
FUZZY_NAME_THRESHOLD = 0.7

LEGAL_FORM_TOKENS = frozenset({
    "mchj", "ytt", "xk", "ooo", "chp", "sp",
    "сп", "чп", "ооо", "мчж", "ытт", "хк",
})

_COORDINATE_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-zа-я]+")
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
