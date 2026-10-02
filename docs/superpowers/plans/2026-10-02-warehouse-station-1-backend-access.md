# Вход станции на backend: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** складская станция получает сессию сайта без пароля только с адресов сети склада, и каждая её сессия работает только оттуда

**Architecture:** новая роль `station` в каталоге прав, новый публичный `POST /api/v1/auth/station`, который выдаёт обычную
cookie-сессию пользователю `warehouse-station`, и проверка адреса клиента в единой точке чтения сессии `read_web_session`;
адрес клиента берётся существующим `client_identity`, поэтому nginx и сеть Docker закрепляются так, чтобы backend всегда видел
доверенного соседа и настоящий адрес склада в `X-Forwarded-For`

**Tech Stack:** Python 3.12, FastAPI 0.133, Starlette 1.3, SQLAlchemy, unittest; nginx шаблон фронта; docker compose

**Spec:** [docs/superpowers/specs/2026-10-02-warehouse-station-web-design.md](../specs/2026-10-02-warehouse-station-web-design.md),
разделы 1, 2, 8, 9, 10, 12; дорожная карта: [2026-10-02-warehouse-station-roadmap.md](2026-10-02-warehouse-station-roadmap.md)

## Global Constraints

- переменная `TAKSKLAD_WAREHOUSE_CIDRS`, список через запятую; пустая значит станция выключена
- битое значение, `0.0.0.0/0`, `::/0`, частные и служебные сети не роняют backend, а выключают станцию с записью в журнал
- роль `station`: ровно `warehouse:read`, `warehouse:write`, `reports:read`
- пользователь станции `warehouse-station`, `password_hash` пустой, роль `station`
- отказ по адресу: 403, тело `{"detail": {"code": "station_network_denied"}}`, без записи в общий ограничитель входа
- успешных входов станции не больше 10 в минуту с одного адреса, дальше 429
- сессии других ролей проверкой адреса не затрагиваются
- в production `TAKSKLAD_TRUSTED_PROXY_CIDRS` остаётся ровно `172.18.0.0/16` (`backend/app/settings.py:26`)
- выкатка backend с пустым `TAKSKLAD_WAREHOUSE_CIDRS`; адреса склада вносятся только в день пилота по разрешению Антона
- push, PR, мерж и выкатка только по отдельному разрешению Антона; коммиты с префиксом `ALLOW_NON_MAIN_BRANCH=1`, файлы поимённо
- рабочее дерево: `/tmp/sklad-web-station`, ветка `feat/warehouse-station-web` (создана от `origin/main` `3d75e86`, в ней уже коммит спецификации)
- интерпретатор тестов: `/Users/anton/Documents/work/TakSklad/.venv/bin/python` (в worktree своего `.venv` нет), запуск из корня дерева с `PYTHONPATH=.`

## Review Focus

1. Cookie станции, унесённая за пределы склада и предъявленная напрямую в `api.taksklad.uz` в обход nginx: должна получить 401
   (Task 3, тест `test_station_session_is_rejected_outside_warehouse` бьёт мимо nginx прямо в приложение)
2. `X-Forwarded-For` с адресом склада от чужого клиента и дописанный перед настоящим адресом: доступа нет
   (Task 3, `test_forwarded_header_counts_only_behind_trusted_proxy`)
3. Сто чужих попыток входа станции не блокируют пароль администратора в `/admin` (Task 3, проверка, что общий ограничитель не вызывается)
4. Администратор деактивирует пользователя станции: все её сессии гаснут сразу, новый вход даёт 503, а не новую сессию
   (Task 3, `test_deactivated_station_user_loses_sessions`)
5. nginx сходил в backend через сеть `172.22`: станция отказала бы складу; закрыто псевдонимом в сети `traefik`
   и живой положительной проверкой (Task 4 и раздел «После мержа»)

Код всех задач прогнан на копии `origin/main` `3d75e86` 02.10: тесты станции из этого плана 16 из 16,
`tests.test_backend_rbac_policy` зелёный после правок счётчиков, тесты задачи 4 зелёные, полный набор зелёный (Task 5)

---

### Task 1: Настройка адресов склада и разбор сетей

**Files:**
- Create: `backend/app/station_access.py`
- Modify: `backend/app/settings.py` (поле в конце `Settings` после `skladbot_daily_report_lookback_days: int`, строка 99 на `3d75e86`;
  разбор рядом с `trusted_proxy_cidrs=...`, строка 198)
- Test: `tests/test_backend_station_access.py` (новый файл, в этой задаче только два теста про разбор)

**Interfaces:**
- Consumes: `backend.app.settings.parse_csv`, `backend.app.access_policy.ROLE_STATION` (появляется в Task 2; в этой задаче
  `station_access.py` ещё не импортирует его, функция `ensure_station_user` добавляется в Task 3)
- Produces:
  - `Settings.warehouse_cidrs: tuple[str, ...]`, по умолчанию `()`
  - `parse_warehouse_cidrs(values) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]`, пустой кортеж при любом сомнительном значении
  - `is_warehouse_address(address: str, networks) -> bool`
  - константы `STATION_USERNAME = "warehouse-station"`, `STATION_NETWORK_DENIED_CODE = "station_network_denied"`,
    `STATION_LOGIN_MAX_PER_MINUTE = 10`

- [ ] **Step 1: Write the failing test**

Создать `tests/test_backend_station_access.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/sklad-web-station && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_backend_station_access -v`
Expected: ERROR `ModuleNotFoundError: No module named 'backend.app.station_access'`; модуль новый, поэтому красный здесь
только на импорте; поведенческий красный у `test_settings_read_warehouse_cidrs` появится, если сначала создать модуль:
`AttributeError: 'Settings' object has no attribute 'warehouse_cidrs'`

- [ ] **Step 3: Write minimal implementation**

`backend/app/settings.py`, в классе `Settings` последним полем:

```python
    skladbot_daily_report_lookback_days: int
    warehouse_cidrs: tuple[str, ...] = ()
```

в `load_settings` сразу после строки `trusted_proxy_cidrs=parse_csv(environ.get("TAKSKLAD_TRUSTED_PROXY_CIDRS", "")),`:

```python
        warehouse_cidrs=parse_csv(environ.get("TAKSKLAD_WAREHOUSE_CIDRS", "")),
```

`validate_backend_settings` не трогать: битое значение выключает станцию, а не backend

Создать `backend/app/station_access.py`:

```python
"""Вход складской станции без пароля: только с адресов сети склада."""

from __future__ import annotations

import ipaddress
import logging

STATION_USERNAME = "warehouse-station"
STATION_NETWORK_DENIED_CODE = "station_network_denied"
STATION_LOGIN_MAX_PER_MINUTE = 10

_FORBIDDEN_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)


def parse_warehouse_cidrs(values) -> tuple:
    """Разбирает список сетей склада; любое сомнительное значение выключает станцию целиком."""
    networks = []
    for raw in values or ():
        try:
            network = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            logging.error("TAKSKLAD_WAREHOUSE_CIDRS содержит битое значение, станция выключена")
            return ()
        if network.prefixlen == 0 or any(network.overlaps(blocked) for blocked in _FORBIDDEN_NETWORKS):
            logging.error("TAKSKLAD_WAREHOUSE_CIDRS содержит общую или частную сеть, станция выключена")
            return ()
        networks.append(network)
    return tuple(networks)


def is_warehouse_address(address: str, networks) -> bool:
    try:
        parsed = ipaddress.ip_address(str(address or "").strip())
    except ValueError:
        return False
    return any(parsed.version == network.version and parsed in network for network in networks)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /tmp/sklad-web-station && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_backend_station_access -v`
Expected: `Ran 2 tests` `OK`; в выводе две строки журнала `ERROR:root:TAKSKLAD_WAREHOUSE_CIDRS ...` это ожидаемо

- [ ] **Step 5: Commit**

```bash
cd /tmp/sklad-web-station
git add backend/app/station_access.py backend/app/settings.py tests/test_backend_station_access.py
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(backend): адреса сети склада для станции, разбор с выключением при сомнительном значении"
```

---

### Task 2: Роль `station` и публичный маршрут входа в каталоге прав

**Files:**
- Modify: `backend/app/access_policy.py` (константа роли рядом с `ROLE_OPERATOR`, строка 10; матрица `ROLE_PERMISSION_MATRIX`, строки 39-54;
  `ROUTE_POLICIES` рядом с `("POST", "/api/v1/auth/login")`, строка 99)
- Modify: `backend/app/web_auth.py` (импорт из `.access_policy`, строки 12-20; `normalize_role`, строка 176-178)
- Modify: `tests/test_backend_rbac_policy.py` (строки 16-20 импорт, 41 счётчик роутов, 61-62 ограниченные роли, 72-76 матрица, 176 счётчик решений)
- Modify: `tests/test_postgres_rbac_audit.py` (словарь ролей, строки 122-126)

**Interfaces:**
- Consumes: ничего из Task 1
- Produces: `ROLE_STATION = "station"` в `backend.app.access_policy`; `normalize_role("station") == "station"`;
  `role_permissions("station") == ("reports:read", "warehouse:read", "warehouse:write")`;
  политика `("POST", "/api/v1/auth/station")` публичная

Маршрута `/api/v1/auth/station` в приложении ещё нет, поэтому тест «у каждого роута ровно одна политика» в этой задаче
станет зелёным только вместе с Task 3; в этой задаче правится счётчик, а проверка набора роутов временно красная по честной причине:
политика есть, роута нет; поэтому Task 2 и Task 3 сдаются ревьюеру вместе, коммит Task 2 делается после зелёного Task 3

- [ ] **Step 1: Write the failing test**

В `tests/test_backend_rbac_policy.py`:

импорт, добавить `ROLE_STATION` после `ROLE_PERMISSION_MATRIX,`:

```python
    ROLE_OPERATOR,
    ROLE_PERMISSION_MATRIX,
    ROLE_STATION,
```

в `test_every_versioned_route_has_exactly_one_policy` заменить `self.assertEqual(len(actual), 70)` на:

```python
        self.assertEqual(len(actual), 71)
        self.assertIn(("POST", "/api/v1/auth/station"), actual)
```

в `test_sensitive_admin_surfaces_are_not_granted_to_restricted_roles` заменить строку `restricted_permissions = ...` на:

```python
        restricted_permissions = (
            ROLE_PERMISSION_MATRIX[ROLE_OPERATOR]
            | ROLE_PERMISSION_MATRIX[ROLE_LOGISTICS_SLOTS]
            | ROLE_PERMISSION_MATRIX[ROLE_STATION]
        )
```

в `test_role_matrix_is_complete_and_unknown_roles_fail_closed` заменить первую строку и добавить проверки станции:

```python
        self.assertEqual(set(ROLE_PERMISSION_MATRIX), {ROLE_ADMIN, ROLE_OPERATOR, ROLE_LOGISTICS_SLOTS, ROLE_STATION})
```

и после `self.assertNotIn(PERMISSION_ADMIN_READ, role_permissions(ROLE_LOGISTICS_SLOTS))`:

```python
        self.assertEqual(
            set(role_permissions(ROLE_STATION)),
            {"warehouse:read", "warehouse:write", "reports:read"},
        )
        self.assertEqual(normalize_role("station"), ROLE_STATION)
```

в `test_enforcement_executes_complete_role_and_service_matrix` заменить `self.assertEqual(decisions, 384)` на
`self.assertEqual(decisions, 448)` (64 защищённых роута на 4 роли плюс неизменные 192 решения сервисных принципалов)

В `tests/test_postgres_rbac_audit.py` в словаре ожидаемых ролей добавить станцию:

```python
            "logistics_slots": 64,
            "station": 64,
        })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/sklad-web-station && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_backend_rbac_policy -v`
Expected: ERROR `ImportError: cannot import name 'ROLE_STATION' from 'backend.app.access_policy'`

- [ ] **Step 3: Write minimal implementation**

`backend/app/access_policy.py`:

```python
ROLE_OPERATOR = "operator"
ROLE_STATION = "station"
```

в `ROLE_PERMISSION_MATRIX` последним элементом:

```python
    # Складская станция в браузере: только склад и отчёт смены, вход только из сети склада
    ROLE_STATION: frozenset({
        PERMISSION_WAREHOUSE_READ,
        PERMISSION_WAREHOUSE_WRITE,
        PERMISSION_REPORT_READ,
    }),
```

в `ROUTE_POLICIES` сразу после строки входа:

```python
    ("POST", "/api/v1/auth/login"): _public(),
    ("POST", "/api/v1/auth/station"): _public(),
```

`backend/app/web_auth.py`, импорт:

```python
    ROLE_OPERATOR,
    ROLE_STATION,
```

и `normalize_role`:

```python
    return role if role in {ROLE_ADMIN, ROLE_LOGISTICS_SLOTS, ROLE_OPERATOR, ROLE_STATION} else ROLE_DENIED
```

- [ ] **Step 4: Run test to verify the expected state**

Run: `cd /tmp/sklad-web-station && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_backend_rbac_policy -v`
Expected: все тесты зелёные, кроме `test_every_versioned_route_has_exactly_one_policy`: он красный с
`AssertionError: Items in the second set but not the first: ('POST', '/api/v1/auth/station')`, роут появляется в Task 3;
`test_enforcement_executes_complete_role_and_service_matrix` уже даёт 448

- [ ] **Step 5: Commit**

Не коммитить отдельно: коммит вместе с Task 3, когда весь `tests.test_backend_rbac_policy` зелёный

---

### Task 3: Пользователь станции, `POST /api/v1/auth/station` и проверка адреса сессии

**Files:**
- Modify: `backend/app/station_access.py` (добавить `ensure_station_user`)
- Modify: `backend/app/main.py`:
  - импорт `.access_policy` (строки 18-22): добавить `ROLE_STATION`
  - после `from .models import AuditLog, User` (строка 204): импорт из `.station_access`
  - после `login_limiter = BoundedTTLLoginLimiter(...)` (строки 225-228): `station_session_limiter`
  - `read_web_session` (строки 586-600): проверка адреса для роли `station`
  - перед `def login_attempt_key` (строка 667): эндпоинт `station_login`
- Test: `tests/test_backend_station_access.py` (дописать класс `StationAccessTests`)

**Interfaces:**
- Consumes: Task 1 (`parse_warehouse_cidrs`, `is_warehouse_address`, константы), Task 2 (`ROLE_STATION`)
- Produces:
  - `ensure_station_user(db, *, now=None) -> User` в `backend.app.station_access`
  - `POST /api/v1/auth/station` без тела; 200 с телом `AuthSessionRead` (`authenticated`, `login`, `role`, `permissions`,
    `expires_at`, `csrf_token`) и cookie `taksklad_web_session`; 403 `{"detail": {"code": "station_network_denied"}}`;
    403 без Origin; 429 с `Retry-After`; 503, если пользователь станции выключен
  - в `backend.app.main`: `station_session_limiter`, `warehouse_networks()`, `request_from_warehouse(request) -> bool`,
    `ensure_station_session_network(request, payload) -> None` (бросает `WebAuthError`)
  - план 2 опирается на этот контракт во фронте

- [ ] **Step 1: Write the failing test**

В `tests/test_backend_station_access.py` заменить шапку файла (импорты и константы до `class StationNetworkParsingTests`) на шапку ниже
и дописать класс `StationAccessTests` после класса из Task 1; класс из Task 1 и блок `if __name__` в конце остаются:

```python
import unittest
import uuid
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main as backend_main
from backend.app.login_limiter import BoundedTTLLoginLimiter
from backend.app.models import Base, User
from backend.app.settings import load_settings
from backend.app.station_access import (
    STATION_USERNAME,
    ensure_station_user,
    is_warehouse_address,
    parse_warehouse_cidrs,
)
from backend.app.web_auth import (
    SESSION_COOKIE_NAME,
    WebAuthError,
    authenticate_web_user,
    hash_password,
    normalize_login,
    role_permissions,
)

WAREHOUSE_IP = "203.0.113.10"
SECOND_WAREHOUSE_IP = "198.51.100.7"
OUTSIDE_IP = "198.51.100.99"
TRUSTED_PROXY_IP = "172.18.0.9"
ORIGIN = {"Origin": "http://testserver"}


def station_settings(**overrides):
    environ = {
        "TAKSKLAD_ENV": "test",
        "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
        "TAKSKLAD_LEGACY_AUTH_MODE": "disabled",
        "TAKSKLAD_WEB_SESSION_SECRET": "synthetic-station-session-secret-0123456789",
        "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        "TAKSKLAD_TRUSTED_PROXY_CIDRS": "172.18.0.0/16",
        "TAKSKLAD_WAREHOUSE_CIDRS": f"{WAREHOUSE_IP}/32,{SECOND_WAREHOUSE_IP}/32",
    }
    environ.update(overrides)
    return load_settings(environ)


class StationAccessTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        backend_main.app.dependency_overrides[backend_main.get_db] = override_get_db
        self.settings_patch = mock.patch.object(backend_main, "settings", station_settings())
        self.settings_patch.start()
        self.limiter_patch = mock.patch.object(
            backend_main,
            "station_session_limiter",
            BoundedTTLLoginLimiter(max_entries=100, entry_ttl_seconds=120),
        )
        self.limiter_patch.start()

    def tearDown(self):
        self.limiter_patch.stop()
        self.settings_patch.stop()
        backend_main.app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def client_from(self, address):
        return TestClient(backend_main.app, client=(address, 50000))

    def station_login(self, client, headers=None):
        return client.post("/api/v1/auth/station", headers={**ORIGIN, **(headers or {})})

    def test_station_login_from_warehouse_issues_session(self):
        client = self.client_from(WAREHOUSE_IP)

        response = self.station_login(client)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["login"], STATION_USERNAME)
        self.assertEqual(body["role"], "station")
        self.assertEqual(body["permissions"], ["reports:read", "warehouse:read", "warehouse:write"])
        self.assertTrue(body["csrf_token"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn(SESSION_COOKIE_NAME, response.cookies)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertEqual(client.get("/api/v1/auth/check").status_code, 204)
        session = client.get("/api/v1/auth/session").json()
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["role"], "station")
        self.assertEqual(client.get("/api/v1/orders/active").status_code, 200)

    def test_second_warehouse_address_is_accepted(self):
        self.assertEqual(self.station_login(self.client_from(SECOND_WAREHOUSE_IP)).status_code, 200)

    def test_outside_address_is_denied_with_code_and_no_shared_limiter_entry(self):
        client = self.client_from(OUTSIDE_IP)
        with mock.patch.object(backend_main, "register_login_failure") as shared_failure:
            response = self.station_login(client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], {"code": "station_network_denied"})
        self.assertNotIn(SESSION_COOKIE_NAME, response.cookies)
        shared_failure.assert_not_called()
        with self.SessionLocal() as db:
            self.assertEqual(db.execute(select(func.count()).select_from(User)).scalar_one(), 0)

    def test_login_without_origin_is_denied(self):
        response = self.client_from(WAREHOUSE_IP).post("/api/v1/auth/station")

        self.assertEqual(response.status_code, 403)
        self.assertNotIn(SESSION_COOKIE_NAME, response.cookies)

    def test_empty_or_unsafe_cidr_list_disables_station(self):
        for value in ("", "0.0.0.0/0", "172.18.0.0/16", "not-a-network", f"{WAREHOUSE_IP}/32,::/0"):
            with self.subTest(value=value):
                with mock.patch.object(backend_main, "settings", station_settings(TAKSKLAD_WAREHOUSE_CIDRS=value)):
                    response = self.station_login(self.client_from(WAREHOUSE_IP))
                self.assertEqual(response.status_code, 403)

    def test_station_session_is_rejected_outside_warehouse(self):
        warehouse = self.client_from(WAREHOUSE_IP)
        token = self.station_login(warehouse).cookies[SESSION_COOKIE_NAME]
        outside = self.client_from(OUTSIDE_IP)
        outside.cookies.set(SESSION_COOKIE_NAME, token)

        self.assertEqual(outside.get("/api/v1/auth/check").status_code, 401)
        self.assertFalse(outside.get("/api/v1/auth/session").json()["authenticated"])
        self.assertEqual(outside.get("/api/v1/orders/active").status_code, 401)
        self.assertEqual(warehouse.get("/api/v1/auth/check").status_code, 204)

    def test_forwarded_header_counts_only_behind_trusted_proxy(self):
        spoofed = self.station_login(self.client_from(OUTSIDE_IP), {"X-Forwarded-For": WAREHOUSE_IP})
        self.assertEqual(spoofed.status_code, 403)

        proxied = self.station_login(self.client_from(TRUSTED_PROXY_IP), {"X-Forwarded-For": WAREHOUSE_IP})
        self.assertEqual(proxied.status_code, 200)

        prepended = self.station_login(
            self.client_from(TRUSTED_PROXY_IP),
            {"X-Forwarded-For": f"{WAREHOUSE_IP}, {OUTSIDE_IP}"},
        )
        self.assertEqual(prepended.status_code, 403)

    def test_auth_check_behind_proxy_needs_forwarded_address(self):
        proxy = self.client_from(TRUSTED_PROXY_IP)
        forwarded = {"X-Forwarded-For": WAREHOUSE_IP}
        self.assertEqual(self.station_login(proxy, forwarded).status_code, 200)

        self.assertEqual(proxy.get("/api/v1/auth/check", headers=forwarded).status_code, 204)
        self.assertEqual(proxy.get("/api/v1/orders/active", headers=forwarded).status_code, 200)
        # nginx без X-Forwarded-For: backend видит только адрес прокси и отказывает
        self.assertEqual(proxy.get("/api/v1/auth/check").status_code, 401)

    def test_station_user_cannot_log_in_with_password(self):
        self.assertEqual(normalize_login(STATION_USERNAME), "")
        with self.SessionLocal() as db:
            ensure_station_user(db)
            db.commit()
            with self.assertRaises(WebAuthError):
                authenticate_web_user(backend_main.settings, STATION_USERNAME, "", db=db)

    def test_station_user_is_created_once(self):
        with self.SessionLocal() as db:
            first = ensure_station_user(db)
            second = ensure_station_user(db)
            db.commit()
            self.assertEqual(first.id, second.id)
            self.assertIsNone(first.password_hash)
            self.assertEqual(first.role, "station")
            self.assertEqual(
                db.execute(select(func.count()).select_from(User).where(User.username == STATION_USERNAME)).scalar_one(),
                1,
            )

    def test_deactivated_station_user_loses_sessions(self):
        client = self.client_from(WAREHOUSE_IP)
        self.station_login(client)
        with self.SessionLocal() as db:
            user = db.execute(select(User).where(User.username == STATION_USERNAME)).scalar_one()
            user.is_active = False
            db.commit()

        self.assertEqual(client.get("/api/v1/auth/check").status_code, 401)
        self.assertEqual(self.station_login(client).status_code, 503)

    def test_other_roles_are_not_bound_to_warehouse_network(self):
        now = datetime.now(timezone.utc)
        with self.SessionLocal() as db:
            db.add(User(
                id=uuid.uuid4(),
                username="998000000001",
                password_hash=hash_password("synthetic-password", salt="synthetic-salt", iterations=1000),
                role="operator",
                is_active=True,
                auth_version=1,
                created_at=now,
                updated_at=now,
            ))
            db.commit()
        outside = self.client_from(OUTSIDE_IP)
        login = outside.post(
            "/api/v1/auth/login",
            json={"login": "998000000001", "password": "synthetic-password"},
            headers=ORIGIN,
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(outside.get("/api/v1/auth/check").status_code, 204)

    def test_successful_station_logins_are_limited_per_address(self):
        client = self.client_from(WAREHOUSE_IP)
        statuses = [self.station_login(client).status_code for _ in range(11)]

        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)
        self.assertEqual(self.station_login(self.client_from(SECOND_WAREHOUSE_IP)).status_code, 200)

    def test_station_role_permissions(self):
        self.assertEqual(role_permissions("station"), ("reports:read", "warehouse:read", "warehouse:write"))
        self.assertTrue(is_warehouse_address(WAREHOUSE_IP, parse_warehouse_cidrs([f"{WAREHOUSE_IP}/32"])))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/sklad-web-station && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_backend_station_access -v`
Expected: ERROR на импорте `ensure_station_user`; после добавления только `ensure_station_user` (первая часть Step 3)
поведенческие отказы: `test_station_login_from_warehouse_issues_session` даёт `404 != 200` (маршрута нет; на базе
`POST /api/v1/auth/station` отвечает `404 {"detail":"Not Found"}`, проверено 02.10), `test_station_session_is_rejected_outside_warehouse`
падает на отсутствующей cookie (ожидаемо `KeyError`, не сверено), тест подделки адреса даёт `404 != 403` на первой проверке;
`test_station_user_cannot_log_in_with_password`, `test_station_user_is_created_once`, `test_station_role_permissions`,
`test_other_roles_are_not_bound_to_warehouse_network` запирающие, зелёные сразу после `ensure_station_user`

- [ ] **Step 3: Write minimal implementation**

`backend/app/station_access.py`: импорты в начало файла после `import logging`:

```python
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .access_policy import ROLE_STATION
from .models import User
```

и функция в конец файла:

```python
def ensure_station_user(db, *, now: datetime | None = None) -> User:
    """Находит или создаёт пользователя станции; две параллельные вставки не дают ошибки."""
    existing = db.execute(select(User).where(User.username == STATION_USERNAME)).scalar_one_or_none()
    if existing is not None:
        return existing
    moment = now or datetime.now(timezone.utc)
    try:
        with db.begin_nested():
            db.add(User(
                id=uuid.uuid4(),
                username=STATION_USERNAME,
                password_hash=None,
                role=ROLE_STATION,
                is_active=True,
                auth_version=1,
                created_at=moment,
                updated_at=moment,
            ))
    except IntegrityError:
        pass
    return db.execute(select(User).where(User.username == STATION_USERNAME)).scalar_one()
```

`backend/app/main.py`, импорт `.access_policy`:

```python
from .access_policy import (
    AUTH_PROTECTED,
    ROLE_STATION,
    SAFE_METHODS,
    route_policy,
)
```

сразу после `from .models import AuditLog, User`:

```python
from .station_access import (
    STATION_LOGIN_MAX_PER_MINUTE,
    STATION_NETWORK_DENIED_CODE,
    ensure_station_user,
    is_warehouse_address,
    parse_warehouse_cidrs,
)
```

сразу после блока `login_limiter = BoundedTTLLoginLimiter(...)`:

```python
station_session_limiter = BoundedTTLLoginLimiter(
    max_entries=1000,
    entry_ttl_seconds=120,
)
```

`read_web_session` целиком заменить на:

```python
def read_web_session(request: Request, db=None, *, touch_last_used: bool = True):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if settings.identity_auth_enabled and db is not None and str(token or "").startswith("tks."):
        verified = validate_user_session(db, token, touch_last_used=touch_last_used)
        payload = {
            "sub": verified.username,
            "role": verified.role,
            "exp": int(verified.expires_at.timestamp()),
            "sid": str(verified.session_id),
            "uid": str(verified.user_id),
            "av": verified.auth_version,
        }
    else:
        if not legacy_auth_window_active():
            raise WebAuthError("legacy web session is disabled")
        payload = verify_session_token(settings, token)
    ensure_station_session_network(request, payload)
    return payload


def warehouse_networks():
    return parse_warehouse_cidrs(settings.warehouse_cidrs)


def request_from_warehouse(request: Request) -> bool:
    return is_warehouse_address(client_identity(request, settings.trusted_proxy_cidrs), warehouse_networks())


def ensure_station_session_network(request: Request, payload) -> None:
    # Сессия станции живёт только в сети склада: унесённая cookie снаружи не работает
    if normalize_role(payload.get("role")) != ROLE_STATION:
        return
    if not request_from_warehouse(request):
        raise WebAuthError("station session outside warehouse network")
```

перед `def login_attempt_key(request: Request, login):`:

```python
@auth_api.post("/station", response_model=AuthSessionRead)
def station_login(request: Request, response: Response, db=Depends(get_db)):
    prevent_auth_response_caching(response)
    require_browser_origin(request)
    address = client_identity(request, settings.trusted_proxy_cidrs)
    if not settings.identity_auth_enabled or not is_warehouse_address(address, warehouse_networks()):
        # Отказ без записи в общий ограничитель: чужие попытки не блокируют вход в /admin
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": STATION_NETWORK_DENIED_CODE},
        )
    station_key = f"station:{address}"
    try:
        station_session_limiter.ensure_not_locked(station_key)
    except (LoginRateLimited, LoginLimiterCapacityExceeded) as exc:
        raise login_rate_limited_http_exception(exc) from exc
    try:
        user = ensure_station_user(db)
        issued = create_user_session(
            db,
            user,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.web_session_ttl_seconds),
        )
        verified = validate_user_session(db, issued.token)
        db.commit()
    except IdentityAuthError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Station session is temporarily unavailable",
        ) from exc
    try:
        station_session_limiter.register_failure(
            station_key,
            max_attempts=STATION_LOGIN_MAX_PER_MINUTE,
            window_seconds=60,
            lock_seconds=60,
        )
    except (LoginRateLimited, LoginLimiterCapacityExceeded):
        pass
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.token,
        max_age=settings.web_session_ttl_seconds,
        path="/",
        httponly=True,
        secure=settings.web_cookie_secure,
        samesite="lax",
    )
    return auth_session_read({
        "sub": verified.username,
        "role": verified.role,
        "exp": int(verified.expires_at.timestamp()),
    }, issued.token)
```

`register_failure` здесь считает успешные входы: ограничитель тот же класс, что у пароля, но свой экземпляр;
после десятого входа за минуту адрес заблокирован на минуту, одиннадцатый получает 429

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /tmp/sklad-web-station && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_backend_station_access tests.test_backend_rbac_policy tests.test_backend_auth_identities tests.test_backend_api_persistence tests.test_frontend_csrf_contract -v 2>&1 | tail -5`
Expected: `Ran 175 tests` `OK`: в файле станции 16 тестов (2 из Task 1 и 14 из Task 3); снято прогоном на копии `3d75e86`
с кодом этого плана 02.10;
до правок счётчиков RBAC падали с `71 != 70`, `448 != 384` и лишней ролью в наборе

- [ ] **Step 5: Commit**

```bash
cd /tmp/sklad-web-station
git add backend/app/access_policy.py backend/app/web_auth.py backend/app/station_access.py backend/app/main.py \
  tests/test_backend_station_access.py tests/test_backend_rbac_policy.py tests/test_postgres_rbac_audit.py
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(backend): вход станции без пароля из сети склада, роль station, проверка адреса сессии"
```

---

### Task 4: nginx и сеть Docker: маршрут входа станции и доверенный путь к backend

Снято на боевом хосте 02.10, только чтение: `vds-frontend-1` и `vds-backend-api-1` стоят в сетях `traefik` (`172.18.0.0/16`)
и `vds_taksklad-internal` (`172.22.0.0/16`); `getent hosts backend-api` в контейнере фронта отдаёт `172.18.0.4`;
выбор сети делает DNS Docker, поэтому путь закрепляется псевдонимом, который есть только в сети `traefik`

**Files:**
- Modify: `frontend/nginx.conf.template` (новый `location = /api/v1/auth/station` после `location = /api/v1/auth/session`,
  строки 44-52; заголовки адреса в `location = /_taksklad_auth_check`, строки 54-64)
- Modify: `deploy/vds/docker-compose.yml` (`backend-api.environment` после `TAKSKLAD_TRUSTED_PROXY_CIDRS`, строка 165;
  `backend-api.networks`, строки 193-195; `frontend.environment`, строка 210)
- Modify: `deploy/vds/.env.example` (после `TAKSKLAD_WEB_LOGIN_LOCK_SECONDS`)
- Test: `tests/test_vds_acceptance_scripts.py` (ожидание `TAKSKLAD_BACKEND_INTERNAL_URL` в `test_frontend_uses_same_origin_api_proxy_contract`,
  строка 314; два счётчика в `test_web_deploy_forces_https_security_headers`; новый тест в том же классе)

`telegram-worker` тоже держит `TAKSKLAD_BACKEND_INTERNAL_URL: http://backend-api:8000`, но стоит только в сети
`taksklad-internal`, где псевдонима нет; его не трогать

**Interfaces:**
- Consumes: маршрут `POST /api/v1/auth/station` из Task 3
- Produces: в production фронт ходит в backend по `http://taksklad-backend-api:8000`; `/api/v1/auth/station` проходит nginx без
  проверки сессии; `/api/v1/auth/check` получает `X-Forwarded-For` от nginx явно

`tools/container_runtime_harness.py:546` (`TAKSKLAD_BACKEND_INTERNAL_URL=http://backend-api:8000`) не менять: харнесс поднимает
свою одиночную сеть, где псевдонима нет и он не нужен

- [ ] **Step 1: Write the failing test**

В `tests/test_vds_acceptance_scripts.py`, в `test_frontend_uses_same_origin_api_proxy_contract`, заменить
`self.assertIn("TAKSKLAD_BACKEND_INTERNAL_URL: http://backend-api:8000", compose)` на:

```python
        self.assertIn("TAKSKLAD_BACKEND_INTERNAL_URL: http://taksklad-backend-api:8000", compose)
```

и добавить в класс `VdsAcceptanceScriptsTests` новый тест:

```python
    def test_station_login_route_and_trusted_backend_path(self):
        compose = (PROJECT_ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        nginx = (PROJECT_ROOT / "frontend" / "nginx.conf.template").read_text(encoding="utf-8")

        def location_block(marker):
            start = nginx.index(marker)
            return nginx[start:nginx.index("\n  }\n", start)]

        station = location_block("location = /api/v1/auth/station {")
        self.assertNotIn("auth_request", station)
        self.assertIn("proxy_pass $taksklad_backend;", station)
        self.assertIn("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", station)
        self.assertIn('proxy_set_header Authorization "";', station)

        auth_check = location_block("location = /_taksklad_auth_check {")
        self.assertIn("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", auth_check)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", auth_check)

        api = location_block("location /api/ {")
        self.assertIn("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", api)

        self.assertIn("TAKSKLAD_WAREHOUSE_CIDRS: ${TAKSKLAD_WAREHOUSE_CIDRS:-}", compose)
        self.assertIn("          - taksklad-backend-api\n", compose)
        frontend_service = compose[compose.index("\n  frontend:\n"):compose.index("\n  skladbot-worker:\n")]
        self.assertIn("TAKSKLAD_BACKEND_INTERNAL_URL: http://taksklad-backend-api:8000", frontend_service)
```

в `test_web_deploy_forces_https_security_headers` два счётчика с 4 на 5 (новый блок станции добавляет по одной строке):

```python
        self.assertEqual(nginx.count("proxy_pass $taksklad_backend;"), 5)
```

```python
        self.assertEqual(nginx.count("proxy_set_header X-Forwarded-Proto https;"), 5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/sklad-web-station && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_vds_acceptance_scripts -v`
Expected (прогнано 02.10): `test_frontend_uses_same_origin_api_proxy_contract` FAIL с
`AssertionError: 'TAKSKLAD_BACKEND_INTERNAL_URL: http://taksklad-backend-api:8000' not found in …` (печатает весь compose,
в нём только ссылки на переменные, значений нет), `test_station_login_route_and_trusted_backend_path` ERROR с
`ValueError: substring not found` (блока станции в nginx нет); `test_web_deploy_forces_https_security_headers` на этом шаге
ещё зелёный и краснеет `5 != 4`, если по ошибке сначала поправить nginx, а счётчики оставить

- [ ] **Step 3: Write minimal implementation**

`frontend/nginx.conf.template`, сразу после блока `location = /api/v1/auth/session { … }`:

```nginx
  location = /api/v1/auth/station {
    proxy_pass $taksklad_backend;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Authorization "";
  }
```

в блоке `location = /_taksklad_auth_check` после `proxy_set_header X-Original-Host $host;`:

```nginx
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

`deploy/vds/docker-compose.yml`, в `backend-api.environment` сразу после строки `TAKSKLAD_TRUSTED_PROXY_CIDRS: …`:

```yaml
      TAKSKLAD_WAREHOUSE_CIDRS: ${TAKSKLAD_WAREHOUSE_CIDRS:-}
```

`backend-api.networks` заменить списком с псевдонимом (отступы как у соседних ключей сервиса):

```yaml
    networks:
      taksklad-internal:
      traefik:
        aliases:
          - taksklad-backend-api
```

`frontend.environment`:

```yaml
      TAKSKLAD_BACKEND_INTERNAL_URL: http://taksklad-backend-api:8000
```

`deploy/vds/.env.example`, после `TAKSKLAD_WEB_LOGIN_LOCK_SECONDS=900`:

```text
# Адреса сети склада для станции без пароля, через запятую; пусто значит станция выключена
TAKSKLAD_WAREHOUSE_CIDRS=
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /tmp/sklad-web-station && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_vds_acceptance_scripts tests.test_container_policy tests.test_principal_provisioning_guards tests.test_compose_test_config -v 2>&1 | tail -4`
Expected: `Ran 37 tests` `OK` (прогнано 02.10)

Затем синтаксис compose, только код возврата, вывод `compose config` секретный и не печатается (`~/.claude/CLAUDE.md`):

```bash
cd /tmp/sklad-web-station/deploy/vds
TAKSKLAD_BACKEND_IMAGE=x TAKSKLAD_FRONTEND_IMAGE=y TAKSKLAD_TRUSTED_PROXY_CIDRS=172.18.0.0/16 \
  docker compose -f docker-compose.yml --env-file .env.example config --quiet >/dev/null 2>&1; echo "rc=$?"
```

Expected: `rc=0` (на копии с псевдонимом и на базе оба 0, 02.10)

- [ ] **Step 5: Commit**

```bash
cd /tmp/sklad-web-station
git add frontend/nginx.conf.template deploy/vds/docker-compose.yml deploy/vds/.env.example tests/test_vds_acceptance_scripts.py
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(deploy): маршрут входа станции в nginx и путь фронта к backend через доверенную сеть"
```

---

### Task 5: Полный прогон, сверка с базой и PR

**Files:** нет новых правок; проверка и доставка

- [ ] **Step 1: Полный прогон unit-тестов против базы**

Run: `cd /tmp/sklad-web-station && TAKSKLAD_PYTHON_BIN=/Users/anton/Documents/work/TakSklad/.venv/bin/python PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest discover -s tests > /tmp/station-plan1-tests.log 2>&1; tail -3 /tmp/station-plan1-tests.log`
Expected: `Ran 2247 tests` `OK (skipped=94, expected failures=4)`, снято прогоном на копии `3d75e86` с кодом этого плана 02.10;
база на `3d75e86` в тот же день `Ran 2230 tests` `OK (skipped=94, expected failures=4)`, разница это 16 тестов
в `tests.test_backend_station_access` и 1 в `tests.test_vds_acceptance_scripts`
Полный прогон запускать в одиночку: `tests.test_returns_auth_canary` берёт общий файловый замок
(`credential_lock.acquire_credential_mutation_lock`), и два параллельных прогона роняют его и
`tests.test_backend_skladbot_request_dry_run` (наблюдалось 02.10, по отдельности оба зелёные)

- [ ] **Step 2: Матрица Postgres**

Run: `cd /tmp/sklad-web-station && TAKSKLAD_TEST_PYTHON=/Users/anton/Documents/work/TakSklad/.venv/bin/python ./tools/run_postgres_tests.sh all`
Expected: зелёный, включая `tests.test_postgres_rbac_audit` со `station: 64`; после прогона откатить побочные артефакты
`git checkout -- test-artifacts/` (скрипт переписывает `test-artifacts/disaster-recovery/*.json`)

- [ ] **Step 3: Проверка состава коммитов**

Run: `git -C /tmp/sklad-web-station log --oneline origin/main..HEAD && git -C /tmp/sklad-web-station show --stat HEAD~2..HEAD`
Expected: коммит спецификации и три коммита плана, только файлы, названные в задачах

- [ ] **Step 4: Push и PR, только по разрешению Антона**

```bash
cd /tmp/sklad-web-station
ALLOW_NON_MAIN_BRANCH=1 git push -u origin feat/warehouse-station-web
gh pr create --title "Вход станции склада без пароля из сети склада (план 1)" --body-file /tmp/station-plan1-pr.md
```

Тело PR: что сделано по задачам 1-4, хвост вывода Step 1 и Step 2, строка «станция выключена: `TAKSKLAD_WAREHOUSE_CIDRS` пустой»,
в конце `🤖 Generated with [Claude Code](https://claude.com/claude-code)`; мерж `gh pr merge --squash --delete-branch` тоже по разрешению

## После мержа: выкатка с выключенной станцией

Порядок выката и квиесценции из `docs/CURRENT_STATUS.md` (раздел «Release-контур») и `docs/deploy-rollback-runbook.md`,
памятка проекта «Ручной выкат без Actions»; каждый шаг по отдельному разрешению Антона

| Исход | Что видно | Что делать |
|---|---|---|
| backend с пустым `TAKSKLAD_WAREHOUSE_CIDRS` | `POST /api/v1/auth/station` даёт 403 всем, `/admin` и программа работают как раньше | ничего |
| порядок | `backend-api` пересоздаётся первым или одной командой с фронтом, фронт после; отдельный выкат только фронта до `backend-api` даёт 502 на весь `/api/` chapman | соблюдать порядок |
| фронт с новым `TAKSKLAD_BACKEND_INTERNAL_URL` | `/api/v1/auth/session` через chapman 200, админка входит | при 502 пересоздать `backend-api` по новому compose (появится псевдоним) или вернуть фронту `TAKSKLAD_BACKEND_INTERNAL_URL: http://backend-api:8000`; прежний образ фронта 502 не лечит, адрес берётся из compose |
| объём волны | новых записей во внешние системы нет, пользователь станции не создаётся, пока станция выключена | |

Живая проверка, только чтение (`ctx_execute`, сеть отдельным вызовом):
1. `POST https://chapman.taksklad.uz/api/v1/auth/station` с заголовком `Origin: https://chapman.taksklad.uz` с машины агента: 403 `station_network_denied`
2. сразу после пересоздания фронта `GET https://chapman.taksklad.uz/api/v1/auth/session` отвечает 200
3. то же, что в пункте 1, с `X-Forwarded-For: <адрес склада>`: 403
4. положительный путь только по разрешению Антона: адрес машины агента на время вносится в `TAKSKLAD_WAREHOUSE_CIDRS`,
   `backend-api` пересоздаётся, вход даёт 200 и `/api/v1/auth/check` с полученной cookie 204, затем адрес убирается тем же путём
   и проверка 1 повторяется
5. PR статуса в `docs/CURRENT_STATUS.md`
6. в день пилота первым делом взять из журнала traefik адрес, с которого пришёл `POST /api/v1/auth/station` с ПК склада, и сверить со списком; у chapman.taksklad.uz записи AAAA нет (проверено 02.10), семейство IPv4
