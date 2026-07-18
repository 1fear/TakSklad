# Deploy And Rollback Runbook

Дата фиксации: 2026-06-30.

Документ описывает минимальную эксплуатационную процедуру TakSklad 2.0 на VDS. Секреты, пароли, токены и backup-файлы в Git не хранятся.

## 1. DNS И HTTPS

Целевой домен backend:

```text
api.taksklad.uz -> 159.195.138.95
```

На стороне DNS-регистратора нужна A-запись:

```text
type: A
name: api
value: 159.195.138.95
ttl: 300
```

Текущий статус на 2026-06-30:

- `api.taksklad.uz` и `taksklad.uz` резолвятся в `159.195.138.95`;
- production app path на сервере: `/opt/stacks/taksklad/app`;
- старый путь `/opt/taksklad/app` и старый IP `135.181.245.84` не использовать для новых deploy.

После обновления DNS на VDS в `deploy/vds/.env` должно быть:

```text
TAKSKLAD_BACKEND_HOST=api.taksklad.uz
```

Для переключения после готового DNS на VDS:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/switch_backend_host.sh api.taksklad.uz
```

Если нужно открыть Adminer через отдельный домен:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/switch_backend_host.sh api.taksklad.uz adminer.taksklad.uz
```

Проверка:

```bash
dig +short api.taksklad.uz
curl -fsS https://api.taksklad.uz/health
curl -fsS https://api.taksklad.uz/ready
```

Fallback-проверка по текущему IP:

```text
https://api.159.195.138.95.sslip.io/health
```

## 2. Deploy Backend

Перед любым production deploy:

```bash
cd /Users/anton/Documents/work/TakSklad
git status --short
git diff --name-only
```

Dirty worktree не является источником production delivery. Разрешены только exact-SHA control bundle и immutable release assets, проверенные по manifest/attestation; source-файлы из checkout не копируются.

Production activation использует только verified `release.json`, exact source SHA,
immutable `image@sha256` и заранее созданный rollback record. Копирование source tree,
локальная сборка на VDS и широкий `rsync` запрещены. До activation создаётся свежий
backup; после activation проверяются `/health`, `/ready` и data-free auth canary.

Если VDS database еще не stamped на baseline `20260616_0001`, сначала пройти `docs/database-migrations-runbook.md`. `deploy/vds/apply_schema.sh` не использовать для обычных production upgrades после baseline stamp; он остается только для legacy/bootstrap сценариев пустой БД.

Если DNS временно недоступен, fallback-проверка:

```bash
curl -fsS https://api.159.195.138.95.sslip.io/health
```

## 2.1 Controlled CI/CD

GitHub CI/CD настроен в безопасном режиме:

- `.github/workflows/ci.yml` запускается на `push main`, `pull_request main` и вручную;
- `.github/workflows/deploy-production.yml` запускается только вручную через `workflow_dispatch`;
- обычный `git push` не деплоит production;
- production deploy должен проходить через GitHub Environment `production`, где можно включить required reviewers.

CI проверяет:

```bash
PYTHONPATH=. python -m compileall -q backend/app backend/migrations tools tests
PYTHONPATH=. python -m unittest discover -s tests
PYTHONPATH=. python -m alembic -c backend/alembic.ini heads
TAKSKLAD_ENV_FILE=.env.example docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config --quiet
npm ci --prefix frontend
npm --prefix frontend run build
```

Для production deploy в GitHub Secrets нужны:

```text
VDS_HOST
VDS_USER
VDS_SSH_KEY
VDS_SSH_KNOWN_HOSTS
VDS_APP_DIR
WINDOWS_CODESIGN_PFX_BASE64
WINDOWS_CODESIGN_PFX_PASSWORD
```

`VDS_APP_DIR` можно не задавать, если production app лежит в стандартном пути `/opt/stacks/taksklad/app`. `VDS_SSH_KNOWN_HOSTS` должен содержать known_hosts строку сервера; не использовать `StrictHostKeyChecking=no`.

Windows signing secrets должны соответствовать заранее закреплённому SHA-256 публичного сертификата в `TRUSTED_WINDOWS_SIGNER_CERT_SHA256`. Сам PFX, пароль и private key не сохраняются в repository, release manifest, artifacts или логах.

Для внутреннего сертификата TakSklad публичные копии root CA и code-signing leaf хранятся в `supply-chain/taksklad-internal-windows-root-ca.pem` и `supply-chain/taksklad-internal-windows-codesign.pem`. Release runner сверяет leaf с PFX, временно добавляет root CA в пользовательский `Root`, а leaf — в `TrustedPublisher`, после сборки удаляя оба. Перед установкой подписанного desktop-релиза оператор должен импортировать root CA в `Trusted Root Certification Authorities`, а leaf — в `Trusted Publishers`; приватные ключи на рабочие станции не переносятся.

### Immutable release candidate

Версия кандидата и уже опубликованный update channel разделены. Во время подготовки candidate `APP_VERSION=2.0.48`, пока корневой `version.json` продолжает указывать на текущий поддерживаемый channel. Это не утверждает, что `2.0.48` уже опубликована.

Безопасная последовательность:

1. получить один финальный candidate SHA и трижды пройти Phase 26 без production;
2. прогнать `CI / Release gate` на этом SHA во временной release-ветке;
3. применить защиту `main` и Environment `production` без bypass;
4. fast-forward отправить тот же SHA в `main` и дождаться exact-SHA CI;
5. создать новый тег `v<APP_VERSION>` и пустой draft release;
6. `Build Immutable Release` проверяет CI identity, подписывает Windows-файлы, один раз публикует digest-only образы, attestations и unified `release.json`;
7. после проверки всех attestations draft публикуется, а update channel обновляется реальными production-хешами отдельным контролируемым promotion-шагом;
8. production deploy принимает только GitHub/Sigstore manifest, создаёт свежий backup, выполняет count-only preflight, forward-only migration, digest activation и пятиминутный read-only SLO window.

Существующий тег или asset никогда не передвигается и не перезаписывается. `--clobber`, source build на VDS, schema downgrade, restore и автоматический data repair запрещены.

Production получает только exact-SHA control bundle и immutable `image@sha256`, связанные с verified `release.json`; source checkout/build на VDS не является rollback или delivery contract.

Ручной запуск:

1. GitHub -> Actions -> `Deploy Production`.
2. `ref`: обычно `main`.
3. `services`: `all` или список compose-сервисов через пробел/запятую.
Production workflow запускается только вручную. Acceptance не имеет bypass-режима и всегда обязателен.

До запуска deploy оператор отдельно provisioned acceptance principal exact kind `acceptance` + scope `returns:read` и его token file в `/opt/stacks/taksklad/private/acceptance-canary.token`. Файл и защищённый parent принадлежат текущему deploy user; file mode только `0400/0600`. Token не передаётся через GitHub secret/env/argv и не печатается. Скрипт до pull/quiesce проверяет owner/mode/parent, bounded scoped format и наличие verified previous deployment record.

### Manual P0 bridge для principal

`Deploy Production` не управляет lifecycle principals. До первого adoption допускается только отдельный manual P0 bridge под явным prod-write gate родителя. На trusted admin host сначала доказать immutable tag → exact main SHA и GitHub/Sigstore attestations `release.json` + exact backend OCI digest. На VDS exact `image@sha256` должен быть уже локально staged; one-shot использует `--pull never` и не доверяет ambient registry credentials.

До передачи чего-либо на VDS trusted admin checkout должен быть чистым на exact tag/main SHA. Скачать release assets в новый каталог и выполнить существующий full verifier; он проверяет manifest, OCI/Windows attestations с exact source digest и ожидаемым release workflow:

```bash
source_sha="<exact-40-lowercase-main-sha>"
release_tag="v2.0.48"
git fetch --tags origin main
test "$(git rev-parse origin/main)" = "$source_sha"
test "$(git rev-list -n 1 "$release_tag")" = "$source_sha"
release_dir="$(mktemp -d /tmp/taksklad-p0-release.XXXXXX)"
chmod 700 "$release_dir"
gh release download "$release_tag" --dir "$release_dir"
TAKSKLAD_RELEASE_MANIFEST="$release_dir/release.json" \
TAKSKLAD_RELEASE_ARTIFACT_DIR="$release_dir" \
./tools/verify_release_attestations.sh --sha "$source_sha"
```

Только digest из прошедшего verifier manifest передаётся на VDS. Exact image staging выполняется отдельно с новым per-run `DOCKER_CONFIG`, credential через `docker login --password-stdin`, затем `docker pull image@sha256`, `docker image inspect` и обязательные logout/удаление только этого owned temp-каталога. Ambient Docker credentials, tag без digest и caller-created «verified» stamp запрещены. Любая ошибка verification или cleanup — stop до one-shot.

После проверки release authority создать новый стабильный UUID операции и свежий operation-bound backup. Result filename должен быть новым; повтор той же операции использует тот же UUID, но новый result filename/backup:

```bash
operation_id="<stable-uuid-approved-for-this-action>"
backup_result="/run/taksklad-observability/principal-backup-${operation_id}.json"
TAKSKLAD_BACKUP_OPERATION_ID="$operation_id" \
TAKSKLAD_BACKUP_RESULT_FILE="$backup_result" \
./deploy/vds/backup_postgres.sh --no-prune
backup_archive="$(python3 - "$backup_result" <<'PY'
import json, sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
path = str(value.get("archive_path") or "")
if not path.startswith("/"):
    raise SystemExit(1)
print(path)
PY
)"
```

Только после отдельного подтверждения backup/PITR и exact action заполнить non-secret bindings. Пример формы для canonical acceptance provision; реальные SHA/digest/UUID сюда не копировать:

```bash
source_sha="<exact-40-lowercase-main-sha>"
release_tag="v2.0.48"
backend_image="ghcr.io/1fear/taksklad-backend@sha256:<exact-attested-digest>"
export TAKSKLAD_PRINCIPAL_BACKUP_ROOT=/opt/taksklad/backups/postgres/completed
export TAKSKLAD_PRINCIPAL_BACKUP_RESULT_FILE="$backup_result"
export TAKSKLAD_PRINCIPAL_BACKUP_ARCHIVE_FILE="$backup_archive"
export TAKSKLAD_PRINCIPAL_WRITE_APPROVAL=ALLOW_SERVICE_PRINCIPAL_WRITE
export TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL=PROVISION_ACCEPTANCE_PRINCIPAL
export TAKSKLAD_MANUAL_P0_RELEASE_AUTHORITY="VERIFIED_TAGGED_MAIN_RELEASE:${release_tag}:${source_sha}:${backend_image}"
export TAKSKLAD_MANUAL_P0_BRIDGE_APPROVAL="MANUAL_P0_BRIDGE:provision:acceptance:acceptance.release:${operation_id}:${source_sha}:${release_tag}:${backend_image}:BACKUP:${operation_id}"
./deploy/vds/provision_service_principal.sh \
  "$backend_image" provision acceptance acceptance.release \
  "$operation_id" "$source_sha" "$release_tag"
```

Shell до DB mutation повторно проверяет actual archive bytes/SHA/freshness/UUID/head, exact local image digest и единственный image Alembic head == live DB head. Для одного UUID создаётся новая internal network; к ней временно подключаются только exact PostgreSQL и one-shot provisioner, после чего сеть всегда отключается/удаляется. Обычный compose не оставляет PostgreSQL в admin network; pre-existing network блокирует операцию и не удаляется. Writers не останавливаются. Success печатается только после cleanup one-shot container/network. Нельзя вручную move/copy/delete plaintext handoff. Acceptance rotate выполняет atomic replace; desktop staging уничтожается только `destroy-handoff` после signed Windows DPAPI canary. Revoke DB-first; `cleanup=unverified` или `.token.*` residue означает partial state, nonzero и escalation, но principal обратно не активируется.

После candidate ready/acceptance/log scan, но до atomic current-release record, deploy вызывает data-free acceptance endpoint и требует exact `204`. Ошибка выполняет schema-compatible runtime rollback без `alembic downgrade`, затем сверяет previous image IDs, SHA/digest и public health/ready. Только legacy first-adoption без endpoint допускает exact `404` через совместные literal `--allow-missing-endpoint --require-missing-endpoint` и отдельный exact approval; legacy `204` запрещён, потому что не доказывает identifier-aware v2 contract. Missing previous record блокирует mutation; bootstrap без rollback требует отдельного явного флага/approval и не используется workflow по умолчанию.

Разрешенные сервисы для rebuild/recreate:

```text
backend-api frontend telegram-worker skladbot-worker smartup-auto-import-worker
```

Серверный скрипт `deploy/vds/deploy_from_git.sh` выполняет:

1. отказывается деплоить при tracked changes на VDS checkout;
2. создает restore point без `outputs`, `.env`, credentials и backup-файлов;
3. запускает `deploy/vds/backup_postgres.sh`;
4. проверяет exact-SHA control bundle и immutable image digests из release manifest;
5. активирует только заранее проверенный `image@sha256` без source build;
6. `alembic -c alembic.ini upgrade head`;
7. read-only сверяет единственный `alembic current` с единственным `alembic heads` до активации;
8. `docker compose up -d --no-build --wait --wait-timeout ...` для выбранных immutable services;
9. проверяет JSON-контракт `/health` с retry;
10. проверяет JSON-контракт `/ready`: database/migrations/head/mandatory policy обязаны быть готовы;
11. обязательно запускает `deploy/vds/acceptance_status.sh --require-go`;
12. выполняет fresh log scan по rebuilt/recreated сервисам.

Любой missing/no-go acceptance, несовпадение migration head, нездоровая БД или обязательная очередь останавливают deploy. `/health` остаётся lightweight-проверкой процесса; `/ready` возвращает HTTP 503 при обязательном отказе.

## 3. Backup

Ручной backup:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/backup_postgres.sh
```

Проверка расписания:

```bash
systemctl list-timers 'taksklad-postgres-backup.timer'
systemctl status taksklad-postgres-backup.timer
```

## 4. Restore Drill

Restore-drill всегда выполняется в отдельную временную БД и не трогает production database.

```bash
cd /opt/stacks/taksklad/app
latest="$(ls -t /opt/taksklad/backups/postgres/taksklad-postgres-*.sql.gz | head -1)"
./deploy/vds/restore_drill.sh "$latest"
```

Успешный результат должен показать таблицы `orders`, `order_items`, `scan_codes`, `imports` и строку `restore_drill_ok`.

## 5. Production Restore

Production restore перезаписывает текущую БД. Запускать только при осознанном откате данных.

```bash
cd /opt/stacks/taksklad/app
CONFIRM_RESTORE=YES ./deploy/vds/restore_postgres.sh /opt/taksklad/backups/postgres/taksklad-postgres-YYYYmmddTHHMMSSZ.sql.gz
```

После restore:

```bash
curl -fsS https://api.taksklad.uz/health
```

## 6. Rollback Backend Runtime

Rollback выбирает только exact previous manifest из protected current-release record,
повторно проверяет image digests и активирует предыдущий immutable bundle без build,
checkout или синхронизации source tree. После rollback обязательны exact runtime
identity и public `/health` + `/ready`; несоответствие означает `rollback_unverified`.

Rollback после backend-only hot path не должен удалять pending events. До rollback сохранить:

```bash
curl -fsS https://api.taksklad.uz/ready > /tmp/taksklad-ready-before-rollback.json
curl -fsS -H "Authorization: Bearer <service-token-from-secret-storage>" \
  https://api.taksklad.uz/api/v1/admin/events > /tmp/taksklad-events-before-rollback.json
curl -fsS -H "Authorization: Bearer <service-token-from-secret-storage>" \
  https://api.taksklad.uz/api/v1/admin/operations > /tmp/taksklad-operations-before-rollback.json
```

После rollback повторить эти же три проверки. Допустимо уменьшение pending events только если есть audit/sync evidence. Недопустимо внезапное исчезновение `google_sheets_export`, `telegram_excel_import`, scan/order-complete events или open incidents без понятной причины.

### Rollback После Production Hardening 2.0.x

Перед откатом:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/backup_postgres.sh
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml ps
curl -fsS https://api.taksklad.uz/health
```

Откат выполняет deploy transaction по exact previous manifest. Команда не собирает
образы и не добавляет retired workers: запускаются только сервисы, перечисленные в
проверенном previous manifest, по их exact digests.

Если после релиза уже применялись Alembic migrations, downgrade БД нельзя делать автоматически вместе с кодом. Сначала выполнить backup, затем отдельно проверить конкретный migration downgrade plan. Если сомневаешься, откатывать только код, а БД оставлять на текущей схеме до ручного решения.

## 7. Release Safety

До ручной приёмки нельзя:

- менять `version.json`;
- отправлять desktop push-update;
- собирать и выкладывать Windows archive как обязательное обновление;
- ослаблять backend-only desktop auth или возвращать local/Google fallback.

Release guard для backend-only:

- `TAKSKLAD_BACKEND_ONLY_REFRESH=1` включать сначала только на одном Windows workstation/test profile;
- `TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED=0` должен быть default;
- `TELEGRAM_DESKTOP_POLLING_ENABLED=0` должен оставаться default, нормальный Telegram listener - backend worker;
- Windows startup diagnostics должны показать `telegram_desktop_polling=no`, `backend_only_refresh=yes`, `backend_emergency_google_fallback=no`;
- backend `/api/v1/admin/operations` должен показать `shadow_diagnostics` без hot-path blocker перед расширением rollout.

## 8. Acceptance Cleanup

Тестовые acceptance-данные удалять только по явному маркеру.

Dry-run:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"
```

Удаление:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --apply
```

Защита скрипта: marker должен содержать `ACCEPTANCE`, `WEB_UI_SMOKE` или `SMOKE_MVP`.
