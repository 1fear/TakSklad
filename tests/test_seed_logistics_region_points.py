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
