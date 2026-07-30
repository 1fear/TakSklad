import logging
import unittest
from unittest import mock

from backend.app import payment_policy
from backend.app.payment_policy import resolve_payment_policy
from backend.app.reports_service import payment_group
from backend.app.skladbot_contracts import normalize_payment_type


PAYMENT_POLICY_CASES = (
    ("Терминал", "terminal", "terminal", "terminal", False),
    ("терминал", "terminal", "terminal", "terminal", False),
    ("ТЕРМИНАЛ", "terminal", "terminal", "terminal", False),
    ("  Терминал  ", "terminal", "terminal", "terminal", False),
    ("Тёрминал", "terminal", "terminal", "terminal", False),
    ("Перечисление", "transfer", "transfer", "transfer", False),
    ("перечисление", "transfer", "transfer", "transfer", False),
    ("ПЕРЕЧИСЛЕНИЕ", "transfer", "transfer", "transfer", False),
    ("безнал", "transfer", "transfer", "transfer", False),
    ("Безналичный расчёт", "transfer", "transfer", "transfer", False),
    ("transfer", "transfer", "unknown", "transfer", True),
    ("terminal", "terminal", "unknown", "terminal", True),
    ("терминал/перечисление", "terminal", "terminal", "terminal", False),
    ("перечисление (терминал)", "terminal", "terminal", "terminal", False),
    ("оплата: перечисление", "transfer", "transfer", "transfer", False),
    ("", "unknown", "unknown", "unknown", False),
    ("   ", "unknown", "unknown", "unknown", False),
    (None, "unknown", "unknown", "unknown", False),
    ("нал", "unknown", "unknown", "unknown", False),
    ("карта", "unknown", "unknown", "unknown", False),
)


class PaymentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.shadow_registry = payment_policy._PaymentPolicyShadowRegistry()
        self.shadow_patch = mock.patch.object(
            payment_policy,
            "_SHADOW_OBSERVATIONS",
            self.shadow_registry,
        )
        self.shadow_patch.start()

    def tearDown(self):
        self.shadow_patch.stop()

    def test_legacy_and_canonical_payment_policy_table(self):
        for value, expected_reports, expected_skladbot, expected_canonical, expected_divergent in PAYMENT_POLICY_CASES:
            with self.subTest(value=value):
                self.assertEqual(payment_group(value), expected_reports)
                self.assertEqual(normalize_payment_type(value), expected_skladbot)
                resolved = resolve_payment_policy(value)
                self.assertEqual(resolved.legacy_reports, expected_reports)
                self.assertEqual(resolved.legacy_skladbot, expected_skladbot)
                self.assertEqual(resolved.canonical, expected_canonical)
                self.assertEqual(resolved.divergent, expected_divergent)

    def test_shadow_observation_omits_full_client_value(self):
        client_value = "customer-payment-secret terminal"
        with self.assertLogs("backend.app.payment_policy", logging.INFO) as captured:
            resolved = resolve_payment_policy(client_value)

        self.assertTrue(resolved.divergent)
        rendered = "\n".join(captured.output)
        self.assertIn("event=payment_policy_shadow", rendered)
        self.assertIn("canonical=terminal", rendered)
        self.assertIn("legacy_reports=terminal", rendered)
        self.assertIn("legacy_skladbot=unknown", rendered)
        self.assertNotIn(client_value, rendered)
        self.assertNotIn("customer-payment-secret", rendered)

    def test_shadow_observation_deduplicates_repeated_classification(self):
        calls = 25
        classification = ("transfer", "transfer", "unknown")

        with self.assertLogs("backend.app.payment_policy", logging.INFO) as captured:
            for _ in range(calls):
                resolve_payment_policy("transfer")

        shadow_records = [
            record
            for record in captured.output
            if "event=payment_policy_shadow" in record
        ]
        self.assertEqual(len(shadow_records), 1)
        self.assertIn("observations=1", shadow_records[0])
        self.assertEqual(self.shadow_registry.count(classification), calls)


if __name__ == "__main__":
    unittest.main()
