import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.logistics_zone_service import (
    ZONE_CITY,
    ZONE_REGION,
    ZONE_UNASSIGNED,
    RegionIndex,
    RegionPoint,
    classify_order,
    haversine_meters,
    load_region_index,
    name_tokens,
    normalize_client_key,
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

    def test_real_city_addresses_from_production_are_inside(self):
        # Боевая сверка 05.08.2026: адреса городские, но лежали за прежней
        # границей и выпадали из обоих отчётов
        for name, latitude, longitude in (
            ("Городок Тракторостроителей 17", 41.362903, 69.3932466),
            ("Тукайтепа, Нурли Замин", 41.3334253477, 69.4135289406),
            ("Богдорчилик 49", 41.255776, 69.437718),
            ("Городок Тракторостроителей 4", 41.3595837621, 69.3895239528),
            ("Тукайтепа, Фаровон хаёт", 41.33372585645, 69.413410809781),
            ("Янгихаётский район, Йулдош 6А", 41.196425, 69.208234),
        ):
            with self.subTest(point=name):
                self.assertTrue(point_in_city(latitude, longitude))

    def test_expanded_boundary_still_excludes_region_towns(self):
        for name, latitude, longitude in (
            ("Пскент", 40.9000, 69.3500),
            ("Зангиата", 41.1900, 69.1300),
            ("Чирчик", 41.4700, 69.5800),
        ):
            with self.subTest(point=name):
                self.assertFalse(point_in_city(latitude, longitude))

    def test_buffer_admits_point_just_outside_polygon(self):
        # около 500 м западнее западной вершины полигона, внутри буфера 1 км
        self.assertTrue(point_in_city(41.2800, 69.1340))

    def test_buffer_rejects_point_far_outside_polygon(self):
        # около 8 км западнее западной вершины, за пределами буфера
        self.assertFalse(point_in_city(41.2800, 69.0450))


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


if __name__ == "__main__":
    unittest.main()
