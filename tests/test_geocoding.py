import os
import tempfile
import unittest
from pathlib import Path

from taksklad import geocoding
from taksklad.config import YANDEX_GEOCODER_ENV_VAR


class GeocodingKeyTests(unittest.TestCase):
    def setUp(self):
        self.previous_env = os.environ.get(YANDEX_GEOCODER_ENV_VAR)
        self.previous_key_file = geocoding.YANDEX_GEOCODER_KEY_FILE

    def tearDown(self):
        if self.previous_env is None:
            os.environ.pop(YANDEX_GEOCODER_ENV_VAR, None)
        else:
            os.environ[YANDEX_GEOCODER_ENV_VAR] = self.previous_env
        geocoding.YANDEX_GEOCODER_KEY_FILE = self.previous_key_file

    def test_env_key_wins_over_file_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            key_file = Path(tmp_dir) / "yandex_geocoder_key.txt"
            key_file.write_text("file-key", encoding="utf-8")
            geocoding.YANDEX_GEOCODER_KEY_FILE = str(key_file)
            os.environ[YANDEX_GEOCODER_ENV_VAR] = "env-key"

            self.assertEqual(geocoding.load_yandex_geocoder_key(), "env-key")

    def test_file_key_is_used_without_env_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            key_file = Path(tmp_dir) / "yandex_geocoder_key.txt"
            key_file.write_text("  file-key  \n", encoding="utf-8")
            geocoding.YANDEX_GEOCODER_KEY_FILE = str(key_file)
            os.environ.pop(YANDEX_GEOCODER_ENV_VAR, None)

            self.assertEqual(geocoding.load_yandex_geocoder_key(), "file-key")

    def test_missing_key_returns_empty_value(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            geocoding.YANDEX_GEOCODER_KEY_FILE = str(Path(tmp_dir) / "missing.txt")
            os.environ.pop(YANDEX_GEOCODER_ENV_VAR, None)

            self.assertEqual(geocoding.load_yandex_geocoder_key(), "")


if __name__ == "__main__":
    unittest.main()
