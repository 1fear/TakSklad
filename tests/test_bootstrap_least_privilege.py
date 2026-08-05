import unittest

from backend.app.auth_identities import (
    DESKTOP_BOOTSTRAP_SCOPES,
    DESKTOP_RUNTIME_SCOPES,
)
from backend.app.access_policy import route_policy


MUTATING_WAREHOUSE_ROUTES = (
    ("POST", "/api/v1/scans"),
    ("POST", "/api/v1/scans/undo"),
    ("POST", "/api/v1/kiz/release"),
    ("POST", "/api/v1/orders/{order_id}/complete"),
    ("POST", "/api/v1/returns/{order_id}"),
    ("POST", "/api/v1/sync/sources"),
)


class BootstrapLeastPrivilegeTests(unittest.TestCase):
    def test_bootstrap_scopes_carry_no_warehouse_mutation(self):
        # Аудит 05.08.2026: анонимный /auth/desktop-bootstrap выдавал весь
        # desktop-набор, то есть посторонний из интернета за один запрос
        # получал право списывать КИЗы, откатывать сканы и завершать заказы
        forbidden = {
            "scans:create",
            "scans:undo",
            "kiz:release",
            "orders:complete",
            "returns:write",
            "sync:run",
            "imports:create",
        }
        self.assertEqual(DESKTOP_BOOTSTRAP_SCOPES & forbidden, frozenset())

    def test_bootstrap_scopes_are_a_strict_subset_of_runtime(self):
        self.assertTrue(DESKTOP_BOOTSTRAP_SCOPES < DESKTOP_RUNTIME_SCOPES)

    def test_bootstrap_scopes_still_allow_canary_and_ack(self):
        # Без этих прав десктоп не пройдёт проверку связи и не подтвердит pairing
        for method, path in (
            ("GET", "/api/v1/returns/auth-canary/desktop"),
            ("POST", "/api/v1/auth/desktop-pairing/{pairing_id}/ack"),
        ):
            with self.subTest(route=path):
                policy = route_policy(method, path)
                self.assertIsNotNone(policy, f"маршрут {path} отсутствует в политике")
                self.assertIn(policy.service_scope, DESKTOP_BOOTSTRAP_SCOPES)

    def test_every_mutating_warehouse_route_is_out_of_reach_before_ack(self):
        missing = []
        for method, path in MUTATING_WAREHOUSE_ROUTES:
            policy = route_policy(method, path)
            if policy is None:
                missing.append(path)
                continue
            with self.subTest(route=path):
                self.assertNotIn(
                    policy.service_scope,
                    DESKTOP_BOOTSTRAP_SCOPES,
                    f"{path} доступен неподтверждённому принципалу",
                )
        self.assertEqual(missing, [], f"маршруты отсутствуют в политике: {missing}")


if __name__ == "__main__":
    unittest.main()
