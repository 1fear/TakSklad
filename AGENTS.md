Ты Codex, работаешь в проекте `TakSklad`.

Пиши Антону на русском языке, коротко и по делу.

Главное:
1. Это рабочее складское приложение/pKIS: Excel-заказы, группировка, КИЗы, Google Sheets, backup, Telegram-отчеты и складские операции.
2. Перед изменениями сначала изучай связанные файлы, текущую логику и docs.
3. Не читать и не использовать секреты, `.env*`, credentials, `Пароли.md`, `/Users/anton/.codex/LOCAL_SECRETS.md`, клиентские выгрузки, outputs, backups и реальные отчеты как источник для графа.
4. Любые изменения должны сохранять надежность склада: дедуп КИЗов, audit, backup, понятные отчеты и воспроизводимые проверки.

Knowledge graph:
1. Общий root контекста: `/Users/anton/Documents/work/_knowledge-graph`.
2. Для архитектурных вопросов, поиска связей и онбординга сначала используй:
   `/Users/anton/Documents/work/_knowledge-graph/scripts/graph-query.sh TakSklad "<вопрос>"`.
3. Если граф отсутствует или устарел, сначала сделай dry-run:
   `/Users/anton/Documents/work/_knowledge-graph/.venv/bin/python /Users/anton/Documents/work/_knowledge-graph/scripts/build_safe_graph.py --project TakSklad --dry-run`.
4. Граф не является source of truth. Проверяй исходный код, docs, тесты и реальные команды проекта.
5. Новые заметки агентов, handoff и cross-project выводы складывай в `/Users/anton/Documents/work/_knowledge-graph/projects/TakSklad/`.
