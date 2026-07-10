# Input and spreadsheet export safety

This runbook defines the enforced Phase 15 boundaries. They apply before import parsing or database dependencies and use fixed error codes without raw customer values or unsafe filenames.

## Enforced limits

| Surface | Limit | Rationale |
| --- | ---: | --- |
| ASGI request body | 24 MiB | Covers the bounded 20 MiB import payload plus JSON overhead. |
| Nginx / Traefik request body | 32 MiB | Outer denial-of-service ceiling; valid requests reach the deterministic ASGI 413 boundary. |
| Import rows | 5,000 | Explicit synthetic operating ceiling; the maximum-valid benchmark covers the exact boundary. |
| Fields per import row | 64 | More than the supported import aliases while preventing arbitrarily wide JSON objects. |
| Import key | 128 characters | Covers Russian and English aliases. |
| Import cell | 16,384 characters | Preserves long notes/product text without permitting unbounded cells. |
| Import row / import payload | 64 KiB / 20 MiB | Bounds both individual rows and aggregate validation memory. |
| Raw payload | depth 4, 256 keys, 512 items, 64 KiB | Supports structured audit metadata while bounding nesting, fan-out, and encoded size. |
| Filename | 128 characters / 255 UTF-8 bytes | Portable leaf filename; controls, paths, drive prefixes, traversal and non-XLSX extensions are rejected. |
| XLSX compressed file | 20 MiB | Matches the Telegram download boundary. |
| XLSX ZIP entries | 2,048 | Covers normal OOXML packages and blocks entry-count attacks. |
| XLSX uncompressed total / entry | 80 MiB / 48 MiB | Bounds expansion before openpyxl; the fully populated 5,000×128 synthetic sheet is 35.8 MiB. |
| XLSX compression ratio | 200:1 | Rejects high-ratio expansion while allowing synthetic maximum-valid evidence. |
| XLSX rows / columns | 5,000 data rows plus one header / 128 | Matches the API row boundary and benchmark shape. |
| XLSX cell | 16,384 characters | Matches the API cell boundary. |

## Deterministic rejection matrix

| Condition | Status / code | Persistence |
| --- | --- | --- |
| Declared or streamed body over 24 MiB | `413 request_too_large` | Parser and DB dependency are not called. |
| Invalid import DTO, nested row, excessive rows/key/cell/bytes | `422 invalid_request` | No import, file, order, item, event, audit, incident or client-point row. |
| Unsafe filename | `422 invalid_request` or `spreadsheet_rejected:filename_*` | No parser or persistence. |
| ZIP traversal, encryption, entry/size/ratio overflow | `spreadsheet_rejected:archive_*` or `spreadsheet_rejected:compression_ratio_exceeded` | Rejected before openpyxl. |
| Worksheet row/column/cell overflow | `spreadsheet_rejected:rows_exceeded`, `columns_exceeded`, or `cell_length_exceeded` | Rejected before business parsing. |
| Deep/wide/large raw payload | `422 invalid_request` | Rejected before route handler; response is redacted. |

## Literal exports

All Google Sheets mutations use `RAW`. Before XLSX save, workbook writers force any string beginning with `=`, `+`, `-`, or `@` to the string data type. The value itself is unchanged; normal text, numeric and date cells retain their existing types and values.

## Synthetic boundary benchmark

Run:

```bash
PYTHONPATH=. .venv/bin/python tools/benchmark_import_limits.py --profile maximum-valid --assert-budgets
```

The benchmark generates only temporary synthetic data: 5,000 fully populated 128-column rows and one 16,384-character cell, then performs a real import into an isolated in-memory database with external writers stubbed. Approved budgets are 75 seconds under concurrent CI load and 256 MiB peak traced Python memory. It also prints compressed/uncompressed archive sizes, ratio, import status and created/duplicate counts for audit evidence.
