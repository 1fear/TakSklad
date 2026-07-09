# Release source boundary

TakSklad release candidates are built only from reviewed Git paths. Operational
data, client exports, credentials, local environment files, backups, outputs and
runtime reports are never release inputs.

## Local workflow

1. Run `tools/check_release_tree.py --strict --path-only` before edits.
2. Record the owned boundary with
   `tools/check_release_tree.py --strict --write-owned-manifest`.
3. Before the next phase, run
   `tools/check_release_tree.py --compare-owned-manifest --strict`.
4. Refresh the manifest only after the current phase is verified and committed.

The manifest lives in ignored `.release-state/`; it contains only HEAD, branch,
allowed path/status values and SHA-256 hashes. Forbidden paths are classified by
name and are never opened or hashed by the guard.

The repository pre-commit hook and CI call the same path policy. Local clones
should enable the tracked hooks with `git config core.hooksPath .githooks`.

`tools/run_safe_tests.py` and normal unittest discovery both execute the full
suite. PostgreSQL integration modules skip only when the disposable test URL is
absent; `tools/run_postgres_tests.sh` supplies it for mandatory migration and
two-session checks. No test module reads repository `.env*` files.
