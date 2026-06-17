# TakSklad Alembic Migrations

Alembic is the production schema migration path for the backend.

Raw SQL files in `backend/sql/` stay as bootstrap/history for an empty local database and for understanding the first rollout. Do not use them as the main way to change a live production schema after Alembic is stamped.
