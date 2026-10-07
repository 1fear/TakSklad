import json
import os
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "deploy" / "vds" / "container_health_check.sh"
UNIT = PROJECT_ROOT / "deploy" / "vds" / "systemd" / "taksklad-container-health.service"
TIMER = PROJECT_ROOT / "deploy" / "vds" / "systemd" / "taksklad-container-health.timer"
INSTALLER = PROJECT_ROOT / "deploy" / "vds" / "install_container_health_timer.sh"
SERVICES = ("backend-api", "frontend", "postgres", "skladbot-worker", "smartup-auto-import-worker", "telegram-worker")

# Подставной docker: отвечает по состоянию из JSON, а `exec ... python -c КОД URL` исполняет
# настоящий код проверки /ready против локального сервера теста
FAKE_DOCKER = """#!{python} -I
import json, os, subprocess, sys
state = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
args = sys.argv[1:]
if args[0] == "ps":
    service = [a.rsplit("=", 1)[1] for a in args if a.startswith("label=com.docker.compose.service=")][0]
    if service in state["running"]:
        print("vds-%s-1" % service)
elif args[0] == "inspect":
    name = args[-1]
    service = name[len("vds-"):-len("-1")]
    if "Health.Status" in args[2]:
        print(state["health"].get(service, "healthy"))
    else:
        print("1 synthetic healthcheck failure")
elif args[0] == "exec":
    code_index = args.index("-c") + 1
    result = subprocess.run([sys.executable, "-I", "-c", args[code_index], state["ready_url"]], capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    sys.exit(result.returncode)
else:
    sys.exit(64)
"""


class _LocalServer(HTTPServer):
    # HTTPServer.server_bind делает обратный DNS-запрос (getfqdn), на части машин это десятки секунд
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _ReadyHandler(BaseHTTPRequestHandler):
    status_code = 200
    payload = {}

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class ContainerHealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        self.bin_dir = bin_dir
        self.server = _LocalServer(("127.0.0.1", 0), _ReadyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.ready(200, "ok", blocking=0)

    def ready(self, code, status, *, blocking):
        _ReadyHandler.status_code = code
        _ReadyHandler.payload = {"status": status, "queue": {"hot_path_blocking_count": blocking, "hot_path_stale_processing_count": 0}}

    def run_check(self, *, running=SERVICES, health=None):
        state_path = Path(self.temp.name) / "state.json"
        state_path.write_text(json.dumps({
            "running": list(running),
            "health": health or {},
            "ready_url": "http://127.0.0.1:%d/ready" % self.server.server_address[1],
        }), encoding="utf-8")
        env = {**os.environ, "PATH": f"{self.bin_dir}:{os.environ['PATH']}", "FAKE_DOCKER_STATE": str(state_path)}
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    def test_all_healthy_and_ready_ok_exits_zero(self):
        result = self.run_check()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CONTAINER_HEALTH_OK project=vds services=6", result.stdout)
        self.assertNotIn("FINDING", result.stdout)

    def test_unhealthy_service_is_a_finding_with_code_two(self):
        result = self.run_check(health={"smartup-auto-import-worker": "unhealthy"})

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FINDING unhealthy_service=smartup-auto-import-worker", result.stdout)

    def test_missing_service_is_a_finding_with_code_two(self):
        result = self.run_check(running=[s for s in SERVICES if s != "telegram-worker"])

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FINDING service_missing=telegram-worker", result.stdout)

    def test_ready_not_ok_is_a_finding_even_when_containers_are_healthy(self):
        self.ready(503, "unhealthy", blocking=1)

        result = self.run_check()

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FINDING ready_not_ok READY unhealthy http=503 blocking=1 stale=0", result.stdout)

    def test_units_record_findings_on_host_store_and_never_call_telegram(self):
        unit = UNIT.read_text(encoding="utf-8")
        timer = TIMER.read_text(encoding="utf-8")
        installer = INSTALLER.read_text(encoding="utf-8")
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("OnFailure=wms-alert@taksklad-container-health.service", unit)
        self.assertIn("ExecStart=/opt/stacks/taksklad/app/deploy/vds/container_health_check.sh", unit)
        self.assertIn("SuccessExitStatus=2", unit)
        self.assertIn("/opt/ops/alert_on_failure.sh %N", unit)
        self.assertIn("OnUnitActiveSec=10min", timer)
        self.assertIn("/opt/ops/alert_on_failure.sh", installer)
        self.assertIn("/opt/stacks/taksklad/app", installer)
        for text in (unit, script, installer):
            self.assertNotIn("api.telegram.org", text)
            self.assertNotIn("notify_telegram", text)


if __name__ == "__main__":
    unittest.main()
