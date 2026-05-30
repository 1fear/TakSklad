# TakSklad Restore Points

Этот файл нужен как локальная памятка: где лежит точка восстановления перед крупными обновлениями.

## 2026-05-31 Before MVP Updates

- Restore ID: `2026-05-31_before_mvp_updates_003050`
- Git branch: `restore/2026-05-31_before_mvp_updates_003050`
- Git tag: `restore-2026-05-31_before_mvp_updates_003050`
- Local snapshot: `/Users/anton/Documents/work/_restore_points/TakSklad_2026-05-31_before_mvp_updates_003050`
- Snapshot permissions: только текущий пользователь macOS.

Состав точки восстановления:

- `files/` - снимок рабочих файлов проекта без `.git`, `.venv`, `node_modules`, кэшей и сборочных директорий;
- `worktree.diff` - patch текущих незакоммиченных изменений;
- `index.diff` - patch staged-изменений, если они были;
- `git-status-short.txt` - короткий статус на момент снимка;
- `git-status-porcelain-v2.txt` - машинный статус Git;
- `untracked-files.txt` - список untracked-файлов.

Важно: локальный снимок может содержать локальные конфиги и credentials, поэтому его нельзя отправлять в GitHub или Telegram.

## 2026-05-31 MVP Telegram/Logistics/SkladBot Checkpoint

- Git tag: `checkpoint-2026-05-31_mvp-telegram-logistics-skladbot`
- Checkpoint commit: commit, на который указывает этот tag.
- Назначение: локальная точка текущего результата после MVP-доработок Telegram import, логистического отчёта, SkladBot matching, КИЗ по файлам и чернового frontend.
- Это не production release и не версия для автообновления рабочих ПК.
- `version.json`, GitHub Release и Windows-архив не менялись.
