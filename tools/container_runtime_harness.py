#!/usr/bin/env python3
"""Disposable Phase 22 runtime checks for isolation and resource controls."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
BACKEND_IMAGE = "vds-backend-api:latest"
FRONTEND_IMAGE = "vds-frontend:latest"
POSTGRES_IMAGE = (
    "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)
PROXY_IMAGE = (
    "tecnativa/docker-socket-proxy:v0.4.2@"
    "sha256:1f3a6f303320723d199d2316a3e82b2e2685d86c275d5e3deeaf182573b47476"
)
ADMINER_IMAGE = (
    "adminer:4@sha256:bb7f148f65aae5916b79a5b7b4ac594f04b17340840cda4c556c84fe4c89b110"
)


class HarnessError(RuntimeError):
    pass


def run(command: list[str], *, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-12:])
        raise HarnessError(f"command failed exit={completed.returncode}: {' '.join(command[:4])}\n{tail}")
    return completed


class DockerScope:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.containers: set[str] = set()
        self.volumes: set[str] = set()
        self.networks: set[str] = set()

    def container(self, suffix: str) -> str:
        name = f"{self.prefix}-{suffix}"
        self.containers.add(name)
        return name

    def volume(self, suffix: str) -> str:
        name = f"{self.prefix}-{suffix}"
        run(["docker", "volume", "create", name])
        self.volumes.add(name)
        return name

    def network(self, suffix: str) -> str:
        name = f"{self.prefix}-{suffix}"
        run(["docker", "network", "create", "--internal", name])
        self.networks.add(name)
        return name

    def remove_container(self, name: str) -> None:
        run(["docker", "rm", "-f", name], check=False, timeout=30)

    def cleanup(self) -> None:
        for name in sorted(self.containers, reverse=True):
            self.remove_container(name)
        for name in sorted(self.volumes, reverse=True):
            run(["docker", "volume", "rm", "-f", name], check=False, timeout=30)
        for name in sorted(self.networks, reverse=True):
            run(["docker", "network", "rm", name], check=False, timeout=30)


def ensure_images() -> None:
    for image in (BACKEND_IMAGE, FRONTEND_IMAGE):
        if run(["docker", "image", "inspect", image], check=False, timeout=30).returncode != 0:
            raise HarnessError(f"required locally built image is missing: {image}")


def hardened_flags(*, memory: str = "256m", cpus: str = "0.5", pids: str = "128") -> list[str]:
    return [
        "--init",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        pids,
        "--memory",
        memory,
        "--cpus",
        cpus,
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
    ]


def docker_bind_source(path: Path) -> str:
    rendered = str(path)
    if sys.platform == "darwin" and rendered.startswith("/private/var/"):
        return rendered.removeprefix("/private")
    return rendered


def wait_until(command: list[str], *, attempts: int = 60, delay: float = 0.5) -> str:
    last = ""
    for _ in range(attempts):
        result = run(command, check=False, timeout=30)
        last = result.stdout
        if result.returncode == 0:
            return last
        time.sleep(delay)
    raise HarnessError(f"timed out waiting for {' '.join(command[:4])}: {last[-500:]}")


def verify_container_security(name: str, expected_uid: int) -> dict[str, object]:
    inspected = json.loads(run(["docker", "inspect", name]).stdout)[0]
    host = inspected["HostConfig"]
    if host.get("ReadonlyRootfs") is not True:
        raise HarnessError(f"{name}: rootfs is not read-only")
    if "ALL" not in (host.get("CapDrop") or []):
        raise HarnessError(f"{name}: capabilities were not dropped")
    if not host.get("SecurityOpt") or not any("no-new-privileges" in item for item in host["SecurityOpt"]):
        raise HarnessError(f"{name}: no-new-privileges is absent")
    uid = int(run(["docker", "exec", name, "id", "-u"]).stdout.strip())
    if uid != expected_uid or uid == 0:
        raise HarnessError(f"{name}: unexpected uid={uid}")
    probe = run(
        [
            "docker",
            "exec",
            name,
            "sh",
            "-ceu",
            "! touch /phase22-rootfs-write; touch /tmp/phase22-tmp-write; "
            "test \"$(awk '/CapEff/{print $2}' /proc/1/status)\" = 0000000000000000; "
            "test \"$(awk '/NoNewPrivs/{print $2}' /proc/1/status)\" = 1",
        ]
    )
    return {"uid": uid, "rootfs_write_denied": True, "tmpfs_write": probe.returncode == 0, "caps": 0, "nnp": 1}


def backend_one_shot_probe() -> None:
    script = """
import os
from pathlib import Path
assert os.getuid() == 10001
try:
    Path('/phase22-rootfs-write').write_text('blocked')
except OSError:
    pass
else:
    raise SystemExit('rootfs write unexpectedly succeeded')
Path('/tmp/phase22-write').write_text('ok')
status = Path('/proc/self/status').read_text()
assert 'CapEff:\t0000000000000000' in status
assert 'NoNewPrivs:\t1' in status
print('BACKEND_ISOLATION_OK uid=10001 rootfs_write=denied tmpfs_write=ok caps=0 nnp=1')
"""
    run(["docker", "run", "--rm", *hardened_flags(), BACKEND_IMAGE, "python", "-c", script])


def verify_output_volume(scope: DockerScope) -> int:
    del scope  # The output probe is a production-equivalent temporary bind, not a Docker volume.
    release_state = ROOT / ".release-state"
    release_state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="taksklad-phase22-output-", dir=release_state) as temporary:
        synthetic_root = Path(temporary).resolve()
        (synthetic_root / ".taksklad-phase22-synthetic-root").write_text(
            "TAKSKLAD_PHASE22_SYNTHETIC_OUTPUT_ROOT",
            encoding="utf-8",
        )
        output_path = synthetic_root / "outputs"
        output_path.mkdir()
        os.chmod(output_path, 0o777)
        bind_source = docker_bind_source(output_path)
        run(
            [
                "tools/reconcile_output_permissions.sh",
                "--path",
                str(output_path),
                "--expected-parent",
                str(synthetic_root),
                "--apply",
                "--confirm",
                "PHASE22_CHANGE_OUTPUT_OWNER",
            ]
        )
        run(
            [
                "tools/reconcile_output_permissions.sh",
                "--path",
                str(output_path),
                "--expected-parent",
                str(synthetic_root),
                "--check",
            ]
        )
        mount = f"type=bind,src={bind_source},dst=/app/outputs"
        writer = "from pathlib import Path; Path('/app/outputs/sentinel').write_text('phase22'); print('OUTPUT_WRITE_OK')"
        run(
            [
                "docker",
                "run",
                "--rm",
                *hardened_flags(),
                "--mount",
                mount,
                BACKEND_IMAGE,
                "python",
                "-c",
                writer,
            ]
        )
        reader = "from pathlib import Path; assert Path('/app/outputs/sentinel').read_text() == 'phase22'; print(1)"
        output = run(
            [
                "docker",
                "run",
                "--rm",
                *hardened_flags(),
                "--mount",
                mount,
                BACKEND_IMAGE,
                "python",
                "-c",
                reader,
            ]
        ).stdout.strip()
        return int(output.splitlines()[-1])


def last_integer_line(output: str, label: str) -> int:
    for line in reversed(output.splitlines()):
        value = line.strip()
        if value.isdigit():
            return int(value)
    raise HarnessError(f"{label}: integer result is missing")


def postgres_identity() -> tuple[int, int]:
    uid = last_integer_line(
        run(["docker", "run", "--rm", "--entrypoint", "id", POSTGRES_IMAGE, "-u", "postgres"]).stdout,
        "postgres uid",
    )
    gid = last_integer_line(
        run(["docker", "run", "--rm", "--entrypoint", "id", POSTGRES_IMAGE, "-g", "postgres"]).stdout,
        "postgres gid",
    )
    return uid, gid


def start_postgres(scope: DockerScope, name: str, network: str, volume: str, uid: int, gid: int) -> None:
    scope.remove_container(name)
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "--network-alias",
            "postgres",
            "--init",
            "--user",
            f"{uid}:{gid}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "256",
            "--memory",
            "512m",
            "--cpus",
            "1.0",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs",
            f"/var/run/postgresql:rw,nosuid,nodev,size=16m,uid={uid},gid={gid}",
            "-e",
            "POSTGRES_DB=taksklad_test",
            "-e",
            "POSTGRES_USER=taksklad_test",
            "-e",
            "POSTGRES_PASSWORD=synthetic-only",
            "--mount",
            f"type=volume,src={volume},dst=/var/lib/postgresql/data,volume-nocopy",
            POSTGRES_IMAGE,
        ]
    )
    wait_until(
        [
            "docker",
            "exec",
            name,
            "psql",
            "-U",
            "taksklad_test",
            "-d",
            "taksklad_test",
            "-Atqc",
            "SELECT 1",
        ]
    )


def prepare_postgres(scope: DockerScope, network: str) -> tuple[str, str, int]:
    volume = scope.volume("postgres")
    uid, gid = postgres_identity()
    run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "sh",
            "--mount",
            f"type=volume,src={volume},dst=/var/lib/postgresql/data,volume-nocopy",
            POSTGRES_IMAGE,
            "-ceu",
            "chown -R postgres:postgres /var/lib/postgresql/data",
        ]
    )
    name = scope.container("postgres")
    start_postgres(scope, name, network, volume, uid, gid)
    run(
        [
            "docker",
            "exec",
            name,
            "psql",
            "-U",
            "taksklad_test",
            "-d",
            "taksklad_test",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "CREATE TABLE phase22_sentinel(value integer NOT NULL); INSERT INTO phase22_sentinel VALUES (1);",
        ]
    )
    start_postgres(scope, name, network, volume, uid, gid)
    count = run(
        [
            "docker",
            "exec",
            name,
            "psql",
            "-U",
            "taksklad_test",
            "-d",
            "taksklad_test",
            "-Atqc",
            "SELECT count(*) FROM phase22_sentinel",
        ]
    ).stdout.strip()
    if count != "1":
        raise HarnessError(f"postgres volume preservation failed: count={count}")
    return name, f"postgresql+psycopg://taksklad_test:synthetic-only@postgres:5432/taksklad_test", uid


def backend_environment(database_url: str) -> list[str]:
    values = {
        "DATABASE_URL": database_url,
        "TAKSKLAD_API_TOKEN": "synthetic-only-service-token-1234567890",
        "TAKSKLAD_ENV": "test",
        "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
        "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(),
        "TAKSKLAD_LEGACY_AUTH_MODE": "enforce",
        "TAKSKLAD_SERVICE_NAME": "taksklad-phase22",
        "TAKSKLAD_SERVICE_TOKEN_ROTATION_MAX_OVERLAP_SECONDS": "900",
        "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
        "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        "TAKSKLAD_WEB_SESSION_SECRET": "phase22-independent-synthetic-session-secret-0123456789",
    }
    result: list[str] = []
    for name, value in values.items():
        result.extend(("-e", f"{name}={value}"))
    return result


def start_backend(scope: DockerScope, network: str, database_url: str) -> str:
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            *hardened_flags(memory="512m", cpus="1", pids="192"),
            *backend_environment(database_url),
            BACKEND_IMAGE,
            "python",
            "-m",
            "alembic",
            "upgrade",
            "head",
        ]
    )
    name = scope.container("backend")
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "--network-alias",
            "backend-api",
            *hardened_flags(memory="512m", cpus="1", pids="192"),
            *backend_environment(database_url),
            BACKEND_IMAGE,
        ]
    )
    readiness = """
import json
from urllib.request import urlopen
r = urlopen('http://127.0.0.1:8000/ready', timeout=3)
p = json.load(r)
assert r.status == 200 and p.get('ready') is True
assert (p.get('database') or {}).get('status') == 'ok'
assert (p.get('migrations') or {}).get('status') == 'ok'
print('ready')
"""
    wait_until(["docker", "exec", name, "python", "-c", readiness], attempts=80)
    verify_container_security(name, 10001)
    return name


def start_worker_probes(scope: DockerScope, network: str, database_url: str) -> dict[str, str]:
    result: dict[str, str] = {}
    workers = {
        "skladbot-worker": (
            ["python", "-m", "app.skladbot_worker_runner"],
            {"SKLADBOT_WORKER_INTERVAL_SECONDS": "60"},
        ),
        "smartup-auto-import-worker": (
            ["python", "-m", "app.smartup_auto_import_worker"],
            {
                "SMARTUP_AUTO_IMPORT_ENABLED": "false",
                "SMARTUP_AUTO_IMPORT_POLL_SECONDS": "30",
                "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
            },
        ),
        "telegram-worker": (
            ["python", "-m", "app.telegram_worker_runner"],
            {
                "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": "",
                "TELEGRAM_ADMIN_CHAT_IDS": "",
                "TELEGRAM_ALLOWED_CHAT_IDS": "",
                "TELEGRAM_BOT_TOKEN": "",
            },
        ),
    }
    healthcheck = (
        "from sqlalchemy import text; from app.db import SessionLocal; "
        "db = SessionLocal(); assert db.execute(text('SELECT 1')).scalar() == 1; db.close(); print('green')"
    )
    for service, (entrypoint, environment) in workers.items():
        name = scope.container(service.replace("-worker", ""))
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            *hardened_flags(),
            "-e",
            f"DATABASE_URL={database_url}",
        ]
        for key, value in environment.items():
            command.extend(("-e", f"{key}={value}"))
        command.extend((BACKEND_IMAGE, *entrypoint))
        run(command)
        wait_until(["docker", "exec", name, "python", "-c", healthcheck], attempts=40)
        running = run(["docker", "inspect", "-f", "{{.State.Running}}", name]).stdout.strip()
        if running != "true":
            raise HarnessError(f"{service}: real worker entrypoint is not running")
        verify_container_security(name, 10001)
        result[service] = f"green:{' '.join(entrypoint)}"
    return result


def start_frontend(scope: DockerScope, network: str) -> tuple[str, dict[str, object]]:
    name = scope.container("frontend")
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            *hardened_flags(memory="128m", cpus="0.5", pids="64"),
            "--tmpfs",
            "/etc/nginx/conf.d:rw,nosuid,nodev,size=4m,uid=101,gid=101,mode=0755",
            "--tmpfs",
            "/var/cache/nginx:rw,nosuid,nodev,size=16m,uid=101,gid=101,mode=0755",
            "--tmpfs",
            "/run:rw,nosuid,nodev,size=4m,uid=101,gid=101,mode=0755",
            "-e",
            "TAKSKLAD_BACKEND_INTERNAL_URL=http://backend-api:8000",
            FRONTEND_IMAGE,
        ]
    )
    wait_until(["docker", "exec", name, "wget", "-qO-", "http://127.0.0.1:8080/"])
    return name, verify_container_security(name, 101)


def start_adminer(scope: DockerScope, network: str) -> tuple[str, dict[str, object]]:
    name = scope.container("adminer")
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "--network-alias",
            "adminer",
            "--user",
            "33:33",
            *hardened_flags(memory="128m", cpus="0.5", pids="64"),
            ADMINER_IMAGE,
        ]
    )
    probe = """
from urllib.request import urlopen
r = urlopen('http://adminer:8080/', timeout=3)
assert r.status == 200
print('ok')
"""
    wait_until(
        ["docker", "run", "--rm", "--network", network, BACKEND_IMAGE, "python", "-c", probe],
        attempts=40,
    )
    return name, verify_container_security(name, 33)


def verify_socket_proxy(scope: DockerScope, network: str) -> tuple[int, int]:
    name = scope.container("socket-proxy")
    environment = {
        "AUTH": "0",
        "CONTAINERS": "1",
        "EVENTS": "1",
        "EXEC": "0",
        "IMAGES": "0",
        "INFO": "1",
        "NETWORKS": "1",
        "PING": "1",
        "POST": "0",
        "SECRETS": "0",
        "SERVICES": "0",
        "SWARM": "0",
        "SYSTEM": "0",
        "TASKS": "0",
        "VERSION": "1",
        "VOLUMES": "0",
    }
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--network-alias",
        "docker-socket-proxy",
        "--init",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--memory",
        "64m",
        "--cpus",
        "0.25",
        "--tmpfs",
        "/run:rw,nosuid,nodev,size=8m",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=8m",
        "--tmpfs",
        "/var/lib/haproxy:rw,nosuid,nodev,size=8m",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock:ro",
    ]
    for key, value in environment.items():
        command.extend(("-e", f"{key}={value}"))
    command.append(PROXY_IMAGE)
    run(command)
    probe = """
from urllib.error import HTTPError
from urllib.request import Request, urlopen
assert urlopen('http://docker-socket-proxy:2375/_ping', timeout=3).status == 200
try:
    urlopen(Request('http://docker-socket-proxy:2375/containers/create', data=b'{}', method='POST'), timeout=3)
except HTTPError as exc:
    assert exc.code == 403
else:
    raise SystemExit('Docker POST unexpectedly allowed')
try:
    urlopen('http://docker-socket-proxy:2375/images/json', timeout=3)
except HTTPError as exc:
    assert exc.code == 403
else:
    raise SystemExit('Docker images endpoint unexpectedly allowed')
print('200 403')
"""
    output = wait_until(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            BACKEND_IMAGE,
            "python",
            "-c",
            probe,
        ],
        attempts=30,
    )
    allowed, denied = output.strip().splitlines()[-1].split()
    return int(allowed), int(denied)


def run_smoke() -> None:
    ensure_images()
    suffix = uuid.uuid4().hex[:8]
    scope = DockerScope(f"taksklad-phase22-{suffix}")
    try:
        network = scope.network("network")
        backend_one_shot_probe()
        outputs_count = verify_output_volume(scope)
        postgres_name, database_url, postgres_uid = prepare_postgres(scope, network)
        backend_name = start_backend(scope, network, database_url)
        worker_health = start_worker_probes(scope, network, database_url)
        frontend_name, frontend_security = start_frontend(scope, network)
        adminer_name, adminer_security = start_adminer(scope, network)
        socket_allowed, socket_denied = verify_socket_proxy(scope, network)
        postgres_running = run(["docker", "inspect", "-f", "{{.State.Running}}", postgres_name]).stdout.strip()
        if postgres_running != "true":
            raise HarnessError("postgres is not running after hardened smoke")
        print(
            "CONTAINER_SMOKE_SECURITY "
            f"backend={backend_name}:uid10001 frontend={frontend_name}:uid{frontend_security['uid']} "
            f"adminer={adminer_name}:uid{adminer_security['uid']} "
            "workers=uid10001x4 rootfs_denied=7 caps_zero=7 nnp=7"
        )
        print(
            "CONTAINER_SMOKE_VOLUMES "
            f"output_bind_preserved={outputs_count} postgres_rows_preserved=1 postgres_uid={postgres_uid}"
        )
        print(
            "CONTAINER_SMOKE_HEALTH "
            f"readiness=green postgres=green workers={','.join(f'{k}:{worker_health[k]}' for k in sorted(worker_health))}"
        )
        print(
            "CONTAINER_SMOKE_SOCKET "
            f"ping_status={socket_allowed} unauthorized_status={socket_denied} images_status={socket_denied}"
        )
        print("CONTAINER_SMOKE_OK production_volumes_touched=0 external_sends=0")
    finally:
        scope.cleanup()


def parse_memory_bytes(value: str) -> int:
    match = re.match(r"([0-9.]+)([KMG]iB)", value)
    if not match:
        return 0
    multiplier = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[match.group(2)]
    return int(float(match.group(1)) * multiplier)


def memory_allocation_was_denied(state: dict[str, object]) -> bool:
    return state.get("OOMKilled") is True or state.get("ExitCode") == 42


def run_load() -> None:
    ensure_images()
    suffix = uuid.uuid4().hex[:8]
    scope = DockerScope(f"taksklad-phase22-load-{suffix}")
    name = scope.container("subject")
    oom_name = scope.container("oom")
    load_script = """
import subprocess, time
data = [bytearray(1024 * 1024) for _ in range(32)]
children = []
denied = False
for _ in range(100):
    try:
        children.append(subprocess.Popen(['sleep', '4']))
    except OSError:
        denied = True
        break
print('PHASE22_LOG_FIRST')
deadline = time.monotonic() + 4
x = 0
while time.monotonic() < deadline:
    x = (x * 33 + 17) % 1000003
for index in range(6000):
    print(f'phase22-log-{index:05d}-' + ('x' * 500))
print('PHASE22_PID_DENIED', int(denied), 'spawned', len(children))
print('PHASE22_LOG_LAST')
for child in children:
    child.wait()
time.sleep(2)
"""
    try:
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--init",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "64",
                "--memory",
                "128m",
                "--cpus",
                "0.5",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m",
                "--log-driver",
                "json-file",
                "--log-opt",
                "max-size=64k",
                "--log-opt",
                "max-file=2",
                BACKEND_IMAGE,
                "python",
                "-c",
                load_script,
            ]
        )
        limits = run(
            [
                "docker",
                "inspect",
                "-f",
                "{{json .HostConfig}}",
                name,
            ]
        ).stdout
        host = json.loads(limits)
        if host.get("Memory") != 128 * 1024**2 or host.get("NanoCpus") != 500_000_000 or host.get("PidsLimit") != 64:
            raise HarnessError("Docker HostConfig resource limits differ from the asserted profile")
        cgroup = wait_until(
            [
                "docker",
                "exec",
                name,
                "sh",
                "-ceu",
                "printf '%s %s %s' \"$(cat /sys/fs/cgroup/memory.max)\" "
                "\"$(cat /sys/fs/cgroup/cpu.max)\" \"$(cat /sys/fs/cgroup/pids.max)\"",
            ],
            attempts=20,
        ).strip()
        if "134217728" not in cgroup or "50000 100000" not in cgroup or not cgroup.endswith("64"):
            raise HarnessError(f"cgroup limits are not effective: {cgroup}")

        peak_cpu = 0.0
        peak_memory = 0
        peak_pids = 0
        for _ in range(5):
            stats_raw = run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}", name],
                check=False,
                timeout=20,
            ).stdout.strip()
            if stats_raw:
                stats = json.loads(stats_raw.splitlines()[-1])
                peak_cpu = max(peak_cpu, float(stats.get("CPUPerc", "0%").rstrip("%") or 0))
                peak_memory = max(peak_memory, parse_memory_bytes(str(stats.get("MemUsage", "")).split("/", 1)[0].strip()))
                peak_pids = max(peak_pids, int(stats.get("PIDs", 0) or 0))
            time.sleep(0.5)

        wait_until(["docker", "inspect", "-f", "{{if .State.Running}}1{{else}}0{{end}}", name], attempts=30)
        for _ in range(60):
            state = run(["docker", "inspect", "-f", "{{.State.Status}}", name]).stdout.strip()
            if state == "exited":
                break
            time.sleep(0.25)
        else:
            raise HarnessError("resource load container did not exit")
        exit_code = int(run(["docker", "inspect", "-f", "{{.State.ExitCode}}", name]).stdout)
        if exit_code != 0:
            raise HarnessError(f"resource load exited {exit_code}")
        logs = run(["docker", "logs", name]).stdout
        if "PHASE22_LOG_LAST" not in logs or "PHASE22_LOG_FIRST" in logs:
            raise HarnessError("json-file log rotation did not retain only the newest bounded window")
        if len(logs.encode("utf-8")) > 140 * 1024:
            raise HarnessError(f"retained logs exceed rotation bound: {len(logs.encode('utf-8'))}")
        denied_match = re.search(r"PHASE22_PID_DENIED\s+(\d)\s+spawned\s+(\d+)", logs)
        if not denied_match or denied_match.group(1) != "1" or int(denied_match.group(2)) >= 64:
            raise HarnessError("PID limit did not deny the synthetic process burst")

        run(
            [
                "docker",
                "run",
                "--name",
                oom_name,
                "--memory",
                "64m",
                "--memory-swap",
                "64m",
                "--pids-limit",
                "32",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                BACKEND_IMAGE,
                "python",
                "-c",
                "import sys\ntry:\n bytearray(256 * 1024 * 1024)\nexcept MemoryError:\n sys.exit(42)\nsys.exit(0)",
            ],
            check=False,
            timeout=60,
        )
        oom_state = json.loads(run(["docker", "inspect", "-f", "{{json .State}}", oom_name]).stdout)
        if not memory_allocation_was_denied(oom_state):
            raise HarnessError("memory limit did not deny the oversize synthetic allocation")
        memory_denial_mode = "oom-kill" if oom_state.get("OOMKilled") is True else "memory-error"

        print(
            "CONTAINER_LOAD_LIMITS "
            f"memory_max=134217728 cpu_max=50000/100000 pids_max=64 "
            f"peak_cpu_percent={peak_cpu:.2f} peak_memory_bytes={peak_memory} peak_pids={peak_pids}"
        )
        print(
            "CONTAINER_LOAD_ENFORCEMENT "
            f"pid_denied=1 memory_limit_denied=1 memory_denial_mode={memory_denial_mode} "
            f"retained_log_bytes={len(logs.encode('utf-8'))} "
            "log_files_max=2 log_size_each=64k"
        )
        print("CONTAINER_LOAD_OK production_volumes_touched=0 external_sends=0")
    finally:
        scope.cleanup()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("smoke")
    subparsers.add_parser("load")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "smoke":
            run_smoke()
        else:
            run_load()
    except (HarnessError, OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        print(f"CONTAINER_HARNESS_ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
