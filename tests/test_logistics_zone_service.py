import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.logistics_zone_service import (
    haversine_meters,
    parse_coordinates,
    point_in_city,
)
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
        # около 500 м западнее западной вершины полигона, внутри буфера 1 км
        self.assertTrue(point_in_city(41.2800, 69.1340))

    def test_buffer_rejects_point_far_outside_polygon(self):
        # около 8 км западнее западной вершины, за пределами буфера
        self.assertFalse(point_in_city(41.2800, 69.0450))


if __name__ == "__main__":
    unittest.main()
