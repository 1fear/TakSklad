import unittest

from taksklad import geocoding
from taksklad.secret_store import (
    GEOCODER_API_KEY_SECRET,
    MemorySecretStore,
    reset_secret_store_for_tests,
    set_secret_store_for_tests,
)


class GeocodingKeyTests(unittest.TestCase):
    def tearDown(self):
        reset_secret_store_for_tests()

    def test_secure_store_key_is_used(self):
        set_secret_store_for_tests(MemorySecretStore({GEOCODER_API_KEY_SECRET: "synthetic-key"}))
        self.assertEqual(geocoding.load_yandex_geocoder_key(), "synthetic-key")

    def test_missing_secure_store_key_returns_empty_value(self):
        set_secret_store_for_tests(MemorySecretStore())
        self.assertEqual(geocoding.load_yandex_geocoder_key(), "")


if __name__ == "__main__":
    unittest.main()
