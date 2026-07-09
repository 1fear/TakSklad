# TakSklad Alembic Migrations

Alembic is the production schema migration path for the backend.

Raw SQL files in `backend/sql/` stay as historical recovery context only. Empty databases and all normal upgrades use `alembic upgrade head`; raw SQL bootstrap is explicit, empty/unversioned-only, and must never run together with Alembic baseline creation.
