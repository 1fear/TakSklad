import json
import tempfile
import unittest
from pathlib import Path

from tools.check_code_organization import (
    build_dependency_graph,
    forbidden_order_skladbot_sccs,
    load_exceptions,
    orchestrator_method_violations,
    run_checks,
    telegram_persistence_findings,
)


class CodeOrganizationGuardTests(unittest.TestCase):
    def make_repo(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "backend" / "app").mkdir(parents=True)
        (root / "backend" / "app" / "__init__.py").write_text("", encoding="utf-8")
        (root / "tools").mkdir()
        self.addCleanup(temporary.cleanup)
        return root

    def write_exceptions(self, root, exceptions):
        path = root / "tools" / "code_organization_exceptions.json"
        path.write_text(json.dumps({"version": 1, "exceptions": exceptions}), encoding="utf-8")
        return path

    def test_nested_imports_participate_in_order_skladbot_cycle_detection(self):
        root = self.make_repo()
        app = root / "backend" / "app"
        (app / "orders_service.py").write_text(
            "def queue():\n    from .skladbot_worker import run\n    return run\n",
            encoding="utf-8",
        )
        (app / "skladbot_worker.py").write_text(
            "from .orders_service import queue\ndef run():\n    return queue\n",
            encoding="utf-8",
        )

        graph = build_dependency_graph(app)

        self.assertEqual(graph["orders_service"], {"skladbot_worker"})
        self.assertEqual(graph["skladbot_worker"], {"orders_service"})
        self.assertEqual(forbidden_order_skladbot_sccs(graph), [["orders_service", "skladbot_worker"]])

    def test_processor_line_limit_fails_without_exception(self):
        root = self.make_repo()
        app = root / "backend" / "app"
        (app / "telegram_worker.py").write_text("pass\n", encoding="utf-8")
        (app / "telegram_report_processor.py").write_text("pass\n" * 701, encoding="utf-8")
        exception_path = self.write_exceptions(root, [])

        result = run_checks(root, exception_path)

        self.assertTrue(any("telegram_report_processor.py: 701 lines" in error for error in result.errors))

    def test_valid_size_exception_is_applied_and_keeps_strict_result_clean(self):
        root = self.make_repo()
        app = root / "backend" / "app"
        (app / "telegram_worker.py").write_text(
            "class TelegramWorker:\n    def poll_once(self):\n        return None\n" + "pass\n" * 1498,
            encoding="utf-8",
        )
        exception_path = self.write_exceptions(root, [{
            "rule": "max_lines",
            "path": "backend/app/telegram_worker.py",
            "owner": "Backend owner",
            "reason": "Temporary extraction bridge",
        }])

        result = run_checks(root, exception_path)

        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.applied_exceptions), 1)
        self.assertEqual(result.applied_exceptions[0].owner, "Backend owner")

    def test_exception_requires_owner_and_reason(self):
        root = self.make_repo()
        path = self.write_exceptions(root, [{
            "rule": "max_lines",
            "path": "backend/app/telegram_worker.py",
            "owner": "",
            "reason": "",
        }])

        entries, errors = load_exceptions(path)

        self.assertEqual(entries, [])
        self.assertTrue(any("owner" in error and "reason" in error for error in errors))

    def test_telegram_orchestrator_rejects_orm_imports_and_persistence_calls(self):
        root = self.make_repo()
        path = root / "backend" / "app" / "telegram_worker.py"
        path.write_text(
            "from sqlalchemy import select\n"
            "from .db import SessionLocal\n"
            "from .models import PendingEvent\n"
            "def run(db):\n"
            "    row = db.execute(select(PendingEvent)).scalars().first()\n"
            "    db.add(row)\n"
            "    db.commit()\n",
            encoding="utf-8",
        )

        findings = telegram_persistence_findings(path)

        self.assertTrue(any("imports ORM module sqlalchemy" in item for item in findings))
        self.assertTrue(any("imports SessionLocal" in item for item in findings))
        self.assertTrue(any("imports persistence module .models" in item for item in findings))
        self.assertTrue(any(".execute()" in item for item in findings))
        self.assertTrue(any("select()" in item for item in findings))
        self.assertTrue(any(".commit()" in item for item in findings))

    def test_telegram_orchestrator_rejects_domain_methods(self):
        root = self.make_repo()
        app = root / "backend" / "app"
        (app / "telegram_worker.py").write_text(
            "class TelegramWorker:\n"
            "    def poll_once(self):\n"
            "        return None\n"
            "    def import_customer_orders(self):\n"
            "        return None\n",
            encoding="utf-8",
        )

        violations = orchestrator_method_violations(root, app)

        self.assertEqual(len(violations), 1)
        self.assertIn("import_customer_orders", violations[0].message)

    def test_unused_exception_fails_guard(self):
        root = self.make_repo()
        (root / "backend" / "app" / "telegram_worker.py").write_text(
            "class TelegramWorker:\n    def poll_once(self):\n        return None\n",
            encoding="utf-8",
        )
        exception_path = self.write_exceptions(root, [{
            "rule": "max_lines",
            "path": "backend/app/telegram_worker.py",
            "owner": "Backend owner",
            "reason": "Stale temporary exception",
        }])

        result = run_checks(root, exception_path)

        self.assertTrue(any("unused organization exception" in error for error in result.errors))

    def test_current_repository_guard_has_no_unexcepted_errors(self):
        root = Path(__file__).resolve().parents[1]

        result = run_checks(root, root / "tools" / "code_organization_exceptions.json")

        self.assertEqual(result.order_skladbot_sccs, [])
        self.assertEqual(result.errors, [])


if __name__ == "__main__":
    unittest.main()
