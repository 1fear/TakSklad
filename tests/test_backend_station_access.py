import unittest

from backend.app.settings import load_settings
from backend.app.station_access import is_warehouse_address, parse_warehouse_cidrs

WAREHOUSE_IP = "203.0.113.10"
SECOND_WAREHOUSE_IP = "198.51.100.7"
OUTSIDE_IP = "198.51.100.99"


class StationNetworkParsingTests(unittest.TestCase):
    def test_settings_read_warehouse_cidrs(self):
        settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_WAREHOUSE_CIDRS": f"{WAREHOUSE_IP}/32, {SECOND_WAREHOUSE_IP}",
        })
        self.assertEqual(settings.warehouse_cidrs, (f"{WAREHOUSE_IP}/32", SECOND_WAREHOUSE_IP))
        self.assertEqual(load_settings({"TAKSKLAD_ENV": "test"}).warehouse_cidrs, ())

    def test_unsafe_or_broken_values_disable_whole_list(self):
        networks = parse_warehouse_cidrs([f"{WAREHOUSE_IP}/32"])
        self.assertTrue(is_warehouse_address(WAREHOUSE_IP, networks))
        self.assertFalse(is_warehouse_address(OUTSIDE_IP, networks))
        self.assertFalse(is_warehouse_address("unknown", networks))
        for values in (
            ["0.0.0.0/0"],
            ["::/0"],
            ["172.18.0.0/16"],
            ["192.168.1.0/24"],
            ["127.0.0.1"],
            ["not-a-network"],
            [f"{WAREHOUSE_IP}/32", "10.0.0.0/8"],
        ):
            with self.subTest(values=values):
                self.assertEqual(parse_warehouse_cidrs(values), ())


if __name__ == "__main__":
    unittest.main()
