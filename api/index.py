"""Same-origin FastAPI entrypoint for the Product 4 workbench.

Vercel recognizes the module-level ``app`` ASGI object.  The route handlers
delegate to the existing ``WorkbenchApp`` methods so local and hosted API
contracts remain the same.
"""

from __future__ import annotations

import json
import sys
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

from product4.workbench.server import (
    MAX_JSON_BYTES,
    PROJECT_ROOT,
    ApiError,
    WorkbenchApp,
    _error_code,
    _error_message,
    _safe_session_id,
)

_STATIC_ROOT = PROJECT_ROOT / "workbench" / "static"
_STATIC_TYPES = {
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "vendor/mermaid-11.16.0.min.js": "text/javascript; charset=utf-8",
}


def _error_response(error: ApiError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": error.code, "message": error.message}},
        status_code=error.status,
        headers={"Cache-Control": "no-store"},
    )


def _json_response(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


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
    return Response(
        body,
        media_type=content_type,
        headers={"Cache-Control": "no-store"},
    )


def _path_parts(path: str) -> list[str]:
    return [unquote(part) for part in path.split("/") if part]


def _handle_get(app: WorkbenchApp, path: str) -> Response:
    if path == "/api/health":
        return _json_response(
            {"status": "ok", "data_root": str(PROJECT_ROOT / ".workbench-data")}
        )
    if path == "/api/sessions":
        return _json_response(app.list_sessions())
    if path == "/api/settings":
        return _json_response(app.settings())
    if path in {"/", "/index.html"}:
        return _static_response("index.html")
    if path.startswith("/static/"):
        return _static_response(path.removeprefix("/static/"))
    parts = _path_parts(path)
    if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
        session_id = _safe_session_id(parts[2])
        with app._lock(session_id):
            return _json_response(app.view(app._load_session(session_id)))
    if len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[3] == "download":
        body, content_type, filename = app.download(parts[2], parts[4])
        return Response(
            body,
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
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


def _handle_post(app: WorkbenchApp, path: str, body: dict[str, Any]) -> Response:
    parts = _path_parts(path)
    if path == "/api/sessions":
        return _json_response(app.start(body), 201)
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
        if action not in actions:
            raise ApiError("P4_ROUTE_NOT_FOUND", "Route not found.", 404)
        return _json_response(actions[action](parts[2], body))
    raise ApiError("P4_ROUTE_NOT_FOUND", "Route not found.", 404)


if FastAPI is not None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    workbench_app = WorkbenchApp()

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def workbench_route(request: Request, path: str) -> Response:
        try:
            normalized = "/" + path if path else "/"
            if request.method == "GET":
                return _handle_get(workbench_app, normalized)
            return _handle_post(
                workbench_app, normalized, _decode_body(await request.body())
            )
        except ApiError as exc:
            return _error_response(exc)
        except Exception as exc:  # noqa: BLE001 - last-resort API boundary
            return _error_response(ApiError(_error_code(exc), _error_message(exc), 500))
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
