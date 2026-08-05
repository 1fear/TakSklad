"""Сквозной HTTP-путь подключения рабочего места: bootstrap, канарейка, ack.

Аудит 06.08.2026: 5583481 сузил анонимный bootstrap до returns:read, но канарейка
требовала точного совпадения со всем набором DESKTOP_RUNTIME_SCOPES, а клиент 2.0.54
дергает её ДО ack. Ни один тест этого не поймал: один проверял только объявленную
политику маршрута, другой подменял канарейку фейком с 204.

Здесь всё по-настоящему: реальное приложение, реальные токены, никаких подмен
аутентификации, потому что дефект жил именно в обработчике, а не в политике.
"""

import dataclasses
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth_identities import DESKTOP_BOOTSTRAP_SCOPES, DESKTOP_RUNTIME_SCOPES
from backend.app.db import get_db
from backend.app import main as main_module
from backend.app.main import app
from backend.app.models import Base, ServicePrincipal

CANARY_PATH = "/api/v1/returns/auth-canary/desktop"


class DesktopBootstrapHttpContractTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        # Pairing отказывает при коротком pepper, боевое значение в тест не тащим
        self.pepper_patch = mock.patch.object(
            main_module,
            "settings",
            dataclasses.replace(
                main_module.settings,
                web_session_secret="x" * 64,
                identity_auth_enabled=True,
            ),
        )
        self.pepper_patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.pepper_patch.stop()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def bootstrap(self):
        response = self.client.post(
            "/api/v1/auth/desktop-bootstrap",
            json={"desktop_version": "2.0.54"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        return payload["credential"], payload["principal_identifier"], payload["pairing_id"]

    def canary(self, token, identifier):
        return self.client.get(
            CANARY_PATH,
            headers={"Authorization": f"Bearer {token}", "X-TakSklad-Canary-Identifier": identifier},
        )

    def test_bootstrap_canary_ack_is_one_working_path(self):
        token, identifier, pairing_id = self.bootstrap()

        # Именно этот вызов идёт в клиенте ДО ack и именно он отдавал 403
        before_ack = self.canary(token, identifier)
        self.assertEqual(before_ack.status_code, 204, before_ack.text)

        ack = self.client.post(
            f"/api/v1/auth/desktop-pairing/{pairing_id}/ack",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(ack.status_code, 200, ack.text)

        after_ack = self.canary(token, identifier)
        self.assertEqual(after_ack.status_code, 204, after_ack.text)

        with self.SessionLocal() as db:
            principal = db.execute(
                select(ServicePrincipal).where(ServicePrincipal.identifier == identifier)
            ).scalar_one()
            self.assertEqual(frozenset(principal.scopes), DESKTOP_RUNTIME_SCOPES)

    def test_canary_accepts_only_the_two_known_scope_sets(self):
        token, identifier, _pairing_id = self.bootstrap()
        with self.SessionLocal() as db:
            principal = db.execute(
                select(ServicePrincipal).where(ServicePrincipal.identifier == identifier)
            ).scalar_one()
            self.assertEqual(frozenset(principal.scopes), DESKTOP_BOOTSTRAP_SCOPES)
            # Третий, произвольный набор канарейка обязана отвергнуть, иначе
            # проверка перестаёт что-либо гарантировать
            principal.scopes = sorted(DESKTOP_RUNTIME_SCOPES - {"kiz:release"})
            db.commit()

        drifted = self.canary(token, identifier)
        self.assertEqual(drifted.status_code, 403, drifted.text)

    def test_station_with_outdated_scope_set_can_rotate_through_bootstrap(self):
        """Станция со старым набором получает 403 и обязана суметь переподключиться."""
        token, identifier, _pairing_id = self.bootstrap()
        with self.SessionLocal() as db:
            principal = db.execute(
                select(ServicePrincipal).where(ServicePrincipal.identifier == identifier)
            ).scalar_one()
            principal.scopes = sorted(DESKTOP_RUNTIME_SCOPES - {"kiz:release"})
            db.commit()

        self.assertEqual(self.canary(token, identifier).status_code, 403)

        # Ровно это делает клиент после 403: берёт новый анонимный credential
        fresh_token, fresh_identifier, fresh_pairing = self.bootstrap()
        self.assertEqual(self.canary(fresh_token, fresh_identifier).status_code, 204)
        ack = self.client.post(
            f"/api/v1/auth/desktop-pairing/{fresh_pairing}/ack",
            headers={"Authorization": f"Bearer {fresh_token}"},
        )
        self.assertEqual(ack.status_code, 200, ack.text)


if __name__ == "__main__":
    unittest.main()
