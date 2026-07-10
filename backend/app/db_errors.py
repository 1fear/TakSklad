from dataclasses import dataclass

from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError, TimeoutError as SQLAlchemyTimeoutError


STATEMENT_TIMEOUT_SQLSTATE = "57014"
LOCK_TIMEOUT_SQLSTATE = "55P03"
RETRYABLE_TRANSACTION_SQLSTATES = frozenset({"40001", "40P01"})


@dataclass(frozen=True)
class DatabaseErrorClassification:
    code: str
    http_status: int
    sqlstate: str = ""


def database_error_sqlstate(error):
    original = getattr(error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if not sqlstate:
        sqlstate = getattr(getattr(original, "diag", None), "sqlstate", None)
    return str(sqlstate or "")


def classify_database_error(error):
    sqlstate = database_error_sqlstate(error)
    if sqlstate == STATEMENT_TIMEOUT_SQLSTATE:
        return DatabaseErrorClassification("database_timeout", status.HTTP_503_SERVICE_UNAVAILABLE, sqlstate)
    if sqlstate == LOCK_TIMEOUT_SQLSTATE:
        return DatabaseErrorClassification("database_busy", status.HTTP_503_SERVICE_UNAVAILABLE, sqlstate)
    if sqlstate in RETRYABLE_TRANSACTION_SQLSTATES:
        return DatabaseErrorClassification("database_retryable", status.HTTP_503_SERVICE_UNAVAILABLE, sqlstate)
    if isinstance(error, SQLAlchemyTimeoutError):
        return DatabaseErrorClassification("database_busy", status.HTTP_503_SERVICE_UNAVAILABLE, sqlstate)
    if isinstance(error, OperationalError) or bool(getattr(error, "connection_invalidated", False)):
        return DatabaseErrorClassification("database_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE, sqlstate)
    if isinstance(error, DBAPIError):
        return DatabaseErrorClassification("database_error", status.HTTP_500_INTERNAL_SERVER_ERROR, sqlstate)
    return DatabaseErrorClassification("database_error", status.HTTP_500_INTERNAL_SERVER_ERROR, sqlstate)


def database_error_response(error):
    classification = classify_database_error(error)
    headers = {"Cache-Control": "no-store"}
    if classification.http_status == status.HTTP_503_SERVICE_UNAVAILABLE:
        headers["Retry-After"] = "1"
    return JSONResponse(
        status_code=classification.http_status,
        content={"detail": classification.code},
        headers=headers,
    )
