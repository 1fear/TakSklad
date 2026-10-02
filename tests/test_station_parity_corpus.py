"""The station parity corpus is the single oracle of the browser station tests

`tools/generate_station_parity_corpus.py` records what the desktop program answers; the TypeScript tests under
`frontend/src/station/__tests__` read only that file. If the desktop answers differently now, the file is stale
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from taksklad.kiz_blocklist import BLOCKED_KIZ_CODES
from tools.generate_station_parity_corpus import main

STATION = Path(__file__).resolve().parents[1] / "frontend" / "src" / "station"


class StationParityCorpusTests(unittest.TestCase):
    def test_corpus_file_is_what_the_desktop_answers_now(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--check"])
        self.assertEqual(status, 0, "regenerate: PYTHONPATH=. python tools/generate_station_parity_corpus.py")
        self.assertIn("STATION_PARITY_CORPUS_OK", output.getvalue())

    def test_corpus_does_not_change_when_the_program_version_does(self):
        # the product mismatch message prints the version; a release must not turn the freshness check red
        from taksklad import desktop_scan_rules

        output = io.StringIO()
        with mock.patch.object(desktop_scan_rules, "APP_VERSION", "9.9.9"), redirect_stdout(output):
            status = main(["--check"])
        self.assertEqual(status, 0, "build_corpus must pin APP_VERSION to a fixed value")

    def test_no_raw_blocklist_code_anywhere_in_the_station_tree(self):
        # the browser bundle is public: only digests of the blocked codes may be there, in the corpus or in the code
        spellings = [(code, json.dumps(code)[1:-1]) for code in BLOCKED_KIZ_CODES]
        files = [path for path in STATION.rglob("*") if path.is_file()]
        self.assertIn("parity-corpus.json", {path.name for path in files})
        for path in files:
            data = path.read_bytes()
            for number, forms in enumerate(spellings, start=1):
                # the message names the file and the number of the code, never the code
                self.assertFalse(any(form.encode("utf-8") in data for form in forms), f"{path.name} holds blocklist code #{number}")


if __name__ == "__main__":
    unittest.main()
