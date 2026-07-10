from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .settings import load_settings


def database_engine_kwargs(database_url, app_settings):
    url = make_url(database_url)
    backend_name = url.get_backend_name()
    if backend_name == "postgresql":
        options = (
            f"-c statement_timeout={app_settings.db_statement_timeout_ms} "
            f"-c lock_timeout={app_settings.db_lock_timeout_ms} "
            f"-c idle_in_transaction_session_timeout={app_settings.db_idle_transaction_timeout_ms}"
        )
        return {
            "pool_pre_ping": True,
            "pool_size": app_settings.db_pool_size,
            "max_overflow": app_settings.db_max_overflow,
            "pool_timeout": app_settings.db_pool_timeout_seconds,
            "pool_recycle": app_settings.db_pool_recycle_seconds,
            "pool_use_lifo": True,
            "pool_reset_on_return": "rollback",
            "connect_args": {
                "connect_timeout": app_settings.db_connect_timeout_seconds,
                "options": options,
            },
        }
    if backend_name == "sqlite":
        kwargs = {
            "pool_pre_ping": True,
            "connect_args": {"check_same_thread": False},
        }
        if url.database in (None, "", ":memory:"):
            kwargs["poolclass"] = StaticPool
        return kwargs
    return {"pool_pre_ping": True, "pool_reset_on_return": "rollback"}


def create_db_engine(app_settings):
    return create_engine(
        app_settings.database_url,
        **database_engine_kwargs(app_settings.database_url, app_settings),
    )


settings = load_settings()
engine = create_db_engine(settings)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    except SQLAlchemyError:
        db.rollback()
        raise
    finally:
        db.close()
