# Desktop secret-location contract

## Allowed locations

| File class | May contain | Must not contain |
|---|---|---|
| Windows per-user DPAPI store under `%LOCALAPPDATA%\TakSklad\secrets` | Ciphertext for Google credentials, Telegram bot token, backend API token and geocoder key | Plaintext, machine-scoped DPAPI blobs |
| Explicit development/test environment provider | Process-scoped synthetic/development values | Files, backups or production fallback |
| `TakSklad_data.json` and last-good backups | Non-secret settings, queue metadata and operational state | Google credentials, Telegram bot token, backend token, geocoder key |
| Update/test archives and rollback directories | Application code and non-secret runtime state | Secret-store blob, legacy credential files, synthetic sentinel |
| Diagnostics, logs and support JSON | Presence/status, counts, error classes | Secret values, credential JSON, authorization headers |

The DPAPI store is outside `APP_DIR`, so the onedir updater never copies it into a new, previous or failed application directory. Its ACL permits only the current Windows SID and `LOCAL SYSTEM`; DPAPI uses CurrentUser scope and UI-forbidden mode.

## Migration transaction

1. Read only the known legacy source classes during startup migration.
2. Write each value to the selected secure provider.
3. Read it back independently and require exact equality.
4. Sanitize current state, every last-good backup and generated runtime JSON.
5. Remove legacy Google, Telegram and geocoder plaintext files.
6. If any write, verification or purge fails, restore the original source bytes and stop startup with a value-free `migration_failed` status.

Generic state save, backup creation and backup restore also sanitize secret fields. Therefore an old backup cannot reintroduce a migrated value.
When current state is absent, migration may source a secret from the newest valid last-good backup, but it leaves current state absent so normal backup recovery still restores the backup's non-secret settings and operational state.

## Provider and rollback rules

- Windows production defaults to the DPAPI CurrentUser provider.
- Non-Windows development/test must select the environment provider or inject the in-memory provider.
- Frozen builds reject environment and in-memory providers even when process environment variables request a development/test mode.
- Windows production validates DPAPI availability even when no legacy plaintext remains; corruption or ACL denial blocks startup rather than reporting a clean migration.
- Frozen non-Windows production fails closed; there is no credentials/state/file fallback.
- Rollback restores source code/config only. It does not export or copy the DPAPI store. A legacy plaintext rollback requires a separately approved manual recovery and is not part of the autonomous segment.

## Verification evidence

Evidence contains only provider/status names, counts and hashes. Synthetic values are never printed. The Windows matrix must prove a fresh same-user process can decrypt, while a temporary alternate user is denied both by ACL and by DPAPI after receiving a readable copy of the ciphertext.
