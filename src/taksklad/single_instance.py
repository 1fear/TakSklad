import json
import logging
import os
import socket
import time
import uuid

from .config import APP_DIR, APP_NAME
from .utils import normalize_text


SINGLE_INSTANCE_LOCK_FILENAME = "TakSklad_instance.lock"
SINGLE_INSTANCE_LOCK_STALE_SECONDS = 24 * 60 * 60


class SingleInstanceLock:
    def __init__(self, path, owner_id):
        self.path = path
        self.owner_id = owner_id
        self.acquired = True


class SingleInstanceResult:
    def __init__(self, acquired, lock=None, message="", existing=None, recovered=False, reason=""):
        self.acquired = acquired
        self.lock = lock
        self.message = message
        self.existing = existing if isinstance(existing, dict) else {}
        self.recovered = recovered
        self.reason = reason


def single_instance_lock_path(app_dir=None):
    return os.path.join(app_dir or APP_DIR, SINGLE_INSTANCE_LOCK_FILENAME)


def build_single_instance_owner_id():
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def process_is_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            logging.debug("Не удалось проверить Windows PID для single-instance lock", exc_info=True)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _now(now=None):
    return float(time.time() if now is None else now)


def read_single_instance_payload(path):
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _lock_file_age_seconds(path, now=None):
    try:
        updated_ts = os.path.getmtime(path)
    except OSError:
        return 0
    return max(0, _now(now) - float(updated_ts))


def single_instance_lock_is_stale(payload, path, now=None, process_running_func=process_is_running):
    payload = payload if isinstance(payload, dict) else {}
    pid = payload.get("pid")
    if pid:
        try:
            return not process_running_func(pid)
        except Exception:
            logging.debug("Не удалось проверить PID single-instance lock", exc_info=True)
            return False

    updated_ts = payload.get("updated_ts") or payload.get("created_ts")
    try:
        age_seconds = _now(now) - float(updated_ts)
    except (TypeError, ValueError):
        age_seconds = _lock_file_age_seconds(path, now=now)
    return age_seconds >= SINGLE_INSTANCE_LOCK_STALE_SECONDS


def format_single_instance_message(existing=None):
    existing = existing if isinstance(existing, dict) else {}
    pid = normalize_text(existing.get("pid")) or "неизвестен"
    return (
        f"{APP_NAME} уже запущен на этом компьютере.\n\n"
        "Не открывайте второе окно: оно может повредить локальные очереди сканов.\n\n"
        f"Активный процесс: PID {pid}.\n\n"
        "Закройте уже открытый TakSklad или перезагрузите компьютер. "
        "Если окна нет, обратитесь к поддержке."
    )


def _write_lock_file(path, owner_id, now=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    timestamp = _now(now)
    payload = {
        "app": APP_NAME,
        "owner_id": owner_id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "created_ts": timestamp,
        "updated_ts": timestamp,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, sort_keys=True)


def acquire_single_instance_lock(app_dir=None, now=None, process_running_func=process_is_running):
    path = single_instance_lock_path(app_dir)
    owner_id = build_single_instance_owner_id()
    recovered = False

    for _attempt in range(2):
        try:
            _write_lock_file(path, owner_id, now=now)
            return SingleInstanceResult(
                True,
                lock=SingleInstanceLock(path, owner_id),
                recovered=recovered,
                reason="acquired_after_stale_recovery" if recovered else "acquired",
            )
        except FileExistsError:
            existing = read_single_instance_payload(path)
            if single_instance_lock_is_stale(
                existing,
                path,
                now=now,
                process_running_func=process_running_func,
            ):
                try:
                    os.remove(path)
                    recovered = True
                    logging.info("Single-instance lock восстановлен после stale состояния")
                    continue
                except OSError:
                    logging.warning("Не удалось удалить stale single-instance lock", exc_info=True)

            return SingleInstanceResult(
                False,
                message=format_single_instance_message(existing),
                existing=existing,
                reason="already_running",
            )

    return SingleInstanceResult(
        False,
        message=format_single_instance_message({}),
        reason="lock_retry_failed",
    )


def release_single_instance_lock(lock):
    if not lock or not getattr(lock, "acquired", False):
        return False
    payload = read_single_instance_payload(lock.path)
    if payload.get("owner_id") != lock.owner_id:
        lock.acquired = False
        return False
    try:
        os.remove(lock.path)
        lock.acquired = False
        return True
    except FileNotFoundError:
        lock.acquired = False
        return False
    except OSError:
        logging.warning("Не удалось освободить single-instance lock", exc_info=True)
        return False
