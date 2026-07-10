# Code organization guard

`PYTHONPATH=. .venv/bin/python tools/check_code_organization.py --strict` is the
machine gate for the Phase 16 worker boundaries.

It parses every Python module under `backend/app`, including imports nested in
functions, and uses Tarjan's algorithm to reject any dependency SCC containing
both an order module and a SkladBot module. It also enforces these limits:

- `backend/app/telegram_worker.py`: at most 1500 lines;
- every `backend/app/telegram_*_processor.py`: at most 700 lines;
- `telegram_worker.py` cannot import SQLAlchemy, `.models`, or `SessionLocal`,
  and cannot call persistence methods such as `execute`, `add`, or `commit`.
- `TelegramWorker` may define only transport, scheduling and routing methods;
  domain import/report/admin methods must live in independently testable processors.

Temporary exceptions live only in
`tools/code_organization_exceptions.json`. Every entry requires a supported
rule, repository-relative path, owner, and concrete reason. Applied and unused
exceptions are printed by the tool. An exception does not change a limit; it
makes an acknowledged migration gap visible and machine-readable. Remove each
temporary entry as soon as the corresponding extraction lands.

Invalid exception JSON, missing owner/reason, duplicate entries, absolute paths,
unsupported rules, or stale unused exceptions fail strict mode. The current
exception list is empty.
