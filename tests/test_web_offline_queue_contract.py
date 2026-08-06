"""The browser offline queue and the desktop queue must agree on final conflicts.

The desktop queue drops a scan event into its durable ``blocked`` section when
the backend answers with one of ``KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES``
(``src/taksklad/backend_events.py``). The browser queue has to reach the same
verdict, otherwise one client would retry forever what the other already
recognised as hopeless, and the two operator surfaces would report different
states for the same physically scanned block.

This test pins ``NON_RETRYABLE_SCAN_CODES`` in
``frontend/src/features/warehouse/offline/errorPolicy.ts`` against the desktop
set, the same way ``tests/test_kiz_format_contract.py`` pins the KIZ format
guard.
"""

from pathlib import Path
import re
import unittest

from backend.app import scan_quantities as backend_quantities
from backend.app.csrf import CSRF_ERROR_DETAIL, ORIGIN_ERROR_DETAIL
from taksklad import scan_quantities as desktop_quantities
from taksklad.backend_events import (
    KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES,
    SCAN_DUPLICATE_ACK_CODE,
)


ROOT = Path(__file__).resolve().parents[1]
ERROR_POLICY_TS = ROOT / "frontend/src/features/warehouse/offline/errorPolicy.ts"
SCAN_QUANTITIES_TS = ROOT / "frontend/src/features/warehouse/scanQuantities.ts"


def read_ts_string_array(source: str, name: str) -> set[str]:
    block = re.search(rf"{name}\s*=\s*\[(.*?)\]", source, re.S)
    if block is None:
        raise AssertionError(f"В {ERROR_POLICY_TS.name} не найден массив {name}")
    return set(re.findall(r'"([^"]+)"', block.group(1)))


class WebOfflineQueueContractTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            ERROR_POLICY_TS.exists(),
            f"Ожидался файл {ERROR_POLICY_TS}, браузерная очередь без него не имеет политики ошибок",
        )
        self.source = ERROR_POLICY_TS.read_text(encoding="utf-8")

    def test_non_retryable_codes_match_desktop(self):
        web_codes = read_ts_string_array(self.source, "NON_RETRYABLE_SCAN_CODES")
        self.assertEqual(web_codes, set(KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES))

    def test_duplicate_ack_code_matches_desktop(self):
        match = re.search(r'DUPLICATE_SCAN_ACK_CODE\s*=\s*"([^"]+)"', self.source)
        self.assertIsNotNone(match, "В errorPolicy.ts не найден DUPLICATE_SCAN_ACK_CODE")
        self.assertEqual(match.group(1), SCAN_DUPLICATE_ACK_CODE)

    def test_duplicate_ack_is_not_listed_as_non_retryable(self):
        """A duplicate is an acknowledgement, treating it as blocked would strand a real scan."""
        web_codes = read_ts_string_array(self.source, "NON_RETRYABLE_SCAN_CODES")
        self.assertNotIn(SCAN_DUPLICATE_ACK_CODE, web_codes)

    def test_recoverable_browser_security_codes_match_backend(self):
        """A stale tab must never turn a queued scan into a final failure.

        `require_browser_request_security` rejects a stale CSRF token or a
        foreign origin with HTTP 403 before touching warehouse state. The queue
        has to keep retrying those, so the browser list must stay equal to the
        backend's own two detail codes.
        """
        expected = {CSRF_ERROR_DETAIL["code"], ORIGIN_ERROR_DETAIL["code"]}
        web_codes = read_ts_string_array(self.source, "RECOVERABLE_BROWSER_SECURITY_CODES")
        self.assertEqual(web_codes, expected)

    def test_recoverable_and_blocking_lists_do_not_overlap(self):
        recoverable = read_ts_string_array(self.source, "RECOVERABLE_BROWSER_SECURITY_CODES")
        blocking = read_ts_string_array(self.source, "NON_RETRYABLE_SCAN_CODES")
        self.assertEqual(recoverable & blocking, set())


class WebAggregateBoxContractTest(unittest.TestCase):
    """Offline block arithmetic in the browser must equal the server rule.

    A box code is worth ``AGGREGATE_BOX_BLOCK_QUANTITY`` blocks, a unit code one
    (``backend/app/scan_quantities.py``). Offline the browser has no server to
    ask, so it carries its own copy of the prefix table. If that copy drifts,
    the operator is told a position still needs blocks it already has, or that a
    position is finished when it is not.
    """

    def setUp(self):
        self.assertTrue(
            SCAN_QUANTITIES_TS.exists(),
            f"Ожидался файл {SCAN_QUANTITIES_TS}, без него офлайн-прогресс считается неверно",
        )
        self.source = SCAN_QUANTITIES_TS.read_text(encoding="utf-8")

    def test_block_quantity_matches_backend(self):
        match = re.search(r"AGGREGATE_BOX_BLOCK_QUANTITY\s*=\s*(\d+)", self.source)
        self.assertIsNotNone(match, "В scanQuantities.ts не найдена AGGREGATE_BOX_BLOCK_QUANTITY")
        self.assertEqual(int(match.group(1)), backend_quantities.AGGREGATE_BOX_BLOCK_QUANTITY)

    def test_aggregate_prefixes_match_backend_and_desktop(self):
        block = re.search(r"AGGREGATE_BOX_PRODUCT_PREFIXES[^=]*=\s*\{(.*?)\};", self.source, re.S)
        self.assertIsNotNone(block, "В scanQuantities.ts не найден AGGREGATE_BOX_PRODUCT_PREFIXES")
        web_map = dict(re.findall(r'"(\d+)"\s*:\s*"([^"]+)"', block.group(1)))
        self.assertEqual(web_map, backend_quantities.AGGREGATE_BOX_PRODUCT_PREFIXES)
        self.assertEqual(web_map, desktop_quantities.AGGREGATE_BOX_PRODUCT_PREFIXES)

    def test_every_backend_box_prefix_is_recognised_by_the_browser_table(self):
        block = re.search(r"AGGREGATE_BOX_PRODUCT_PREFIXES[^=]*=\s*\{(.*?)\};", self.source, re.S)
        web_prefixes = set(re.findall(r'"(\d+)"\s*:', block.group(1)))
        for prefix in backend_quantities.AGGREGATE_BOX_PRODUCT_PREFIXES:
            self.assertIn(prefix, web_prefixes)
            self.assertEqual(
                backend_quantities.block_quantity_for_code(f"{prefix}217ABCDEF"),
                backend_quantities.AGGREGATE_BOX_BLOCK_QUANTITY,
            )


if __name__ == "__main__":
    unittest.main()
