"""Small server-side client for the Glific import-and-publish contract.

The client deliberately accepts only the already compiled Engine 3 artifact.
It keeps credentials and session tokens in memory and never includes them in
errors or returned result objects.
"""

from __future__ import annotations

import copy
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit


class GlificClientError(RuntimeError):
    """A safe, user-facing Glific operation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


Transport = Callable[[str, str, dict[str, Any] | None, dict[str, str]], tuple[int, Any]]


@dataclass(frozen=True, repr=False)
class GlificConfig:
    """Validated runtime configuration; secret fields are intentionally hidden."""

    ui_base_url: str
    api_base_url: str
    phone: str = field(repr=False)
    password: str = field(repr=False)
    timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> GlificConfig:
        values = environ if environ is not None else os.environ
        raw_base = (
            values.get("GLIFIC_BASE_URL")
            or values.get("GLIFIC_PRODUCTION_BASE_URL")
            or ""
        ).strip()
        phone = str(values.get("GLIFIC_PHONE") or "").strip()
        password = str(values.get("GLIFIC_PASSWORD") or "").strip()
        missing = []
        if not raw_base:
            missing.append("GLIFIC_BASE_URL or GLIFIC_PRODUCTION_BASE_URL")
        if not phone:
            missing.append("GLIFIC_PHONE")
        if not password:
            missing.append("GLIFIC_PASSWORD")
        if missing:
            raise GlificClientError(
                "P4_GLIFIC_CONFIGURATION_MISSING",
                "Glific publishing is not configured. Set " + ", ".join(missing) + ".",
            )
        ui_base_url, api_base_url = _normalize_base_url(raw_base)
        try:
            timeout = float(values.get("GLIFIC_TIMEOUT_SECONDS", "30"))
        except ValueError as exc:
            raise GlificClientError(
                "P4_GLIFIC_CONFIGURATION_INVALID",
                "GLIFIC_TIMEOUT_SECONDS must be a number between 1 and 120.",
            ) from exc
        if not 1 <= timeout <= 120:
            raise GlificClientError(
                "P4_GLIFIC_CONFIGURATION_INVALID",
                "GLIFIC_TIMEOUT_SECONDS must be a number between 1 and 120.",
            )
        return cls(ui_base_url, api_base_url, phone, password, timeout)

    def __repr__(self) -> str:
        return (
            f"GlificConfig(ui_base_url={self.ui_base_url!r}, "
            f"api_base_url={self.api_base_url!r}, timeout_seconds={self.timeout_seconds!r})"
        )


def _normalize_base_url(raw: str) -> tuple[str, str]:
    """Normalize a tenant URL and derive the documented ``api.`` origin."""

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise GlificClientError(
            "P4_GLIFIC_CONFIGURATION_INVALID",
            "The Glific base URL is invalid.",
        ) from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise GlificClientError(
            "P4_GLIFIC_CONFIGURATION_INVALID",
            "The Glific base URL must be an HTTPS tenant URL.",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GlificClientError(
            "P4_GLIFIC_CONFIGURATION_INVALID",
            "The Glific base URL must not contain credentials, query parameters, or fragments.",
        )
    if parsed.path not in {"", "/"}:
        raise GlificClientError(
            "P4_GLIFIC_CONFIGURATION_INVALID",
            "The Glific base URL must point to the tenant origin, not an API path.",
        )
    host = parsed.hostname.lower()
    api_host = host if host.startswith("api.") else f"api.{host}"
    host_for_ui = host.removeprefix("api.")
    port_suffix = f":{port}" if port is not None else ""
    ui_base = f"https://{host_for_ui}{port_suffix}"
    api_base = f"https://{api_host}{port_suffix}"
    return ui_base, api_base


def _safe_detail(value: Any, secrets: tuple[str, ...] = ()) -> str:
    """Keep remote error context useful without returning credentials or tokens."""

    if isinstance(value, dict):
        value = value.get("message") or value.get("error") or value.get("detail") or ""
    elif isinstance(value, list):
        value = value[0] if value else ""
        return _safe_detail(value, secrets)
    text = str(value or "").strip()
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)(bearer\s+)[a-z0-9._+/=-]+", r"\1[redacted]", text)
    return text[:240]


def _schema_shape_error(errors: list[Any]) -> bool:
    detail = _safe_detail(errors).casefold()
    return any(
        marker in detail
        for marker in (
            "cannot query field",
            "unknown field",
            "unknown type",
            "doesn't exist",
            "does not exist",
            "field argument",
        )
    )


def _interactive_action_ids(definition: Mapping[str, Any]) -> set[str]:
    return {
        str(action.get("uuid"))
        for node in definition.get("nodes", [])
        if isinstance(node, dict)
        for action in node.get("actions", [])
        if isinstance(action, dict)
        and action.get("type") == "send_interactive_msg"
        and action.get("uuid")
    }


def _remap_interactive_ids(definition: dict[str, Any], latest: Mapping[str, Any]) -> dict[str, Any]:
    """Keep Glific's server-assigned interactive template IDs on a saved draft."""

    cleaned = copy.deepcopy(definition)
    ids_by_action_uuid = {
        str(action["uuid"]): action["id"]
        for node in latest.get("nodes", [])
        if isinstance(node, dict)
        for action in node.get("actions", [])
        if isinstance(action, dict)
        and action.get("type") == "send_interactive_msg"
        and action.get("uuid")
        and "id" in action
    }
    for node in cleaned.get("nodes", []):
        if not isinstance(node, dict):
            continue
        for action in node.get("actions", []):
            if not isinstance(action, dict):
                continue
            action_uuid = str(action.get("uuid"))
            if action.get("type") == "send_interactive_msg" and action_uuid in ids_by_action_uuid:
                action["id"] = ids_by_action_uuid[action_uuid]
    return cleaned


class GlificClient:
    """Authenticate, import, save, and publish one compiled Glific artifact."""

    def __init__(self, config: GlificConfig, transport: Transport | None = None) -> None:
        self.config = config
        self._transport = transport or self._urlopen_transport
        self._token: str | None = None

    @classmethod
    def from_environment(cls) -> GlificClient:
        return cls(GlificConfig.from_environment())

    @classmethod
    def from_values(
        cls,
        base_url: str,
        phone: str,
        password: str,
        *,
        timeout_seconds: float = 30.0,
        transport: Transport | None = None,
    ) -> GlificClient:
        """Build a client from already validated per-user settings."""

        ui_base_url, api_base_url = _normalize_base_url(base_url)
        if not phone.strip() or not password.strip():
            raise GlificClientError(
                "P4_GLIFIC_CONFIGURATION_MISSING",
                "Glific publishing is not configured.",
            )
        if not 1 <= timeout_seconds <= 120:
            raise GlificClientError(
                "P4_GLIFIC_CONFIGURATION_INVALID",
                "Glific timeout must be between 1 and 120 seconds.",
            )
        return cls(
            GlificConfig(ui_base_url, api_base_url, phone.strip(), password, timeout_seconds),
            transport=transport,
        )

    def _urlopen_transport(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> tuple[int, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
                return response.status, json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GlificClientError(
                "P4_GLIFIC_API_UNAVAILABLE",
                "Glific could not be reached. Check the configured tenant URL and try again.",
            ) from exc

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        operation: str,
    ) -> dict[str, Any]:
        status, response = self._transport(method, url, payload, headers)
        if not isinstance(response, dict):
            raise GlificClientError(
                "P4_GLIFIC_RESPONSE_INVALID",
                f"Glific returned an invalid response during {operation}.",
            )
        if status in {401, 403}:
            raise GlificClientError(
                "P4_GLIFIC_AUTHENTICATION_FAILED",
                "Glific authentication was rejected. Check the server-side phone and password.",
            )
        if status >= 400:
            code = {
                "authentication": "P4_GLIFIC_AUTHENTICATION_FAILED",
                "import": "P4_GLIFIC_IMPORT_FAILED",
                "revision_save": "P4_GLIFIC_REVISION_SAVE_FAILED",
                "publish": "P4_GLIFIC_PUBLISH_FAILED",
            }.get(operation, "P4_GLIFIC_API_UNAVAILABLE")
            raise GlificClientError(
                code,
                f"Glific {operation} request failed (HTTP {status}).",
            )
        return response

    def _authenticate(self) -> None:
        if self._token:
            return
        response = self._request(
            "POST",
            f"{self.config.api_base_url}/api/v1/session",
            {"user": {"phone": self.config.phone, "password": self.config.password}},
            {"Content-Type": "application/json"},
            "authentication",
        )
        token = response.get("data", {}).get("access_token") if isinstance(response.get("data"), dict) else None
        if not isinstance(token, str) or not token:
            raise GlificClientError(
                "P4_GLIFIC_AUTHENTICATION_FAILED",
                "Glific authentication did not return a session token. Check the server-side phone and password.",
            )
        self._token = token

    def _graphql_raw(
        self,
        query: str,
        variables: dict[str, Any] | None,
        operation: str,
    ) -> tuple[dict[str, Any] | None, list[Any]]:
        self._authenticate()
        response = self._request(
            "POST",
            f"{self.config.api_base_url}/api",
            {"query": query, "variables": variables or {}},
            {"Authorization": self._token or "", "Content-Type": "application/json"},
            operation,
        )
        errors = response.get("errors")
        if errors:
            return None, errors if isinstance(errors, list) else [errors]
        data = response.get("data")
        if not isinstance(data, dict):
            raise GlificClientError(
                "P4_GLIFIC_RESPONSE_INVALID",
                f"Glific returned no usable data during {operation}.",
            )
        return data, []

    def _graphql(self, query: str, variables: dict[str, Any] | None, operation: str) -> dict[str, Any]:
        data, errors = self._graphql_raw(query, variables, operation)
        if errors:
            detail = _safe_detail(errors, (self.config.phone, self.config.password, self._token or ""))
            suffix = f": {detail}" if detail else "."
            code = "P4_GLIFIC_PUBLISH_FAILED" if operation == "publish" else "P4_GLIFIC_IMPORT_FAILED" if operation == "import" else "P4_GLIFIC_API_UNAVAILABLE"
            raise GlificClientError(code, f"Glific rejected the {operation} request{suffix}")
        return data or {}

    def _import(self, artifact: dict[str, Any]) -> None:
        variables = {"flow": json.dumps(artifact, ensure_ascii=False, separators=(",", ":"))}
        queries = (
            "mutation($flow: Json) { importFlow(flow: $flow) { success errors { key message } } }",
            "mutation($flow: JSON!) { importFlow(flow: $flow) { success errors { key message } } }",
            "mutation($flow: Json) { importFlow(flow: $flow) { status { status } } }",
            "mutation($flow: JSON!) { importFlow(flow: $flow) { status { status } } }",
        )
        last_shape_errors: list[Any] = []
        for query in queries:
            data, errors = self._graphql_raw(query, variables, "import")
            if errors:
                if _schema_shape_error(errors):
                    last_shape_errors = errors
                    continue
                detail = _safe_detail(errors, (self.config.phone, self.config.password, self._token or ""))
                raise GlificClientError(
                    "P4_GLIFIC_IMPORT_FAILED",
                    "Glific rejected the flow import" + (f": {detail}" if detail else "."),
                )
            result = data.get("importFlow") if data else None
            if not isinstance(result, dict):
                raise GlificClientError(
                    "P4_GLIFIC_IMPORT_FAILED",
                    "Glific returned no import result.",
                )
            if "success" in result:
                if result.get("success") is True:
                    return
                detail = _safe_detail(result.get("errors"), (self.config.phone, self.config.password, self._token or ""))
                raise GlificClientError(
                    "P4_GLIFIC_IMPORT_FAILED",
                    "Glific rejected the flow import" + (f": {detail}" if detail else "."),
                )
            statuses = result.get("status")
            status = statuses[0].get("status") if isinstance(statuses, list) and statuses and isinstance(statuses[0], dict) else None
            if status == "Successfully imported":
                return
            raise GlificClientError(
                "P4_GLIFIC_IMPORT_FAILED",
                "Glific did not confirm the flow import." + (f" Detail: {status}" if status else ""),
            )
        detail = _safe_detail(last_shape_errors, (self.config.phone, self.config.password, self._token or ""))
        raise GlificClientError(
            "P4_GLIFIC_IMPORT_FAILED",
            "The configured Glific instance does not expose a supported import response contract."
            + (f" Detail: {detail}" if detail else ""),
        )

    def _find_identity(self, name: str, expected_uuid: str) -> dict[str, Any] | None:
        data = self._graphql(
            "query($filter: FlowFilter) { flows(filter: $filter) { id uuid name isActive lastPublishedAt } }",
            {"filter": {"name": name}},
            "identity",
        )
        flows = data.get("flows")
        if not isinstance(flows, list):
            raise GlificClientError(
                "P4_GLIFIC_FLOW_IDENTITY_FAILED",
                "Glific returned an invalid flow identity response.",
            )
        exact_name_matches: list[dict[str, Any]] = []
        for item in flows:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("uuid"), str)
                or item.get("id") is None
                or not isinstance(item.get("isActive"), bool)
                or not (
                    item.get("lastPublishedAt") is None
                    or isinstance(item.get("lastPublishedAt"), str)
                )
            ):
                raise GlificClientError(
                    "P4_GLIFIC_FLOW_IDENTITY_FAILED",
                    "Glific returned an invalid flow identity response.",
                )
            if item["name"] == name:
                exact_name_matches.append(item)
        if len(exact_name_matches) > 1:
            raise GlificClientError(
                "P4_GLIFIC_FLOW_IDENTITY_FAILED",
                "Glific returned more than one exact-name flow identity.",
            )
        if not exact_name_matches:
            return None
        match = exact_name_matches[0]
        if match["uuid"] != expected_uuid:
            raise GlificClientError(
                "P4_GLIFIC_FLOW_NAME_COLLISION",
                "A Glific flow with this name already exists. Rename the new flow before publishing.",
            )
        return {
            "flow_id": str(match.get("id")) if match.get("id") is not None else None,
            "flow_uuid": expected_uuid,
            "flow_name": name,
            "is_active": match.get("isActive") is True,
            "last_published_at": match.get("lastPublishedAt"),
        }

    def _resolve_identity(self, name: str, expected_uuid: str) -> dict[str, Any]:
        identity = self._find_identity(name, expected_uuid)
        if identity is None:
            raise GlificClientError(
                "P4_GLIFIC_FLOW_IDENTITY_FAILED",
                "Glific accepted the import but did not return one exact matching flow identity.",
            )
        return identity

    @staticmethod
    def _is_confirmed_published(identity: Mapping[str, Any]) -> bool:
        return identity.get("is_active") is True and isinstance(identity.get("last_published_at"), str) and bool(identity["last_published_at"].strip())

    @staticmethod
    def _public_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "flow_id": identity.get("flow_id"),
            "flow_uuid": identity["flow_uuid"],
            "flow_name": identity["flow_name"],
        }

    def _readback_published(self, name: str, flow_uuid: str) -> dict[str, Any] | None:
        identity = self._find_identity(name, flow_uuid)
        return identity if identity is not None and self._is_confirmed_published(identity) else None

    def _save_revision(self, definition: dict[str, Any], identity: Mapping[str, Any]) -> None:
        action_ids = _interactive_action_ids(definition)
        saved_definition = copy.deepcopy(definition)
        if action_ids:
            response = self._request(
                "GET",
                f"{self.config.api_base_url}/flow-editor/flows/{quote(str(identity['flow_uuid']), safe='')}",
                None,
                {"Authorization": self._token or ""},
                "revision_save",
            )
            latest = response.get("results", response)
            if not isinstance(latest, dict):
                raise GlificClientError(
                    "P4_GLIFIC_REVISION_SAVE_FAILED",
                    "Glific returned an invalid flow-editor draft.",
                )
            saved_definition = _remap_interactive_ids(saved_definition, latest)
        response = self._request(
            "POST",
            f"{self.config.api_base_url}/flow-editor/revisions/{quote(str(identity['flow_uuid']), safe='')}",
            saved_definition,
            {"Authorization": self._token or "", "Content-Type": "application/json"},
            "revision_save",
        )
        if not response.get("revision"):
            raise GlificClientError(
                "P4_GLIFIC_REVISION_SAVE_FAILED",
                "Glific did not confirm saving the imported draft revision.",
            )

    def _publish(self, flow_uuid: str) -> None:
        data = self._graphql(
            "mutation($uuid: UUID4!) { publishFlow(uuid: $uuid) { success errors { key message } } }",
            {"uuid": flow_uuid},
            "publish",
        )
        result = data.get("publishFlow")
        if not isinstance(result, dict) or result.get("success") is not True:
            detail = _safe_detail(result.get("errors") if isinstance(result, dict) else None, (self.config.phone, self.config.password, self._token or ""))
            raise GlificClientError(
                "P4_GLIFIC_PUBLISH_FAILED",
                "Glific did not confirm publication" + (f": {detail}" if detail else "."),
            )

    def publish_artifact(
        self,
        artifact: dict[str, Any],
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Publish one structurally validated Engine 3 artifact."""

        if not isinstance(artifact, dict):
            raise GlificClientError("P4_GLIFIC_ARTIFACT_INVALID", "The compiled Glific artifact is not an object.")
        flows = artifact.get("flows")
        if not isinstance(flows, list) or len(flows) != 1 or not isinstance(flows[0], dict):
            raise GlificClientError("P4_GLIFIC_ARTIFACT_INVALID", "The compiled Glific artifact has no single flow.")
        definition = flows[0].get("definition")
        if not isinstance(definition, dict) or not isinstance(definition.get("uuid"), str) or not isinstance(definition.get("name"), str):
            raise GlificClientError("P4_GLIFIC_ARTIFACT_INVALID", "The compiled Glific artifact has no usable flow identity.")
        if progress:
            progress("connecting")
        self._authenticate()
        identity = self._find_identity(definition["name"], definition["uuid"])
        if identity is not None and self._is_confirmed_published(identity):
            return {**self._public_identity(identity), "status": "published"}
        if identity is None:
            if progress:
                progress("importing")
            self._import(artifact)
            identity = self._resolve_identity(definition["name"], definition["uuid"])
        self._save_revision(definition, identity)
        if progress:
            progress("publishing")
        try:
            self._publish(identity["flow_uuid"])
        except GlificClientError:
            try:
                readback = self._readback_published(definition["name"], identity["flow_uuid"])
            except GlificClientError:
                readback = None
            if readback is not None:
                return {**self._public_identity(readback), "status": "published"}
            raise
        try:
            readback = self._readback_published(definition["name"], identity["flow_uuid"])
        except GlificClientError as exc:
            raise GlificClientError(
                "P4_GLIFIC_PUBLISH_FAILED",
                "Glific did not confirm publication in its authoritative flow state.",
            ) from exc
        if readback is None:
            raise GlificClientError(
                "P4_GLIFIC_PUBLISH_FAILED",
                "Glific did not confirm publication in its authoritative flow state.",
            )
        return {**self._public_identity(readback), "status": "published"}


__all__ = ["GlificClient", "GlificClientError", "GlificConfig"]
