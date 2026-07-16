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
from taksklad.secret_store import (
    GOOGLE_CREDENTIALS_SECRET,
    TELEGRAM_BOT_TOKEN_SECRET,
    MemorySecretStore,
    reset_secret_store_for_tests,
    set_secret_store_for_tests,
)
from taksklad.logging_setup import LOG_FORMAT, SecretRedactingFormatter


class DesktopDiagnosticsTests(unittest.TestCase):
    def tearDown(self):
        reset_secret_store_for_tests()

    def test_refresh_diagnostic_summary_uses_counts_without_payload_values(self):
        orders = [
            {
                "Дата отгрузки": "01.06.2026",
                "Клиент": "SECRET CLIENT",
                "Тип оплаты": "Терминал",
                "Адрес": "SECRET ADDRESS",
                "Товары": "Chapman Brown OP 20",
                "Отсканированные коды": "TEST-SECRET-CODE-1",
            },
            {
                "Дата отгрузки": "02.06.2026",
                "Клиент": "SECRET CLIENT",
                "Тип оплаты": "Терминал",
                "Адрес": "SECRET ADDRESS",
                "Товары": "Chapman Red SSL 20",
            },
        ]
        sync_result = {
            "synced": 1,
            "failed": 0,
            "remaining": 2,
            "primary_source": "google_emergency_fallback",
            "backend_only_refresh": True,
            "emergency_google_fallback": True,
            "backend": {"enabled": True, "synced": 1, "failed": 1, "remaining": 3},
            "google_sheets_pending": {
                "status": "completed_with_errors",
                "synced": 2,
                "failed": 1,
                "remaining": 4,
                "error": "Bearer SECRET TOKEN",
            },
            "skladbot": {"enabled": True, "matched": 4, "not_found": 5, "multiple": 1, "errors": 0},
        }

        with (
            mock.patch.object(desktop_diagnostics, "load_pending_saves", return_value=[{}]),
            mock.patch.object(desktop_diagnostics, "load_pending_prints", return_value=[{}, {}]),
            mock.patch.object(
                desktop_diagnostics,
                "load_pending_backend_events",
                return_value=[
                    {
                        "type": "scan",
                        "attempts": 0,
                        "last_error": "",
                        "payload": {"code": "TEST-SECRET-CODE-1"},
                    },
                    {
                        "type": "scan",
                        "attempts": 2,
                        "last_error": "timeout with SECRET TOKEN",
                        "payload": {"code": "TEST-SECRET-CODE-2"},
                    },
                    {
                        "type": "order_complete",
                        "attempts": 1,
                        "last_error": "Backend HTTP 504",
                        "payload": {"order_id": "secret-order"},
                    },
                    {
                        "type": "unexpected",
                        "attempts": "3",
                        "last_error": "",
                        "payload": {"Authorization": "Bearer secret"},
                    },
                ],
            ),
            mock.patch.object(desktop_diagnostics, "load_pending_telegram", return_value=[]),
        ):
            summary = desktop_diagnostics.build_refresh_diagnostic_summary(
                orders,
                {"TEST-SECRET-CODE-1", "TEST-SECRET-CODE-2"},
                sync_result=sync_result,
                source="backend",
            )
            text = desktop_diagnostics.format_refresh_diagnostic_summary(summary)

        self.assertEqual(summary["source"], "google_emergency_fallback")
        self.assertEqual(summary["primary_source"], "google_emergency_fallback")
        self.assertTrue(summary["backend_only_refresh"])
        self.assertTrue(summary["emergency_google_fallback"])
        self.assertEqual(summary["orders"], 2)
        self.assertEqual(summary["groups"], 1)
        self.assertEqual(summary["order_dates"], 2)
        self.assertEqual(summary["known_codes"], 2)
        self.assertEqual(summary["pending_prints"], 2)
        self.assertEqual(summary["pending_backend_events"], 4)
        self.assertEqual(summary["pending_backend_scan_events"], 2)
        self.assertEqual(summary["pending_backend_order_complete_events"], 1)
        self.assertEqual(summary["pending_backend_other_events"], 1)
        self.assertEqual(summary["pending_backend_failed_events"], 2)
        self.assertEqual(summary["pending_backend_attempted_events"], 3)
        self.assertEqual(summary["pending_backend_max_attempts"], 3)
        self.assertEqual(summary["google_mirror_status"], "completed_with_errors")
        self.assertEqual(summary["google_mirror_synced_exports"], 2)
        self.assertEqual(summary["google_mirror_failed_exports"], 1)
        self.assertEqual(summary["google_mirror_pending_exports"], 4)
        self.assertEqual(summary["skladbot_not_found"], 5)
        self.assertIn("source=google_emergency_fallback", text)
        self.assertIn("backend_only_refresh=True", text)
        self.assertIn("emergency_google_fallback=True", text)
        self.assertIn("google_mirror_pending_exports=4", text)
        self.assertNotIn("SECRET CLIENT", text)
        self.assertNotIn("SECRET ADDRESS", text)
        self.assertNotIn("TEST-SECRET-CODE-1", text)
        self.assertNotIn("TEST-SECRET-CODE-2", text)
        self.assertNotIn("SECRET TOKEN", text)
        self.assertNotIn("secret-order", text)
        self.assertNotIn("Authorization", text)

    def test_sync_queue_summary_redacts_payloads_and_marks_conflict_blocked(self):
        now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        sync_result = {
            "backend": {
                "blocked": 1,
                "blocked_events": [
                    {
                        "type": "scan",
                        "created_at": "2026-07-03T10:00:00+00:00",
                        "attempts": 1,
                        "last_error": "Backend HTTP 409: code already scanned",
                        "payload": {
                            "code": "TEST-SECRET-KIZ-1",
                            "order_item_id": "secret-item",
                            "Authorization": "Bearer secret",
                        },
                    }
                ],
            }
        }

        with (
            mock.patch.object(
                desktop_diagnostics,
                "load_pending_saves",
                return_value=[
                    {
                        "created_at": "2026-07-03 09:00:00",
                        "last_error": "quota token SECRET",
                        "order": {"Клиент": "SECRET CLIENT", "Адрес": "SECRET ADDRESS"},
                        "codes": ["TEST-SECRET-KIZ-1"],
                    }
                ],
            ),
            mock.patch.object(
                desktop_diagnostics,
                "load_pending_prints",
                return_value=[{"created_at": "2026-07-03 11:00:00", "address": "SECRET ADDRESS"}],
            ),
            mock.patch.object(
                desktop_diagnostics,
                "load_pending_backend_events",
                return_value=[
                    {
                        "type": "scan",
                        "created_at": "2026-07-03T10:00:00+00:00",
                        "attempts": 2,
                        "last_error": "Backend HTTP 409: conflict",
                        "payload": {"code": "TEST-SECRET-KIZ-2", "order_item_id": "secret-item"},
                    },
                    {
                        "type": "order_complete",
                        "created_at": "2026-07-03T11:30:00+00:00",
                        "attempts": 1,
                        "last_error": "timeout",
                        "payload": {"order_id": "secret-order"},
                    },
                ],
            ),
            mock.patch.object(
                desktop_diagnostics,
                "load_pending_telegram",
                return_value=[
                    {
                        "created_at": "2026-07-03 11:45:00",
                        "attempts": 3,
                        "caption": "SECRET CLIENT",
                        "path": "/tmp/secret.xlsx",
                    }
                ],
            ),
        ):
            summary = desktop_diagnostics.build_sync_queue_summary(
                sync_result=sync_result,
                now=now,
                google_available=True,
                backend_available=True,
            )
            text = desktop_diagnostics.format_sync_queue_summary(summary)

        self.assertEqual(summary["queues"]["google_saves"]["count"], 1)
        self.assertEqual(summary["queues"]["backend_scans"]["count"], 1)
        self.assertEqual(summary["queues"]["backend_completes"]["count"], 1)
        self.assertEqual(summary["queues"]["prints"]["count"], 1)
        self.assertEqual(summary["queues"]["telegram"]["count"], 1)
        self.assertEqual(summary["queues"]["backend_scans"]["blocked"], 2)
        self.assertEqual(summary["queues"]["backend_scans"]["state"], "blocked")
        self.assertFalse(summary["retry_enabled"])
        self.assertIn("конфликт backend 409", summary["retry_blocker"].lower())
        self.assertIn("Backend сканы: 1", text)
        self.assertIn("требуют проверки=2", text)
        self.assertIn("классы=conflict", text)
        for secret in (
            "SECRET CLIENT",
            "SECRET ADDRESS",
            "TEST-SECRET-KIZ-1",
            "TEST-SECRET-KIZ-2",
            "secret-item",
            "secret-order",
            "Authorization",
            "Bearer",
            "secret.xlsx",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)

    def test_sync_queue_retry_blockers_reflect_missing_backend_or_google(self):
        with (
            mock.patch.object(desktop_diagnostics, "load_pending_saves", return_value=[{"created_at": ""}]),
            mock.patch.object(desktop_diagnostics, "load_pending_prints", return_value=[]),
            mock.patch.object(desktop_diagnostics, "load_pending_backend_events", return_value=[]),
            mock.patch.object(desktop_diagnostics, "load_pending_telegram", return_value=[]),
        ):
            summary = desktop_diagnostics.build_sync_queue_summary(google_available=False)
        self.assertFalse(summary["retry_enabled"])
        self.assertIn("Google", summary["retry_blocker"])

        with (
            mock.patch.object(desktop_diagnostics, "load_pending_saves", return_value=[]),
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
                mock.patch.object(
                    desktop_diagnostics,
                    "load_pending_saves",
                    return_value=[{
                        "created_at": "2026-07-03 09:00:00",
                        "order": {"Клиент": "SECRET CLIENT", "Адрес": "SECRET ADDRESS"},
                        "codes": ["TEST-SECRET-KIZ-1"],
                        "last_error": "quota token=SECRET",
                    }],
                ),
                mock.patch.object(desktop_diagnostics, "load_pending_prints", return_value=[]),
                mock.patch.object(desktop_diagnostics, "load_pending_backend_events", return_value=[]),
                mock.patch.object(desktop_diagnostics, "load_pending_telegram", return_value=[]),
            ):
                path, manifest = desktop_diagnostics.write_diagnostic_bundle(
                    output_dir=tmp_dir,
                    probes=[{"name": "backend_health", "status": "failed", "class": "auth_rejected"}],
                )

            saved_text = Path(path).read_text(encoding="utf-8")
            saved = json.loads(saved_text)
            serialized = json.dumps(saved, ensure_ascii=False)

        self.assertEqual(saved["app"]["version"], "2.0.39")
        self.assertEqual(saved["app"]["build_label"], "MVP 2.0")
        self.assertEqual(saved["startup_self_check"]["app_dir"], "[redacted-path]")
        self.assertEqual(saved["startup_self_check"]["log_file"], "[redacted-path]")
        self.assertEqual(saved["queue_summary"]["queues"]["google_saves"]["count"], 1)
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

    def test_log_formatter_redacts_nested_google_credential_components(self):
        sentinel = "TAKSKLAD" + "_SYNTHETIC_" + "SECRET_SENTINEL_V1"
        credentials = {
            "client_email": sentinel + "@example.test",
            "private_key": "-----BEGIN PRIVATE KEY-----\n" + sentinel + "\n-----END PRIVATE KEY-----\n",
        }
        set_secret_store_for_tests(MemorySecretStore({
            GOOGLE_CREDENTIALS_SECRET: json.dumps(credentials, separators=(",", ":")),
        }))
        record = logging.LogRecord(
            name="synthetic",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="google failure %s %s",
            args=(credentials, credentials["private_key"]),
            exc_info=None,
        )

        formatted = SecretRedactingFormatter(LOG_FORMAT).format(record)

        self.assertNotIn(sentinel, formatted)
        self.assertNotIn("BEGIN PRIVATE KEY", formatted)
        self.assertNotIn(credentials["client_email"], formatted)
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
