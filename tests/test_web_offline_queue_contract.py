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

from taksklad.backend_events import (
    KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES,
    SCAN_DUPLICATE_ACK_CODE,
)


ROOT = Path(__file__).resolve().parents[1]
ERROR_POLICY_TS = ROOT / "frontend/src/features/warehouse/offline/errorPolicy.ts"


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


if __name__ == "__main__":
    unittest.main()
