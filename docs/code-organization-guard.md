# Code organization guard

`PYTHONPATH=. .venv/bin/python tools/check_code_organization.py --strict` is the
machine gate for the Phase 16 worker boundaries.

It parses every Python module under `backend/app`, including imports nested in
functions, and uses Tarjan's algorithm to reject any dependency SCC containing
both an order module and a SkladBot module. It also rejects every
`telegram_*_processor -> telegram_worker` back-edge and every SCC containing the
Telegram worker plus one of its processors. It enforces these limits:

- `backend/app/telegram_worker.py`: at most 1500 lines;
- every `backend/app/telegram_*_processor.py`: at most 700 lines;
- `telegram_worker.py` cannot import SQLAlchemy, `.models`, `SessionLocal`, or a
  persistence service-locator wrapper, and cannot call persistence methods such
  as `execute`, `add`, or `commit`;
- `TelegramWorker` may define only configuration, scheduling, and routing
  methods. HTTP transports, Telegram/backend payload formatting, and domain
  import/report/admin methods must live behind independently testable clients
  and processors;
- `TelegramWorker` uses composition and cannot inherit processors, transport,
  API client, or processor-port bases; it cannot import HTTP/URL transport
  modules or call the generic
  `telegram_request` surface; polling request payloads belong to
  `TelegramApiClient`;
- every extracted Telegram processor must declare `TelegramProcessorDelegate`,
  whose constructor composes a `TelegramProcessorPorts` object, so injected fake
  clients can exercise it independently without making Worker a transport;
- the import processor cannot import HTTP/URL transport modules or contain a
  Telegram API URL. File metadata and streaming download are client-owned.

Temporary exceptions live only in
`tools/code_organization_exceptions.json`. Every entry requires a supported
rule, repository-relative path, owner, and concrete reason. Applied and unused
exceptions are printed by the tool. An exception does not change a limit; it
makes an acknowledged migration gap visible and machine-readable. Remove each
temporary entry as soon as the corresponding extraction lands.

Invalid exception JSON, missing owner/reason, duplicate entries, absolute paths,
unsupported rules, or stale unused exceptions fail strict mode. The current
exception list is empty.
