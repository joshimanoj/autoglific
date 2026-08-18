"""Request-scoped database connection seam for hosted read paths."""

from __future__ import annotations

import os
import threading
from contextvars import ContextVar, Token
from typing import Any

_CONNECTION: ContextVar[Any | None] = ContextVar("product4_request_connection", default=None)
_IDLE_CONNECTIONS: list[Any] = []
_IDLE_LOCK = threading.Lock()
_MAX_IDLE_CONNECTIONS = 2


def open_request_connection() -> tuple[Token[Any | None] | None, Any | None]:
    """Open one connection for the current request, when hosted DB is configured."""

    if _CONNECTION.get() is not None:
        return None, None
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        return None, None
    connection = None
    with _IDLE_LOCK:
        while _IDLE_CONNECTIONS:
            candidate = _IDLE_CONNECTIONS.pop()
            if not getattr(candidate, "closed", False):
                connection = candidate
                break
    if connection is None:
        try:
            import psycopg

            connection = psycopg.connect(database_url)
        except Exception:
            # The normal backend will produce the safe public storage error. Do not
            # change error classification at this seam.
            return None, None
    return _CONNECTION.set(connection), connection


def close_request_connection(
    token: Token[Any | None] | None,
    connection: Any | None,
    *,
    commit: bool = True,
) -> None:
    if token is None or connection is None:
        return
    healthy = True
    if commit:
        try:
            connection.commit()
        except Exception:
            healthy = False
            try:
                connection.rollback()
            except Exception:
                pass
    else:
        try:
            connection.rollback()
        except Exception:
            healthy = False
    try:
        if healthy and not getattr(connection, "closed", False):
            with _IDLE_LOCK:
                if len(_IDLE_CONNECTIONS) < _MAX_IDLE_CONNECTIONS:
                    _IDLE_CONNECTIONS.append(connection)
                    connection = None
        if connection is not None:
            connection.close()
    finally:
        _CONNECTION.reset(token)


def current_request_connection() -> Any | None:
    return _CONNECTION.get()
