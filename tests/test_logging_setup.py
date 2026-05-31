import logging
import tempfile
import unittest
from pathlib import Path

from taksklad.logging_setup import configure_app_logging


class LoggingSetupTests(unittest.TestCase):
    def tearDown(self):
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if getattr(handler, "_taksklad_test_handler", False):
                root_logger.removeHandler(handler)
                handler.close()

    def test_configure_app_logging_is_idempotent_for_same_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_file = str(Path(tmp_dir) / "TakSklad.log")

            first = configure_app_logging(log_file, 1024, 2)
            first._taksklad_test_handler = True
            second = configure_app_logging(log_file, 1024, 2)

            self.assertIs(first, second)

    def test_configure_app_logging_rotates_large_log(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "TakSklad.log"
            handler = configure_app_logging(str(log_path), 120, 2)
            handler._taksklad_test_handler = True

            for index in range(20):
                record = logging.LogRecord(
                    "taksklad-test",
                    logging.INFO,
                    __file__,
                    1,
                    "line %s %s",
                    (index, "x" * 40),
                    None,
                )
                handler.emit(record)
            handler.flush()

            self.assertTrue(log_path.exists())
            self.assertTrue(log_path.with_suffix(".log.1").exists())


if __name__ == "__main__":
    unittest.main()
