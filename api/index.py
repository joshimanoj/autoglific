"""Same-origin FastAPI entrypoint for the Product 4 workbench.

Vercel recognizes the module-level ``app`` ASGI object.  The route handlers
delegate to the existing ``WorkbenchApp`` methods so local and hosted API
contracts remain the same.
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote

_HERE = Path(__file__).resolve()
for _candidate in (_HERE.parents[2], _HERE.parents[1]):
    if (_candidate / "product4").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response
except (
    ModuleNotFoundError
):  # pragma: no cover - deployment dependency is declared separately
    FastAPI = None  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[misc,assignment]
    JSONResponse = Response = None  # type: ignore[assignment,misc]

PROJECT_ROOT = _HERE.parents[1]
if not (PROJECT_ROOT / "workbench").is_dir() and (PROJECT_ROOT / "product4" / "workbench").is_dir():
    PROJECT_ROOT = PROJECT_ROOT / "product4"
MAX_JSON_BYTES = 8 * 1024 * 1024
_RUNTIME: dict[str, Any] | None = None
_REQUEST_TIMINGS: list[tuple[str, float]] | None = None


def _runtime() -> dict[str, Any]:
    global _RUNTIME
    if _RUNTIME is None:
        started = time.perf_counter()
        from product4.workbench import server
        from product4.workbench import auth

        _RUNTIME = {
            "server": server,
            "auth": auth,
            "ApiError": server.ApiError,
            "AuthError": auth.AuthError,
            "Principal": auth.Principal,
            "WorkbenchApp": server.WorkbenchApp,
            "AUTH_SESSION_COOKIE": auth.AUTH_SESSION_COOKIE,
            "AUTH_SESSION_SECONDS": auth.AUTH_SESSION_SECONDS,
            "CSRF_COOKIE": auth.CSRF_COOKIE,
        }
        globals().update(
            ApiError=server.ApiError,
            AuthError=auth.AuthError,
            Principal=auth.Principal,
            WorkbenchApp=server.WorkbenchApp,
            AUTH_SESSION_COOKIE=auth.AUTH_SESSION_COOKIE,
            AUTH_SESSION_SECONDS=auth.AUTH_SESSION_SECONDS,
            CSRF_COOKIE=auth.CSRF_COOKIE,
            _error_code=server._error_code,
            _error_message=server._error_message,
            _safe_session_id=server._safe_session_id,
        )
        _mark_timing("runtime-import", started)
    return _RUNTIME


def _mark_timing(name: str, started: float) -> None:
    if _REQUEST_TIMINGS is not None:
        _REQUEST_TIMINGS.append((name, (time.perf_counter() - started) * 1000))


def _server_timing() -> str | None:
    if not _REQUEST_TIMINGS:
        return None
    return ", ".join(
        f"{name};dur={duration:.1f}" for name, duration in _REQUEST_TIMINGS
    )

_STATIC_ROOT = PROJECT_ROOT / "workbench" / "static"
_STATIC_TYPES = {
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "vendor/mermaid-11.16.0.min.js": "text/javascript; charset=utf-8",
}
_LOCAL_HOSTED_MARKER = b'data-vercel-hosted="false"'
_VERCEL_HOSTED_MARKER = b'data-vercel-hosted="true"'


def _index_body(body: bytes) -> bytes:
    """Expose Vercel's built-in deployment signal to the static app only."""

    if os.environ.get("VERCEL", "").strip() != "1":
        return body
    return body.replace(_LOCAL_HOSTED_MARKER, _VERCEL_HOSTED_MARKER, 1)


def _error_response(error: ApiError) -> JSONResponse:
    runtime = _runtime()
    return JSONResponse(
        {"error": {"code": error.code, "message": runtime["server"]._safe_error_message(error)}},
        status_code=error.status,
        headers={"Cache-Control": "no-store"},
    )


def _json_response(payload: Any, status_code: int = 200) -> JSONResponse:
    started = time.perf_counter()
    response = JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )
    _mark_timing("response-json", started)
    return response


def _public_json_response(
    payload: Any,
    request_headers: Mapping[str, str] | None = None,
    *,
    max_age: int = 30,
) -> Response:
    """Cache only sanitized, cookie-independent public payloads."""

    response = _json_response(payload)
    etag = '"' + hashlib.sha256(response.body).hexdigest() + '"'
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = (
        f"public, max-age=0, s-maxage={max_age}, stale-while-revalidate=120"
    )
    if request_headers and request_headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": response.headers["Cache-Control"]},
        )
    return response


def _auth_error(exc: AuthError) -> ApiError:
    runtime = _runtime()
    return runtime["ApiError"](exc.code, exc.message, exc.status)


def _require_principal(
    app: WorkbenchApp,
    headers: Mapping[str, str] | None,
    cookies: Mapping[str, str] | None,
    principal: Principal | None = None,
) -> Principal:
    if principal is not None:
        return principal
    try:
        started = time.perf_counter()
        result = app.authenticate(headers or {}, cookies or {})
        _mark_timing("auth-resolve", started)
        return result
    except _runtime()["AuthError"] as exc:
        _mark_timing("auth-resolve-error", started)
        raise _auth_error(exc) from exc


def _apply_auth_cookies(response: JSONResponse, app: WorkbenchApp, result: Any) -> JSONResponse:
    session_token = getattr(result, "session_token", None)
    csrf_token = getattr(result, "csrf_token", None)
    if session_token:
        response.set_cookie(
            AUTH_SESSION_COOKIE,
            session_token,
            max_age=AUTH_SESSION_SECONDS,
            httponly=True,
            secure=app.cookie_secure,
            samesite="lax",
            path="/",
        )
    if csrf_token:
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=AUTH_SESSION_SECONDS,
            httponly=False,
            secure=app.cookie_secure,
            samesite="lax",
            path="/",
        )
    if getattr(result, "clear_session", False):
        response.delete_cookie(
            AUTH_SESSION_COOKIE,
            secure=app.cookie_secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.delete_cookie(
            CSRF_COOKIE,
            secure=app.cookie_secure,
            httponly=False,
            samesite="lax",
            path="/",
        )
    return response


def _static_response(name: str) -> Response:
    if name not in {"index.html", *tuple(_STATIC_TYPES)}:
        raise ApiError("P4_STATIC_NOT_FOUND", "Static resource not found.", 404)
    path = _STATIC_ROOT / name
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise ApiError(
            "P4_STATIC_NOT_FOUND", "Static resource not found.", 404
        ) from exc
    content_type = (
        "text/html; charset=utf-8" if name == "index.html" else _STATIC_TYPES[name]
    )
    if name == "index.html":
        body = _index_body(body)
    if name == "index.html":
        cache_control = "public, max-age=0, must-revalidate, s-maxage=60"
    elif name.startswith("vendor/"):
        cache_control = "public, max-age=31536000, immutable"
    else:
        cache_control = "public, max-age=0, must-revalidate, s-maxage=31536000"
    return Response(body, media_type=content_type, headers={"Cache-Control": cache_control})


def _path_parts(path: str) -> list[str]:
    return [unquote(part) for part in path.split("/") if part]


def _handle_get(
    app: WorkbenchApp,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
    principal: Principal | None = None,
) -> Response:
    if path == "/api/health":
        return _json_response({"status": "ok"})
    if path == "/api/auth/csrf":
        try:
            result = app.auth_csrf(cookies or {})
        except AuthError as exc:
            raise _auth_error(exc) from exc
        return _apply_auth_cookies(_json_response(result.payload), app, result)
    if path == "/api/auth/me":
        try:
            return _json_response(app.auth_me(headers or {}, cookies or {}))
        except AuthError as exc:
            raise _auth_error(exc) from exc
    if path in {"/", "/index.html"}:
        return _static_response("index.html")
    if path.startswith("/static/"):
        return _static_response(path.removeprefix("/static/"))
    _runtime()

    if path == "/api/public/sessions":
        return _public_json_response(
            app.list_sessions(None, include_legacy=False), headers, max_age=30
        )
    public_parts = _path_parts(path)
    if len(public_parts) == 4 and public_parts[:3] == ["api", "public", "sessions"]:
        session_id = _safe_session_id(public_parts[3])
        if app.is_shared_session(session_id):
            return _public_json_response(app.view_shared(session_id), headers, max_age=300)
        if app.public_publisher_id:
            return _public_json_response(app.view_public(session_id), headers, max_age=300)
        raise ApiError("P4_SESSION_NOT_FOUND", "Session does not exist.", 404)

    if path == "/api/sessions":
        if principal is None:
            try:
                started = time.perf_counter()
                principal = app.authenticate(headers or {}, cookies or {})
                _mark_timing("auth-resolve", started)
            except AuthError as exc:
                if exc.code not in {"P4_AUTH_REQUIRED", "P4_AUTH_INVALID_SESSION"}:
                    raise _auth_error(exc) from exc
                principal = None
        owner_id = principal.user_id if principal is not None and not app.test_mode else None
        started = time.perf_counter()
        result = app.list_sessions(owner_id, include_legacy=app.test_mode)
        _mark_timing("library", started)
        return _json_response(result)
    parts = _path_parts(path)
    if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
        session_id = _safe_session_id(parts[2])
        if app.is_shared_session(session_id):
            return _json_response(app.view_shared(session_id))
        if app.public_publisher_id:
            viewer_id = None
            session_cookie = (cookies or {}).get(AUTH_SESSION_COOKIE)
            if session_cookie:
                try:
                    viewer_id = app.authenticate(headers or {}, cookies or {}).user_id
                except AuthError as exc:
                    if exc.code not in {"P4_AUTH_REQUIRED", "P4_AUTH_INVALID_SESSION"}:
                        raise _auth_error(exc) from exc
            if viewer_id != app.public_publisher_id:
                try:
                    return _json_response(app.view_public(session_id))
                except ApiError as exc:
                    if exc.code != "P4_SESSION_NOT_FOUND":
                        raise

    principal = _require_principal(app, headers, cookies, principal)
    owner_id = principal.user_id if not app.test_mode else None
    if path == "/api/settings":
        started = time.perf_counter()
        result = app.settings(owner_id)
        _mark_timing("settings", started)
        return _json_response(result)
    if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
        session_id = _safe_session_id(parts[2])
        with app._lock(session_id, owner_id):
            started = time.perf_counter()
            session = app._load_session(session_id, owner_id)
            _mark_timing("session-load", started)
            started = time.perf_counter()
            result = app.view(session, owner_id)
            _mark_timing("view-build", started)
            return _json_response(result)
    if len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[3] == "download":
        body, content_type, filename = app.download(parts[2], parts[4], owner_id)
        return Response(
            body,
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )
    raise ApiError("P4_ROUTE_NOT_FOUND", "Route not found.", 404)


def _decode_body(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_JSON_BYTES:
        raise ApiError("P4_REQUEST_TOO_LARGE", "Request body is too large.", 413)
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(
            "P4_INVALID_JSON_REQUEST", "Request body must be valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ApiError(
            "P4_REQUEST_OBJECT_REQUIRED", "Request body must be a JSON object."
        )
    return payload


def _handle_post(
    app: WorkbenchApp,
    path: str,
    body: dict[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
    client_ip: str | None = None,
    principal: Principal | None = None,
) -> Response:
    _runtime()
    headers = headers or {}
    cookies = cookies or {}
    if path in {"/api/auth/register", "/api/auth/login"}:
        try:
            result = (
                app.auth_register(body, headers=headers, cookies=cookies)
                if path.endswith("/register")
                else app.auth_login(
                    body,
                    headers=headers,
                    cookies=cookies,
                    client_ip=client_ip,
                )
            )
        except AuthError as exc:
            raise _auth_error(exc) from exc
        return _apply_auth_cookies(_json_response(result.payload), app, result)
    if path == "/api/auth/logout":
        try:
            result = app.auth_logout(headers=headers, cookies=cookies)
        except AuthError as exc:
            raise _auth_error(exc) from exc
        return _apply_auth_cookies(_json_response(result.payload), app, result)

    principal = _require_principal(app, headers, cookies, principal)
    owner_id = principal.user_id if not app.test_mode else None
    if path == "/api/settings":
        try:
            checked = app.require_csrf(headers, cookies)
        except AuthError as exc:
            raise _auth_error(exc) from exc
        if not app.test_mode and checked.user_id != principal.user_id:
            raise ApiError("P4_AUTH_REQUIRED", "Sign in to use AutoGlific.", 401)
        return _json_response(app.save_settings(owner_id or principal.user_id, body))
    parts = _path_parts(path)
    if path == "/api/sessions":
        try:
            app.require_csrf(headers, cookies)
        except AuthError as exc:
            raise _auth_error(exc) from exc
        return _json_response(app.start(body, owner_id), 201)
    if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
        actions = {
            "propose": app.propose,
            "answer": app.answer,
            "prepare-confirmation": app.prepare,
            "freeze": app.freeze,
            "compile": app.compile,
            "publish": app.publish,
        }
        action = parts[3]
        if action == "delete":
            try:
                app.require_csrf(headers, cookies)
            except AuthError as exc:
                raise _auth_error(exc) from exc
            return _json_response(app.delete(parts[2], body, owner_id))
        if action not in actions:
            raise ApiError("P4_ROUTE_NOT_FOUND", "Route not found.", 404)
        try:
            app.require_csrf(headers, cookies)
        except AuthError as exc:
            raise _auth_error(exc) from exc
        return _json_response(actions[action](parts[2], body, owner_id))
    raise ApiError("P4_ROUTE_NOT_FOUND", "Route not found.", 404)


def _handle_delete(
    app: WorkbenchApp,
    path: str,
    body: dict[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
    principal: Principal | None = None,
) -> Response:
    _runtime()
    headers = headers or {}
    cookies = cookies or {}
    principal = _require_principal(app, headers, cookies, principal)
    owner_id = principal.user_id if not app.test_mode else None
    parts = _path_parts(path)
    if len(parts) != 3 or parts[:2] != ["api", "sessions"]:
        raise ApiError("P4_ROUTE_NOT_FOUND", "Route not found.", 404)
    try:
        app.require_csrf(headers, cookies)
    except AuthError as exc:
        raise _auth_error(exc) from exc
    return _json_response(app.delete(parts[2], body, owner_id))


if FastAPI is not None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    workbench_app: Any | None = None

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    async def workbench_route(request: Request, path: str) -> Response:
        global workbench_app, _REQUEST_TIMINGS
        _REQUEST_TIMINGS = []
        request_started = time.perf_counter()
        try:
            normalized = "/" + path if path else "/"
            if normalized == "/api/health":
                response = _json_response({"status": "ok"})
                response.headers["Server-Timing"] = "health;dur=0"
                return response
            lightweight_static = normalized in {"/", "/index.html"} or normalized.startswith("/static/")
            db_token = db_connection = None
            db_started = None
            should_commit = request.method != "GET"
            if not lightweight_static:
                from product4.workbench.request_db import open_request_connection, close_request_connection

                db_started = time.perf_counter()
                db_token, db_connection = open_request_connection()
                _mark_timing("request-db-open", db_started)
                runtime_started = time.perf_counter()
                _runtime()
                _mark_timing("runtime-ready", runtime_started)
                if workbench_app is None:
                    bootstrap_started = time.perf_counter()
                    workbench_app = WorkbenchApp()
                    _mark_timing("app-bootstrap", bootstrap_started)
            headers = dict(request.headers)
            cookies = request.cookies
            if request.method == "GET":
                response = _handle_get(
                    workbench_app,
                    normalized,
                    headers=headers,
                    cookies=cookies,
                )
            elif request.method == "DELETE":
                response = _handle_delete(
                    workbench_app,
                    normalized,
                    _decode_body(await request.body()),
                    headers=headers,
                    cookies=cookies,
                )
            else:
                response = _handle_post(
                    workbench_app,
                    normalized,
                    _decode_body(await request.body()),
                    headers=headers,
                    cookies=cookies,
                    client_ip=request.client.host if request.client else None,
                )
            _mark_timing("request-total", request_started)
            timing = _server_timing()
            if timing:
                response.headers["Server-Timing"] = timing
            return response
        except Exception as exc:  # noqa: BLE001 - safe API boundary
            runtime = _runtime()
            if isinstance(exc, runtime["ApiError"]):
                response = _error_response(exc)
            elif isinstance(exc, runtime["AuthError"]):
                response = _error_response(_auth_error(exc))
            else:
                response = _error_response(
                    runtime["ApiError"](
                        runtime["server"]._error_code(exc),
                        runtime["server"]._error_message(exc),
                        500,
                    )
                )
            _mark_timing("request-total", request_started)
            timing = _server_timing()
            if timing:
                response.headers["Server-Timing"] = timing
            return response
        finally:
            if 'db_token' in locals():
                from product4.workbench.request_db import close_request_connection

                close_started = time.perf_counter()
                close_request_connection(db_token, db_connection, commit=should_commit)
                _mark_timing("request-db-close", close_started)
                if "response" in locals():
                    timing = _server_timing()
                    if timing:
                        response.headers["Server-Timing"] = timing
else:
    # This keeps source-only checks importable; the declared deployment
    # manifest always installs FastAPI before Vercel evaluates ``app``.
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:  # type: ignore[no-redef]
        if scope.get("type") != "http":
            return
        body = b'{"error":{"code":"P4_DEPLOYMENT_DEPENDENCY_MISSING","message":"Hosted web dependencies are not installed."}}'
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = ["app"]
