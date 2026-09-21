import asyncio
import threading
import unittest
from io import BytesIO
from unittest import mock
from urllib.parse import quote

import httpx
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main as main_module
from backend.app.db import get_db
from backend.app.main import app, require_admin_write_permission, require_service_token
from backend.app.models import Base


class ExcelImportEventLoopTests(unittest.TestCase):
    """Загрузка Excel не должна исполнять блокирующую работу в теле корутины.

    Из всех обработчиков приложения только эти два объявлены async, остальные
    обычным def и потому целиком уезжают в пул потоков. Разбор книги и особенно
    геокодирование строк ходят в сеть блокирующим httpx с таймаутом 10 с на адрес
    при потолке 5000 строк на файл, поэтому исполнение этой работы на потоке
    событийного цикла останавливает весь процесс, а не один запрос.

    Инвариант проверяется по идентификатору потока: он не зависит от времени и
    потому не мигает. Проверить его через отзывчивость цикла нельзя, заблокированный
    цикл не исполняет и собственные таймауты
    """

    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        def override_get_db():
            with self.SessionLocal() as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_service_token] = lambda: None
        app.dependency_overrides[require_admin_write_permission] = lambda: None

    def tearDown(self):
        app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def workbook_bytes(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Заявки"
        sheet.append(["Клиент", "Тип оплаты", "ТМЦ", "Количество заказа", "Адрес", "Дата заказа"])
        sheet.append(["Клиент 1", "Терминал", "Chapman Brown OP 20", 20, "Самовывоз", "16.07.2026"])
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        return output.getvalue()

    def call_endpoint(self, path, seen):
        content = self.workbook_bytes()
        headers = {"X-TakSklad-Filename": quote("Заказы 16.07.2026.xlsx")}

        async def scenario():
            # Тело корутины исполняется на том же потоке, что и событийный цикл,
            # поэтому этот идентификатор и есть идентификатор потока цикла
            seen["loop"] = threading.get_ident()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(path, content=content, headers=headers)

        return asyncio.run(scenario())

    def test_preview_runs_parser_and_database_work_off_the_loop_thread(self):
        seen = {}
        real_parse = main_module.parse_raw_excel_upload
        real_preview = main_module.preview_import_in_db

        def spy_parse(content, filename, *, source="web"):
            seen["parse"] = threading.get_ident()
            return real_parse(content, filename, source=source)

        def spy_preview(db, payload):
            seen["preview"] = threading.get_ident()
            return real_preview(db, payload)

        with mock.patch.object(main_module, "parse_raw_excel_upload", spy_parse), \
                mock.patch.object(main_module, "preview_import_in_db", spy_preview):
            response = self.call_endpoint("/api/v1/imports/excel/preview", seen)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(seen["parse"], seen["loop"], "разбор книги исполнился на потоке событийного цикла")
        self.assertNotEqual(seen["preview"], seen["loop"], "превью импорта исполнилось на потоке событийного цикла")

    def test_commit_runs_parser_and_database_work_off_the_loop_thread(self):
        seen = {}
        real_parse = main_module.parse_raw_excel_upload
        real_create = main_module.create_import_in_db

        def spy_parse(content, filename, *, source="web"):
            seen["parse"] = threading.get_ident()
            return real_parse(content, filename, source=source)

        def spy_create(db, payload):
            seen["create"] = threading.get_ident()
            return real_create(db, payload)

        with mock.patch.object(main_module, "parse_raw_excel_upload", spy_parse), \
                mock.patch.object(main_module, "create_import_in_db", spy_create):
            response = self.call_endpoint("/api/v1/imports/excel", seen)

        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotEqual(seen["parse"], seen["loop"], "разбор книги исполнился на потоке событийного цикла")
        self.assertNotEqual(seen["create"], seen["loop"], "запись импорта исполнилась на потоке событийного цикла")

    def test_broken_workbook_still_answers_422(self):
        # Исключение из потока пула обязано долетать до обработчика так же,
        # как оно долетало из тела корутины
        headers = {"X-TakSklad-Filename": quote("битый.xlsx")}

        async def scenario():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post("/api/v1/imports/excel/preview", content=b"not-a-workbook", headers=headers)

        response = asyncio.run(scenario())
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
