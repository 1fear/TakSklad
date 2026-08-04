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
