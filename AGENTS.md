<!-- kb-entrypoint -->
Хаб проекта в базе знаний это обзор для человека:
`/Users/anton/Documents/work/База знаний/TakSklad/00-TakSklad.md`
Операционные правила и разобранные поломки живут здесь и в памяти проекта,
хаб для них не источник и может отставать

Живой портфель всех проектов:
`/Users/anton/Documents/work/База знаний/00-Система/Портфель.base`

База знаний это указатель, целиком её не читать и широко не грепать:
маршрут короткий, контракт, один хаб, один физический документ
<!-- kb-entrypoint -->

# TakSklad

Ты Codex, работаешь в проекте `TakSklad`. Пиши Антону по-русски, коротко и по делу

Рабочее складское приложение и pKIS: Excel-заказы, группировка, КИЗы, Google Sheets,
backup, Telegram-отчёты, складские операции
Общие правила workspace в корневом `AGENTS.md`, здесь только своё

Любое изменение сохраняет надёжность склада: дедуп КИЗов, audit, backup,
понятные отчёты и воспроизводимые проверки

Перед изменениями сначала изучить связанные файлы, текущую логику и `docs/`

Не читать и не использовать секреты: `.env*`, credentials, `Пароли.md`,
`/Users/anton/.codex/LOCAL_SECRETS.md`, клиентские выгрузки, outputs, backups
и реальные отчёты как источник для графа

## Client-facing output contract

1. Без явного согласования запрещено добавлять или менять client-facing Telegram reports,
   XLSX строки, колонки, captions, filenames, labels, web UI, routes, schedules, fallbacks,
   message types и новый пользовательский функционал
2. Перед таким изменением показать exact before/after и запросить approval
3. Изменения Telegram routing или error требуют synthetic contract tests и no-send verifier
4. Raw production chat ID и Telegram-токены не коммитить и не логировать
5. Технический internal provenance не выводить в client-facing output без согласования

## Git branch discipline

1. Конечная точка любой работы это `main`, прямой push в него не используется:
   доставка идёт через ветку, PR и squash-мерж, один коммит на PR
   Ruleset `TakSklad main release gate` держит запрет удаления, запрет force-push
   и линейную историю
2. Зелёного CI не будет: GitHub Actions не стартуют с 2026-08-14 (биллинг),
   решением от 2026-08-16 платные функции GitHub не используются, обязательный
   чек `Release gate` снят из ruleset 2026-08-17
   Доказательство изменения это локальные прогоны тестов, их вывод показывать в PR
3. Перед изменениями выполнить `git branch --show-current` и `git status --short --branch`
4. Ветку под PR брать от `origin/main` в отдельном worktree, а не от локального
   `main` и не переключением веток: рабочее дерево почти всегда несёт
   незакоммиченные правки, переключение их задевает
5. Запрещено оставлять production или runtime fix только в `codex/*`, `feature/*` или боковой ветке
6. Локальные хуки из `.githooks/` блокируют commit и push не из `main`
   Для ветки под PR это согласованное исключение, префикс `ALLOW_NON_MAIN_BRANCH=1`
   нужен и на `git commit`, и на `git push`

Рабочий порядок доставки:

```
git fetch origin main
git worktree add --detach /tmp/<задача> origin/main
cd /tmp/<задача> && git checkout -b <ветка>
<правки>
ALLOW_NON_MAIN_BRANCH=1 git commit   # только свои файлы поимённо
ALLOW_NON_MAIN_BRANCH=1 git push -u origin <ветка>
gh pr create
gh pr merge --squash --delete-branch
git worktree remove /tmp/<задача>
```

Не делать `git reset --hard`: он сносит незакоммиченные правки рабочего дерева
Не коммитить каталогами (`git add -A`, `git add tests/`): так в PR уезжает чужая
незавершённая работа, перед push сверять `git show --stat`

Деплой сам не запускается, а без Actions не работает и `workflow_dispatch`:
выкат делается вручную на сервере, порядок в `docs/CURRENT_STATUS.md`
(раздел «Release-контур») и `docs/deploy-rollback-runbook.md`

## Smartup live smoke

1. Локальные параметры искать только в `.env.smartup.local`, он gitignored с правами `600`
   и не попадает в docs, логи, граф, git diff, субагентам и внешние сервисы
2. Переменные: `SMARTUP_BASE_URL`, `SMARTUP_PROJECT_CODE`, `SMARTUP_FILIAL_ID`,
   `SMARTUP_USERNAME`, `SMARTUP_PASSWORD`
3. Без отдельного подтверждения разрешён только read-only `order$export` на короткое окно
   Менять статусы Smartup, импортировать в TakSklad и делать write-back нельзя

## Проверка изменения

Frontend: `npm run lint` в `frontend/`
Backend: запусти тесты проекта и покажи вывод, конкретную команду смотри в `docs/`
и в CI-конфиге репозитория, не выдумывай её
Изменения client-facing вывода дополнительно проверяются contract tests и no-send verifier

## Knowledge graph

1. Общий root контекста: `/Users/anton/Documents/work/_knowledge-graph`
2. Для архитектурных вопросов, поиска связей и онбординга сначала:
   `/Users/anton/Documents/work/_knowledge-graph/scripts/graph-query.sh TakSklad "<вопрос>"`
3. Граф отсутствует или устарел, сначала dry-run:
   `/Users/anton/Documents/work/_knowledge-graph/.venv/bin/python /Users/anton/Documents/work/_knowledge-graph/scripts/build_safe_graph.py --project TakSklad --dry-run`
4. Граф не источник истины. Проверять исходный код, docs, тесты и реальные команды проекта
5. Новые заметки агентов, handoff и cross-project выводы складывать
   в `/Users/anton/Documents/work/_knowledge-graph/projects/TakSklad/`
