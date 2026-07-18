import json
import logging
import socket
import ssl
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from taksklad import desktop_diagnostics
from taksklad.config import APP_VERSION
from taksklad.logging_setup import LOG_FORMAT, SecretRedactingFormatter
from taksklad.secret_store import (
    TELEGRAM_BOT_TOKEN_SECRET,
    MemorySecretStore,
    reset_secret_store_for_tests,
    set_secret_store_for_tests,
)


class DesktopDiagnosticsTests(unittest.TestCase):
    def tearDown(self):
        reset_secret_store_for_tests()

    def test_refresh_summary_contains_backend_queues_without_google_fields(self):
        with (
            mock.patch.object(desktop_diagnostics, "load_pending_prints", return_value=[]),
            mock.patch.object(
                desktop_diagnostics,
                "load_pending_backend_events",
                return_value=[{"type": "scan", "attempts": 1, "last_error": "timeout"}],
            ),
            mock.patch.object(desktop_diagnostics, "load_pending_telegram", return_value=[]),
        ):
            summary = desktop_diagnostics.build_refresh_diagnostic_summary(
                [{"Дата отгрузки": "01.06.2026", "Клиент": "Client"}],
                {"KIZ"},
                sync_result={"primary_source": "backend", "backend_only_refresh": True},
            )
            text = desktop_diagnostics.format_refresh_diagnostic_summary(summary)

        self.assertEqual(summary["primary_source"], "backend")
        self.assertEqual(summary["pending_backend_events"], 1)
        self.assertNotIn("google", text.lower())
        self.assertNotIn("pending_saves", summary)

    def test_queue_summary_marks_backend_conflict_blocked(self):
        now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        with (
            mock.patch.object(desktop_diagnostics, "load_pending_prints", return_value=[]),
            mock.patch.object(
                desktop_diagnostics,
                "load_pending_backend_events",
                return_value=[{
                    "type": "scan",
                    "created_at": "2026-07-03T10:00:00+00:00",
                    "attempts": 1,
                    "last_error": "Backend HTTP 409: conflict",
                }],
            ),
            mock.patch.object(desktop_diagnostics, "load_pending_telegram", return_value=[]),
        ):
            summary = desktop_diagnostics.build_sync_queue_summary(now=now)

        self.assertEqual(summary["queues"]["backend_scans"]["state"], "blocked")
        self.assertFalse(summary["retry_enabled"])
        self.assertNotIn("google_saves", summary["queues"])

    def test_missing_backend_blocks_retry(self):
        with (
            mock.patch.object(desktop_diagnostics, "load_pending_prints", return_value=[]),
            mock.patch.object(
                desktop_diagnostics,
                "load_pending_backend_events",
                return_value=[{"type": "scan", "payload": {}}],
            ),
            mock.patch.object(desktop_diagnostics, "load_pending_telegram", return_value=[]),
        ):
            summary = desktop_diagnostics.build_sync_queue_summary(backend_available=False)

        self.assertFalse(summary["retry_enabled"])
        self.assertIn("Backend", summary["retry_blocker"])

    def test_diagnostic_bundle_manifest_redacts_payloads_and_log_lines(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            app_log = tmp_path / "TakSklad.log"
            update_log = tmp_path / "TakSklad_update.log"
            app_log.write_text(
                "ERROR Authorization: Bearer SECRET TEST-SECRET-KIZ-1 SECRET CLIENT\n",
                encoding="utf-8",
            )
            update_log.write_text("WARNING timeout token=SECRET\n", encoding="utf-8")

            with (
                mock.patch.object(desktop_diagnostics, "LOG_FILE", str(app_log)),
                mock.patch.object(desktop_diagnostics, "UPDATE_LOG_FILE", str(update_log)),
                mock.patch.object(
                    desktop_diagnostics,
                    "build_startup_self_check",
                    return_value={
                        "version": "2.0.25",
                        "build_label": "MVP 2.0",
                        "workstation_id": "abc123",
                        "app_dir": str(tmp_path / "Users" / "operator" / "TakSklad"),
                        "log_file": str(tmp_path / "Users" / "operator" / "TakSklad.log"),
                        "backend_token": "yes",
                        "telegram_token": "yes",
                        "secret_sample": "Authorization: Bearer SECRET",
                    },
                ),
                mock.patch.object(
                    desktop_diagnostics,
                    "build_version_update_status",
                    return_value={"state": "current", "workstation_id": "abc123"},
                ),
                mock.patch.object(desktop_diagnostics, "load_pending_prints", return_value=[]),
                mock.patch.object(
                    desktop_diagnostics,
                    "load_pending_backend_events",
                    return_value=[{
                        "type": "scan",
                        "created_at": "2026-07-03T09:00:00+00:00",
                        "payload": {
                            "client": "SECRET CLIENT",
                            "address": "SECRET ADDRESS",
                            "code": "TEST-SECRET-KIZ-1",
                        },
                        "last_error": "timeout token=SECRET",
                    }],
                ),
                mock.patch.object(desktop_diagnostics, "load_pending_telegram", return_value=[]),
            ):
                path, manifest = desktop_diagnostics.write_diagnostic_bundle(
                    output_dir=tmp_dir,
                    probes=[{"name": "backend_health", "status": "failed", "class": "auth_rejected"}],
                )

            saved_text = Path(path).read_text(encoding="utf-8")
            saved = json.loads(saved_text)
            serialized = json.dumps(saved, ensure_ascii=False)

        self.assertEqual(saved["app"]["version"], APP_VERSION)
        self.assertEqual(saved["app"]["build_label"], "MVP 2.0")
        self.assertEqual(saved["startup_self_check"]["app_dir"], "[redacted-path]")
        self.assertEqual(saved["startup_self_check"]["log_file"], "[redacted-path]")
        self.assertEqual(saved["queue_summary"]["queues"]["backend_scans"]["count"], 1)
        self.assertEqual(saved["probes"][0]["class"], "auth_rejected")
        self.assertEqual(saved["log_tail_classes"]["app_log"]["classes"]["error"], 1)
        self.assertEqual(saved["log_tail_classes"]["update_log"]["classes"]["warning"], 1)
        for secret in (
            "SECRET CLIENT",
            "SECRET ADDRESS",
            "TEST-SECRET-KIZ-1",
            "Authorization: Bearer SECRET",
            "token=SECRET",
            "operator",
            "TakSklad.log",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, serialized)
                self.assertNotIn(secret, saved_text)

    def test_diagnostic_bundle_redacts_registered_arbitrary_sentinel(self):
        sentinel = "TAKSKLAD" + "_SYNTHETIC_" + "SECRET_SENTINEL_V1"
        set_secret_store_for_tests(MemorySecretStore({TELEGRAM_BOT_TOKEN_SECRET: sentinel}))
        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.object(
                desktop_diagnostics,
                "run_readonly_diagnostic_probes",
                return_value=[{"name": "synthetic", "status": "failed", "detail": sentinel}],
            ):
                path, manifest = desktop_diagnostics.write_diagnostic_bundle(output_dir=tmp_dir)

            serialized = json.dumps(manifest, ensure_ascii=False)
            archive_bytes = Path(path).read_bytes()

        self.assertNotIn(sentinel, serialized)
        self.assertNotIn(sentinel.encode("utf-8"), archive_bytes)
        self.assertIn("[redacted-secret]", serialized)

    def test_log_formatter_redacts_registered_arbitrary_sentinel(self):
        sentinel = "TAKSKLAD" + "_SYNTHETIC_" + "SECRET_SENTINEL_V1"
        set_secret_store_for_tests(MemorySecretStore({TELEGRAM_BOT_TOKEN_SECRET: sentinel}))
        record = logging.LogRecord(
            name="synthetic",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="synthetic failure %s",
            args=(sentinel,),
            exc_info=None,
        )
        formatted = SecretRedactingFormatter(LOG_FORMAT).format(record)
        self.assertNotIn(sentinel, formatted)
        self.assertIn("[redacted-secret]", formatted)

    def test_probe_failure_class_matrix(self):
        self.assertEqual(desktop_diagnostics.classify_probe_exception(socket.gaierror("getaddrinfo failed")), "dns")
        self.assertEqual(desktop_diagnostics.classify_probe_exception(ssl.SSLError("certificate verify failed")), "tls")
        self.assertEqual(
            desktop_diagnostics.classify_probe_exception(
                urllib.error.HTTPError("https://api.example", 403, "Forbidden", {}, None)
            ),
            "auth_rejected",
        )
        self.assertEqual(
            desktop_diagnostics.classify_probe_exception(
                urllib.error.HTTPError("https://api.example", 503, "Unavailable", {}, None)
            ),
            "backend_unavailable",
        )
        self.assertEqual(desktop_diagnostics.classify_probe_exception(RuntimeError("unexpected boom")), "unknown")

if __name__ == "__main__":
    unittest.main()
