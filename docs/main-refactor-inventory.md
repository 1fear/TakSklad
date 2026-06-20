# TakSklad main.py Refactor Inventory

Created: 2026-06-21

## Baseline

- `src/taksklad/main.py`: 2785 lines.
- `ScanningApp`: starts at line 722 and spans about 2041 lines.
- Goal: keep `main.py` as app assembly and startup wiring, target <= 500 lines after extraction.

## Baseline Clusters Before Extraction

| Lines | Responsibility | Target owner |
|---|---|---|
| 1-130 | imports, mixins, logging, UI constants | `main.py`, `app_layout.py`, focused owner modules |
| 142-178 | startup/error popup helpers | `app_runtime.py` or runtime message helpers |
| 181-253 | date/money/scan count/product result helpers | `desktop_scan_rules.py` |
| 256-526 | finish blockers, terminal state, backend helper messages | `desktop_scan_rules.py`, `backend_flow.py` |
| 529-720 | Google/backend/SkladBot refresh source functions | `desktop_refresh_service.py` |
| 722-790 | `ScanningApp.__init__` and app state | `main.py` |
| 792-920 | data load, backend event sync, background runner | `app_data_loading.py`, `app_runtime.py` |
| 922-1115 | busy/refresh/status/error UI | `app_runtime.py` |
| 1117-1250 | backend blocked scan recovery and undo | `app_scanning.py` |
| 1252-1560 | main Tkinter layout builder | `app_layout.py` |
| 1562-1636 | order group list and selection helpers | `app_order_display.py` |
| 1638-2040 | returns window and return routing | `app_returns.py` |
| 2042-2333 | reset, refresh trigger, selected order display, product photo | `app_order_display.py`, `app_data_loading.py` |
| 2335-2444 | KIZ scan input flow | `app_scanning.py` |
| 2446-2570 | position save flow | `app_scanning.py` |
| 2572-2748 | finish, print, backend complete, Google archive | `app_finish.py` |
| 2750-2785 | close and `run_app` | `app_runtime.py`, `main.py` |

## Extraction Order

1. Lock baseline with characterization tests.
2. Extract pure helpers.
3. Extract refresh/data loading services.
4. Extract runtime/status infrastructure.
5. Extract returns.
6. Extract order display and current product photo.
7. Extract scan and position-save flow.
8. Extract finish/print/archive flow.
9. Extract layout builder.
10. Install architecture guards.
11. Run final hardening.

## Final Ownership Map

| Area | Owner module |
|---|---|
| App assembly, startup, `ScanningApp.__init__`, Windows shortcut/bootstrap | `src/taksklad/main.py` |
| Tkinter main screen layout and widget creation | `src/taksklad/app_layout.py` |
| Order list, order selection, current product text, product photo | `src/taksklad/app_order_display.py` |
| KIZ validation, scan input, duplicate checks, undo, position save | `src/taksklad/app_scanning.py` |
| Finish order, print summary, backend complete, Google archive | `src/taksklad/app_finish.py` |
| Refresh and data loading from Google/backend/SkladBot | `src/taksklad/app_data_loading.py`, `src/taksklad/desktop_refresh_service.py` |
| Runtime state, busy flags, toast/error/status handling, close hook | `src/taksklad/app_runtime.py` |
| Returns UI and backend/Google return routing | `src/taksklad/app_returns.py` |
| Pure scan/date/money/result helpers | `src/taksklad/desktop_scan_rules.py` |
| Backend event blockers, completion helpers, backend messages | `src/taksklad/backend_flow.py` |
| Existing action surfaces: imports, catalog, control panel, printing, day-end, SkladBot, Telegram, updates | `src/taksklad/app_*.py` focused modules |

## Future Change Rule

Substantial new workflow code must not be added directly to `src/taksklad/main.py`.
Use the owner module above, or create a focused `app_<area>.py` / service module when the feature has a new responsibility.
If code has to stay in `main.py`, document the extraction rationale in this file and keep `main.py` at or below 500 lines.

## Invariants

- KIZ path stays `validate/SKU/dedup -> write_scan_backup -> scanned_codes -> queue_backend_scan`.
- Every scan, undo, queued save, saved position, and finish path keeps local backup behavior.
- `add_pending_print` happens before direct print, and `remove_pending_print` happens only after print success.
- Backend complete must not happen if printing fails.
- Google archive runs only for the non-backend finish path.
- Backend blocked events only block the current item/group.
- Backend return mode must not silently write a Google return for an order without backend identity.
- `ScanningApp` mixin order must be changed cautiously because Tkinter callbacks resolve methods through MRO.
