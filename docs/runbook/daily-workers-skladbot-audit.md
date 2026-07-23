# Daily Workers SkladBot Audit

Дата аудита: 2026-07-06, Asia/Tashkent.

Verdict: `AUDIT_READY_FOR_REVIEW`.

## 1. Executive summary

Аудит выполнен read-only по текущему worktree на ветке `main`. Код не исправлялся, deploy не запускался, внешние отправки не выполнялись.

Главный вывод: текущий worktree уже содержит защиту от части старых проблем daily SkladBot: page-based `/requests` crawl, coverage status, diagnostic sheets, scheduled block для partial/failed, stale processing fail-safe и registry после успешной scheduled document-send. Но остались места, где daily report может быть неполным или вводящим в заблуждение без жесткого scheduled-блока:

- SkladBot POST-вызовы существуют: daily report использует read-style POST для movements/stock/products, а отдельные workers умеют создавать SkladBot requests. По правилу этого аудита это P0-зона, даже если daily report сам не вызывает create.
- Movements/products/stock читаются без pagination/total guard. Если SkladBot вернет больше лимита, отчет может быть `complete`, но данные будут усечены.
- Conflict `unloading_date != movement_date` попадает в date diagnostics, но included operational request может остаться в итогах без coverage warning.
- Same-day scheduled send idempotency фиксирован по date/chat/mode/kind/version `v2`; failed/completed событие блокирует auto-send исправленного отчета за тот же день.
- Manual `/skladbot_daily` ведет себя мягче scheduled: может отправить partial report с warning, scheduled partial блокируется.
- Production logs в этом запуске не читались live; использованы только локальные sanitized runbook-артефакты из `.supergoal`.

## 2. Scope and non-actions

Проверено:

- `.supergoal/daily-skladbot-bvytBP/ROADMAP.md` и локальные result/status артефакты.
- `backend/app/skladbot_daily_report.py`.
- `backend/app/telegram_worker.py`.
- SkladBot worker/create/return paths.
- Unit tests for daily report and Telegram import worker.
- `docs/report-source-rules.md`, `docs/changelog.md`, `README.md`, `backend/README.md`.
- `deploy/vds/docker-compose.yml`, `deploy/vds/.env.example`, `.github/workflows/*`.
- local generated/report-like artifact paths.

Non-actions:

- Code changed: NO.
- Existing docs changed: NO.
- New audit report created: YES, this file only.
- Telegram sent: NO.
- SkladBot write calls executed: NO.
- Production deploy: NO.
- Production migrations: NO.
- Commit: NO.
- Push: NO.
- Values from secret-like files printed: NO.

Preflight facts:

- cwd: `/Users/anton/Documents/work/TakSklad`.
- branch: `main`.
- HEAD: `c7b3ecffda55ff3ae7ff4e3bc8b2edebe5c06866`.
- worktree: dirty before this audit; daily-scope files already modified before this audit include `backend/app/skladbot_daily_report.py`, `backend/app/telegram_worker.py`, `tests/test_skladbot_daily_report.py`, `tests/test_backend_telegram_import.py`, `docs/report-source-rules.md`, `docs/changelog.md`, `docs/implementation-log.md`.
- unrelated dirty/untracked files exist and were not touched.
- `.venv/bin/python`: present, Python 3.12.13.
- root `Makefile`: not present.
- production/runtime artifact dirs in repo: `outputs/`, `reports/`, `scan_backups/`, `archive/`, `Сверка/`.

## 3. Worker/job map

| Worker/job | File/function | Trigger | Frequency | Inputs | Outputs | Side effects | Idempotency | Tests | Risks |
|---|---|---|---|---|---|---|---|---|---|
| Telegram worker loop | `backend/app/telegram_worker.py:2791` `main`, `poll_once` | docker service `telegram-worker` | loop, sleep on failure | bot updates, PendingEvent queue, env config | messages/documents, imported Excel, daily report | Sends Telegram, imports files, updates local DB events | per event type | `tests/test_backend_telegram_import.py`, `tests/test_skladbot_daily_report.py` | One process owns manual commands and scheduled daily; bugs affect both paths |
| SkladBot scheduled daily send | `send_due_skladbot_daily_reports` | called from `poll_once` after queued imports/notifications | once due time per chat/day | business date, configured chats | Telegram message + XLSX | writes local PendingEvent send status, reported registry, reconciliation after success | send event key date/chat/mode/kind/v2 | daily report tests | Failed/completed same-day key blocks corrected auto-send |
| Manual daily command | `/skladbot_daily`, `/skladbot_report` in `handle_text` | admin command | manual | optional date | message + XLSX | sends via safe wrappers | no scheduled send event | daily report tests | Can send partial report with warning; not same behavior as scheduled |
| Queued Telegram notifications | `process_pending_telegram_notifications` | `poll_once` | each loop | `telegram_notification` PendingEvent | Telegram messages | marks local event status | PendingEvent status | Telegram import tests | Queue failures can retry/accumulate |
| Queued Telegram Excel imports | `process_queued_telegram_imports` | `poll_once` | each loop | uploaded document event | import jobs/orders | writes local DB and may enqueue downstream actions | PendingEvent status | Telegram import tests | Separate from daily but shares same worker |
| SkladBot worker | `backend/app/skladbot_worker.py:1253` `main` | docker service `skladbot-worker` | interval, min 60s | DB orders/events, SkladBot API | local sync, request-create processing | can create SkladBot requests through create workers | create-event keys and recovery lookup | SkladBot worker/create tests | Real SkladBot write path exists outside daily report |
| SkladBot order create queue | `process_pending_skladbot_request_creates` / `client.create_request` | SkladBot worker | interval | local pending create events | SkladBot request | SkladBot write | local event + request recovery lookup | create tests | P0-by-audit-rule POST/write capability |
| SkladBot return create queue | `process_pending_skladbot_return_request_creates` / `client.create_request` | SkladBot worker | interval | return pending events | SkladBot return request | SkladBot write | local event + recovery lookup | return tests | P0-by-audit-rule POST/write capability |
| Smartup auto import worker | `backend/app/smartup_auto_import_worker.py:21` | docker service | configured slots | Smartup/export config, DB | imports/events/notifications | can write local DB; configured flags decide behavior | Smartup events | smartup tests | Out of daily SkladBot scope, but can enqueue related actions |
| Google Sheets sync worker | `backend/app/google_sheets_sync_worker.py:764` | docker service | interval, min 30s | DB and Sheets config | Sheets/backend sync | local/Sheets sync depending config | worker cycle state | sync tests | Shares operational data surface |
| CI workflow | `.github/workflows/ci.yml` | push/PR/manual | GitHub event | repo checkout | test/build status | no app data write | workflow concurrency | CI config | Does not verify live schedule |
| Production deploy workflow | `.github/workflows/deploy-production.yml` | manual dispatch | manual | GitHub secrets, ref | deploy to VDS | production deploy/restart | workflow concurrency | workflow preflight | Not run in this audit |
| Postgres backup timer | `deploy/vds/install_backup_timer.sh` | systemd timer | configured by install script | DB | backup files | reads DB, writes backup | timer/service | script syntax via CI | Not daily notification path |

Who does what:

- Sends Telegram: `TelegramWorker.send_message`, `send_document`, queued notification path, manual daily path, scheduled daily path.
- Builds report only: `backend/app/skladbot_daily_report.py`.
- Reads SkladBot for daily: daily report collector via `SkladBotClient`.
- Writes daily registry: `mark_skladbot_daily_report_requests_reported`, only after scheduled document success.
- Runs reconciliation after daily: `run_scheduled_daily_reconciliation`, only after scheduled success.
- Manual and scheduled modes differ: scheduled blocks partial/failed before XLSX/send; manual can send a warning report.

## 4. Daily report data flow

| Step | File/function | Fields/data | Error handling | Partial logic | Telegram | XLSX | Logging/tests |
|---|---|---|---|---|---|---|---|
| Start report | `collect_skladbot_daily_report` | report date, customer, request types | missing SkladBot auth returns failed coverage | failed if no usable API | no direct send | report object | tested |
| Request list crawl | `crawl_daily_request_list_pages` | `/requests`, `page`, `limit`, type | list errors stored in errors/api_errors | list error, max pages, repeated page mark partial | scheduled blocks partial | coverage/errors sheets | page crawl tests |
| Detail fetch | `fetch_daily_requests` + `get_daily_request_detail` | request id/detail payload | detail errors stored and excluded | detail errors/detail limit mark partial | scheduled blocks partial | excluded/errors/diagnostics | detail failure/limit tests |
| Date scope | `apply_request_scope` | `unloading_date`, movement date, created/completed/archived dates | no exception path | diagnostics are not always partial | summary counts excluded | date diagnostics sheet | date scenario tests exist, conflict missing |
| Status filter | `request_is_completed_and_archived` | completed + archived | non-matching rows excluded | status diagnostics counted for excluded | excluded count visible | excluded/date diagnostics | tests for status cases |
| Product rows | request products + identity keys | vendor/barcode/name/amount | missing data stays in row | no separate partial | included only | request product sheet | sheet tests |
| Movements | `fetch_daily_movements` | movement date, request number, amount | movement API error goes to errors | errors become partial; truncation is not detected | movement totals visible | movements sheet | limited tests |
| Stock | `fetch_current_stock` | products/stock report | errors go to errors | errors become partial; truncation is not detected | stock total visible | stock sheet | limited tests |
| Workbook | `build_skladbot_daily_report_xlsx` | summary, requests, products, movements, stock, diagnostics, errors | generated from report object | partial workbook still valid | file sent only after scheduled block | 9 sheets | sheet tests |
| Message | `build_skladbot_daily_report_message` | date, scope, coverage, included/excluded/errors/totals | no exception handling here | warns if coverage not complete/errors/warnings | visible text | no | message tests |
| Scheduled send | `send_skladbot_daily_report` | report object + chat | raises blocker before XLSX/send | blocked if not complete/errors/0 included with excluded | send only after block passes | send after message | scheduled tests |
| Registry | `mark_skladbot_daily_report_requests_reported` | included requests only | skips existing keys | no retry marker before send | after document success | no | registry tests |
| Reconciliation | `run_scheduled_daily_reconciliation` | report date/chat | catches and logs failed result | does not alter report send result | after scheduled success | no | reconciliation tests |

## 5. SkladBot API usage

| Endpoint | Method | Used by | Read/write | Pagination | Retry | Error handling | Risk |
|---|---|---|---|---|---|---|---|
| `/requests` | GET | daily request list crawl | read | page + limit, max-page guard, duplicate page/id guard | `SkladBotClient.get` retries timeout/429/5xx | list error -> partial/failed | Low after current fix |
| `/requests/show/{id}` | GET | daily detail fetch | read | per id | client GET retry plus daily 429 wrapper | detail error -> excluded + partial | Medium if detail budget too low |
| `/warehouse/transactions` | POST | daily movements | read-style query | no pagination in current code | client POST does not retry 429/5xx | error -> partial; truncation not detected | P0/P1 |
| `/products` | POST | daily stock fallback/source | read-style query | fixed limit 1000, no paging | client POST does not retry 429/5xx | error recorded; truncation not detected | P0/P1 |
| `/report/stock` | POST | daily stock | read-style query | no paging | client POST does not retry 429/5xx | error recorded | P1 |
| `/requests` create | POST | SkladBot create/return workers | write | n/a | client POST no retry, recovery lookup after failure | local event failed/recovered | P0-by-audit-rule; not daily report path |

Findings:

- Daily report itself does not call `create_request`.
- POST methods are present and used. Some are read-style report queries, one family is real SkladBot create writes in other workers.
- GET path has stronger retry/backoff than POST path.
- `/requests` crawl no longer uses offset in current worktree.
- Stale old list rows are bucketed and should not consume the primary detail budget after the current local changes.

## 6. Date scope audit

Current code behavior:

- Primary daily scope is `Дата выгрузки / движение склада`.
- Inclusion priority is `unloading_date == report_date`, then movement date by request number.
- `created_at`, `completed_at`, `archived_at` are diagnostic signals, not operational inclusion signals.
- Operational inclusion additionally requires completed + archived.

Docs/roadmap:

- Roadmap requires moving primary business date to unloading date and treating created date as diagnostic.
- `docs/report-source-rules.md` matches that direction.
- README/backend README do not describe the current daily SkladBot behavior and are not reliable for this feature.

| Scenario | created_at | unloading_date | movement date | completed | archived | current behavior | expected behavior | risk |
|---|---|---|---|---|---|---|---|---|
| created today + unloading today | today | today | empty/same | yes | yes | included operational | included | Low |
| created earlier + unloading today | old | today | empty/same | yes | yes | included operational | included | Low |
| created today + unloading future | today | future | empty | any | any | diagnostic/excluded | diagnostic only | Low/Medium, count visible |
| created earlier + movement today | old | empty/old | today | yes | yes | included operational | included | Low |
| completed only | any | today or diagnostic date | any | yes | no | excluded diagnostic | diagnostic only | Low |
| archived only | any | today or diagnostic date | any | no | yes | excluded diagnostic | diagnostic only | Low |
| neither | any | today or diagnostic date | any | no | no | excluded diagnostic | diagnostic only | Low |
| detail error | list row only | unknown | unknown | unknown | unknown | excluded error + partial | partial/block scheduled | Low |
| stale completed+archived old row | old | old/empty | old/empty | yes | yes | out of scope/excluded or skipped | not operational | Low after current crawl |
| unloading today + movement other day | any | today | other | yes | yes | included; conflict only in date diagnostics | owner decision needed | P0/P1 |
| movement today + unloading other day | any | other | today | yes | yes | included by movement only if unloading branch does not match today | owner decision needed | P1 |

Rows that can still disappear from operational XLSX:

- Known out-of-scope rows after sample limit: represented in excluded diagnostics without detail.
- In-scope candidates over detail limit: represented as `detail_limit_reached`, scheduled partial blocks.
- Movement/stock rows beyond fixed limit: not represented and not flagged unless SkladBot response exposes an error that current parser catches.

## 7. Status/inclusion audit

| Status case | Current behavior | Should be operational? | Should be diagnostic? | Current test coverage | Risk |
|---|---|---|---|---|---|
| completed + archived + in-scope | included | yes | date row also present | yes | Low |
| completed only | excluded | no | yes | yes | Low |
| archived only | excluded | no | yes | yes | Low |
| neither | excluded | no | yes | yes | Low |
| cancelled/problem/conflict | no explicit special status branch found in daily scope | owner decision | yes | not confirmed | P1/P2 |
| API list error | no rows or partial/failed | no | errors sheet | yes | Low |
| API detail error | excluded + partial | no | yes | yes | Low |
| out of scope | excluded/skipped diagnostic | no | yes | yes | Low |
| in-scope but not detailed due limit | excluded list row + partial | no | yes | yes | Low |
| included with date conflict | included; conflict in date diagnostics only | unclear | yes | missing | P0/P1 |

## 8. XLSX audit

Workbook sheets in current code:

| Sheet | Purpose | Source data | Missing diagnostics | Risk |
|---|---|---|---|---|
| `Сводка` | summary totals and formula start/end stock | included requests + movements + stock | start stock is formula, not historical snapshot | P2 if reader treats it as true morning stock |
| `Заявки` | operational requests | included only | excluded rows not here | Low |
| `Товары заявок` | products of included requests | included only | excluded products absent | Low |
| `Движения` | warehouse movements | movement endpoint | no pagination/truncation guard | P0/P1 |
| `Остатки` | current stock | products/stock endpoints | no pagination/truncation guard | P1 |
| `Контроль покрытия` | counters/status | coverage object | included conflict not counted as warning | P1 |
| `Исключенные заявки` | excluded/diagnostic/error rows | excluded list/detail rows | included date conflicts are not excluded | P1 |
| `Диагностика дат` | date decision rows | all detailed requests | skipped out-of-scope without detail only in excluded sheet | Low |
| `Ошибки` | collection/API errors | errors and api_errors | truncation without error invisible | P1 |

Partial workbook behavior:

- Manual path can build and send partial workbook with warning.
- Scheduled path blocks partial/failed before workbook creation.
- Empty/diagnostic-only scheduled report is blocked if included is 0 and excluded is greater than 0.

Telegram vs XLSX discrepancy:

- Telegram shows coverage status, included/excluded/API error counts and high-level totals.
- XLSX has deeper sheets.
- If conflict exists on an included request, Telegram may still show `COMPLETE`; details are only inside date diagnostics.

## 9. Telegram/scheduled idempotency audit

| Telegram behavior | Current logic | Expected | Risk | Test coverage |
|---|---|---|---|---|
| Scheduled partial report | blocker raises before XLSX/send | no send | Low | yes |
| Scheduled failed coverage | blocker raises before XLSX/send | no send | Low | yes |
| Scheduled 0 included + excluded rows | blocker raises | no diagnostic-only send | Low | yes |
| Manual partial report | safe message/doc send with warning | allowed only knowingly | P1 operator confusion | partial behavior indirectly covered |
| Message date | derived from report date | match filename | Low | tested |
| Filename date | derived from report date | match message | Low | tested |
| Registry timing | after scheduled document success | after successful send only | Low | tested |
| Document send failure | event failed, no registry, no reconciliation | no false success | Low | tested |
| Send idempotency | date/chat/mode/kind/v2 | prevents duplicate | P1 hides corrected same-day auto-send | tested as no retry |
| Reported registry idempotency | includes date/chat/mode/kind/report hash/request id | allows changed report registry keys | Low | tested |
| getUpdates conflict | scheduled jobs still run | run scheduled despite conflict | Low | tested |

Direct answers:

- Can Telegram say everything is fine if report is partial? Scheduled: no, partial is blocked. Manual: it can send, but message includes incomplete-report warning.
- Can Telegram filename and message date diverge? Not in current code path: both use report date after collection.
- Is registry updated before or after successful send? After scheduled document success.
- Does scheduled idempotency include report date/chat/mode? Yes, plus kind and fixed `v2`.
- Do manual and scheduled commands behave the same? No. Scheduled is stricter.
- Can scheduled send hide an amended same-day report? Yes. A failed or completed send event for the same key blocks auto-send for that date/chat/mode/kind/v2.
- Can scheduled send duplicate? Current key prevents normal duplicate for same date/chat/mode/kind/v2; crash after external delivery and before local finish remains a known distributed-send risk, mitigated by no same-day auto retry.

## 10. Recent runs/logs/artifacts audit

Production/runtime logs in this audit: `PRODUCTION_LOGS_NOT_AVAILABLE`.

Reason: this run did not open live SSH/container logs or production DB. Only local sanitized runbooks and local artifact paths were read.

| Evidence | Time | Mode | report_date | Status | Telegram sent | XLSX created | Notes |
|---|---|---|---|---|---|---|---|
| `.supergoal/daily-skladbot-bvytBP/NO_SEND_DEPLOY_RESULT.md` | 2026-07-05 19:47 Asia/Tashkent | deploy smoke | n/a | deployed no-send | no before schedule | n/a | copied runtime files only; health ready OK in note |
| `.supergoal/daily-skladbot-bvytBP/POST_22_PROCESSING_JOB_FINAL_STATUS.md` | 2026-07-05 22:00 window | scheduled | 2026-07-05 | processing/stuck at check | unknown/no DB proof | unknown | event later needed root cause analysis |
| `.supergoal/daily-skladbot-bvytBP/PARTIAL_REPORT_2026-07-05_FINAL_FACTS.md` | 2026-07-05 17:00Z-17:34Z | scheduled | 2026-07-05 | completed | no manual evidence in sanitized scan | unknown from durable evidence | reported registry count 0 |
| `.supergoal/daily-skladbot-bvytBP/PARTIAL_REPORT_FIX_NO_SEND_DEPLOY_RESULT.md` | 2026-07-05 23:35-23:37 Asia/Tashkent | deploy smoke | n/a | deployed no-send safe | no post-deploy send evidence in note | n/a | next expected 2026-07-06 22:00 |
| `outputs/reconciliations/customer_eod_stock_vs_taksklad_skladbot_daily_2026-06.xlsx` | local artifact path only | reconciliation artifact | 2026-06 | unknown | unknown | yes | contents not read in this audit |

## 11. Test coverage audit

| Test area | What it covers | Missing | Risk |
|---|---|---|---|
| `tests.test_skladbot_daily_report` | page crawl, no offset, max pages, repeated pages, detail errors, detail limit, date/status filters, sheets, message, scheduled block, stale/failed no retry, registry after success | conflict `unloading_date` vs movement date; movement/stock pagination/truncation; SkladBot POST read-style retry | P0/P1 |
| `tests.test_backend_telegram_import` | Telegram import queue, notifications, admin gates, getUpdates 409 still running scheduled jobs | full daily send not primary here | Low |
| fake SkladBot clients | list/detail/pagination/date/status/error fixtures | movement/product/stock high-volume fixtures | P1 |
| scheduled idempotency tests | completed/failed/stale same-day behavior | amended report auto-send decision as explicit product contract | P1 |
| workbook tests | sheet names, coverage/errors sheets | formula semantics and stock snapshot wording | P2 |

Checks run in this audit:

- `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_skladbot_daily_report`: 46 tests OK.
- `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_telegram_import`: 71 tests OK.
- `PYTHONPATH=. ./.venv/bin/python -m py_compile backend/app/skladbot_daily_report.py backend/app/telegram_worker.py tests/test_skladbot_daily_report.py`: OK.
- `git diff --check`: OK.

Tests to add before implementation:

- included request with conflicting unloading/movement dates must either warn/block or follow an explicit owner-approved rule.
- movement endpoint returns exactly limit and has more rows: coverage must become partial or paginate.
- products endpoint returns exactly fixed limit and has more rows: coverage must become partial or paginate.
- manual partial send copy must be unmistakable and covered.
- same-day amended report behavior must be explicit: no auto-send, or a new versioned/manual recovery path.

## 12. Docs-code drift

| Doc | Claim | Code reality | Drift severity | Fix recommendation |
|---|---|---|---|---|
| Roadmap | primary daily scope should be unloading date/movement date; created date diagnostic | current code matches direction | Low | keep as contract |
| `docs/report-source-rules.md` | page+limit crawl, diagnostics, scheduled partial block | current code mostly matches | Low | add remaining limits/conflict caveats |
| `docs/changelog.md` | recent entries claim 46/117 tests and no-send deploy | local tests now match 46 + 71 separately; old combined count not rerun as one command here | Low | no action in audit |
| `README.md` | general project/Telegram features | does not describe current daily SkladBot flow | P2 | update after implementation phase |
| `backend/README.md` | backend MVP/general status | not daily-specific and may be stale | P2 | update after implementation phase |
| old changelog entries | older created-date behavior appears in history | current code no longer uses created date as primary | P2 historical | leave as history, clarify current docs |

## 13. P0/P1/P2 risk register

| Priority | Risk | Evidence | Impact | Recommended fix phase |
|---|---|---|---|---|
| P0 | SkladBot POST/write capability exists in workers | `client.create_request` in request/return create workers | Can mutate SkladBot if pending create events are processed | Phase 0 safety map, deploy/read-only gates |
| P0 | Movement/product/stock POST read-style queries have no pagination/total guard | daily movements/products use fixed limits | Can silently undercount movements/stock while coverage says complete | Phase 2/4 |
| P0/P1 | Included request with conflicting unloading/movement dates does not force coverage warning | conflict diagnostic set before inclusion, coverage count only for excluded rows | Telegram may show complete while date conflict exists | Phase 3/4 |
| P1 | Same-day scheduled idempotency hides corrected report after failed/completed event | send key fixed to date/chat/mode/kind/v2; failed returns empty claim | Corrected report requires manual recovery/new key | Phase 5 |
| P1 | Manual command can send partial report | manual path does not use scheduled blocker | Operator may treat warning report as final | Phase 5 |
| P1 | POST retry weaker than GET retry | `SkladBotClient.post` marks cooldown and raises | transient 429/5xx can block/partial daily; scheduled blocks, manual warns | Phase 2/4 |
| P1 | Some status meanings are not explicit | no dedicated cancelled/problem branch found in daily scope | edge status can be diagnostic but not business-approved | Phase 1/3 |
| P2 | `Сводка` start stock is formula, not historical stock snapshot | formula `end - daily categories` | reader can misinterpret stock movement | Phase 6 |
| P2 | README files do not document current daily flow | daily logic exists in code/docs elsewhere | onboarding drift | Phase 6 |
| P2 | Production live state not verified in this audit | no live log/DB checks this run | cannot claim current 2026-07-06 22:00 behavior | Phase 8 if owner approves |

## 14. Recommended implementation plan

Phase 0 - safety/preflight:

- Files: no code first; write runbook/checklist only if approved.
- Behavior: lock read/write boundaries, daily send recovery policy, owner decision on POST read-style calls.
- Tests: none or existing tests only.
- Stop condition: any real secret/live-write risk.
- Deploy: NO unless explicit.

Phase 1 - lock contract/failing-free fixtures:

- Files: `tests/test_skladbot_daily_report.py`, possibly a fixture helper.
- Behavior: add tests for date conflict, movement/product truncation, manual partial wording, same-day amended report policy.
- Tests: targeted daily report tests.
- Stop condition: owner cannot decide expected conflict behavior.
- Deploy: NO.

Phase 2 - pagination crawl:

- Files: `backend/app/skladbot_daily_report.py`, SkladBot client wrapper if needed.
- Behavior: paginate or explicitly partial-block movements/products/stock when fixed limit may truncate.
- Tests: high-volume fake endpoint fixtures.
- Stop condition: SkladBot API does not expose reliable pagination/total and no owner-approved fallback.
- Deploy: NO.

Phase 3 - date scope:

- Files: daily report scope functions.
- Behavior: explicit rule for conflicting unloading/movement dates; decide whether conflict blocks scheduled send.
- Tests: scenario matrix.
- Stop condition: ambiguous business date rule.
- Deploy: NO.

Phase 4 - coverage/XLSX diagnostics:

- Files: daily coverage, workbook writers.
- Behavior: count conflicts/truncation in coverage; show all risky rows in diagnostics.
- Tests: workbook/coverage assertions.
- Stop condition: any partial case can still become `complete` without owner-approved reason.
- Deploy: NO.

Phase 5 - Telegram summary/idempotency:

- Files: `telegram_worker.py`, daily message tests.
- Behavior: make partial/manual wording unmistakable; define amended same-day send recovery path.
- Tests: scheduled/manual/idempotency tests.
- Stop condition: duplicate-send risk not mitigated.
- Deploy: NO.

Phase 6 - docs update:

- Files: `docs/report-source-rules.md`, README/backend README if approved.
- Behavior: document actual source truth and stock formula semantics.
- Tests: docs only plus existing tests.
- Stop condition: docs imply live behavior not verified.
- Deploy: NO.

Phase 7 - local hardening:

- Files: tests + small code guards.
- Behavior: py_compile, unit tests, diff check, marker scan.
- Tests: required verifier loop.
- Stop condition: any failing test.
- Deploy: NO.

Phase 8 - optional deploy gate:

- Files: runtime files only, if approved.
- Behavior: no-send deploy window, backup, py_compile on host/container, health/ready, no unexpected sends.
- Tests: live smoke read-only plus no-send evidence.
- Stop condition: near schedule window, active processing event, or missing rollback.
- Deploy: only with explicit approval.

## 15. Open questions for owner

1. If `unloading_date` and movement date conflict, should scheduled daily block, include by unloading date with warning, or exclude to diagnostics?
2. Does SkladBot guarantee movement/product/stock endpoints never exceed current fixed limits for this customer?
3. Should a corrected same-day scheduled report ever auto-send, or must it always require manual recovery?
4. Is manual `/skladbot_daily` allowed to send partial reports, or should it share the scheduled blocker?
5. Should `Сводка` start stock remain derived formula, or should a real opening stock snapshot be required before calling it start-of-day stock?

## 16. Evidence paths

Code truth:

- `backend/app/skladbot_daily_report.py`
- `backend/app/telegram_worker.py`
- `backend/app/skladbot_worker.py`
- `backend/app/skladbot_request_dry_run.py`
- `backend/app/skladbot_return_requests.py`

Test truth:

- `tests/test_skladbot_daily_report.py`
- `tests/test_backend_telegram_import.py`

Docs truth:

- `.supergoal/daily-skladbot-bvytBP/ROADMAP.md`
- `.supergoal/daily-skladbot-bvytBP/IMPLEMENTATION_RESULTS.md`
- `.supergoal/daily-skladbot-bvytBP/STUCK_PROCESSING_ROOT_CAUSE.md`
- `.supergoal/daily-skladbot-bvytBP/NO_SEND_DEPLOY_RESULT.md`
- `.supergoal/daily-skladbot-bvytBP/PARTIAL_REPORT_2026-07-05_FINAL_FACTS.md`
- `.supergoal/daily-skladbot-bvytBP/PARTIAL_REPORT_FIX_NO_SEND_DEPLOY_RESULT.md`
- `docs/report-source-rules.md`
- `docs/changelog.md`
- `docs/implementation-log.md`
- `README.md`
- `backend/README.md`

Runtime/deploy config truth:

- `deploy/vds/docker-compose.yml`
- `deploy/vds/.env.example`
- `.github/workflows/ci.yml`
- `.github/workflows/deploy-production.yml`

Data/artifact truth:

- `outputs/reconciliations/customer_eod_stock_vs_taksklad_skladbot_daily_2026-06.xlsx`

Live truth:

- Not checked in this audit.

Operator truth:

- Not checked in this audit.
