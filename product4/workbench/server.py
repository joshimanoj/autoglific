"""Minimal local HTTP server for the Product 4 internal workbench."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import uuid
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTOBUILDER_ROOT = PROJECT_ROOT.parent

if str(AUTOBUILDER_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOBUILDER_ROOT))

from product4.authoring.brief_translation import (
    IncrementalSemanticModelClient,
    SemanticTranslationError,
    safe_validation_fingerprint,
    safe_network_subtype,
)
from product4.authoring.freeze import freeze, prepare_confirmation
from product4.authoring.interpreter import RegistryInterpreter
from product4.authoring.review import (
    authored_mermaid_review,
    authored_presentation_mermaid,
    expanded_mermaid_review,
    text_review,
)
from product4.authoring.session import AuthoringService
from product4.contracts.package_boundary import (
    canonical_authoring_package_hash,
    validate_frozen_package,
)
from product4.contracts.questions import QuestionAnswer
from product4.contracts.session import AuthoringSession, SessionState
from product4.workbench.glific_client import (
    GlificClient,
    GlificClientError,
    GlificConfig,
    _normalize_base_url,
)
from product4.workbench.pipeline import run_pipeline
from product4.workbench.storage import (
    PublishLeaseBusy,
    StorageError,
    StorageNotFound,
    StorageRevisionConflict,
    build_storage,
)

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_JSON_BYTES = 8 * 1024 * 1024
DATA_ROOT = Path(
    os.environ.get("PRODUCT4_WORKBENCH_DATA", str(PROJECT_ROOT / ".workbench-data"))
).expanduser()
SESSIONS_ROOT = DATA_ROOT / "sessions"
ARTIFACTS_ROOT = DATA_ROOT / "artifacts"
CONFIRMATIONS_ROOT = DATA_ROOT / "confirmations"
GLIFIC_RESULTS_ROOT = DATA_ROOT / "glific-publishes"
# Vercel's configured 300-second function ceiling plus bounded cleanup and a
# safety margin.  Expiry therefore recovers at 420 seconds, while an old owner
# cannot record a result after the lease has expired.
PUBLISH_LEASE_SECONDS = 420
_REQUEST_ID_RE = re.compile(r"^REQ-[A-F0-9]{12}$")
_BRANCH_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_LOGGER = logging.getLogger("product4.autoglific")


def _assert_non_evidence_path(path: Path) -> None:
    resolved = path.resolve()
    completed = (PROJECT_ROOT / "completed").resolve()
    if resolved == completed or completed in resolved.parents:
        raise RuntimeError("P4_WORKBENCH_DATA_MUST_NOT_BE_COMPLETED")


_assert_non_evidence_path(DATA_ROOT)
if not os.environ.get("DATABASE_URL", "").strip():
    for _directory in (SESSIONS_ROOT, ARTIFACTS_ROOT, CONFIRMATIONS_ROOT, GLIFIC_RESULTS_ROOT):
        _directory.mkdir(parents=True, exist_ok=True)


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = HTTPStatus.BAD_REQUEST,
        *,
        request_id: str | None = None,
        validation_fingerprint: str | None = None,
        network_subtype: str | None = None,
        available_branches: list[str] | tuple[str, ...] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id if _REQUEST_ID_RE.fullmatch(str(request_id or "")) else _new_request_id()
        self.validation_fingerprint = safe_validation_fingerprint(validation_fingerprint)
        self.network_subtype = safe_network_subtype(network_subtype)
        self.available_branches = _safe_branch_labels(available_branches)


def _new_request_id() -> str:
    return f"REQ-{uuid.uuid4().hex[:12].upper()}"


def _safe_branch_labels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value[:10]:
        if not isinstance(item, str):
            continue
        label = item.strip()
        if not label or not _BRANCH_LABEL_RE.fullmatch(label) or label in result:
            continue
        result.append(label)
    return tuple(result)


def _open_branch_labels(session: AuthoringSession) -> tuple[str, ...]:
    """Expose only current authored branch labels for an ambiguity prompt."""

    try:
        context = AuthoringService._authoring_context(session)
    except Exception:
        return ()
    labels: list[str] = []
    for branch in context.get("open_branches", []):
        raw_labels = branch.get("labels") if isinstance(branch, dict) else None
        if not isinstance(raw_labels, list):
            continue
        label = " / ".join(
            item.strip()
            for item in raw_labels
            if isinstance(item, str) and item.strip()
        ) or "Main flow"
        labels.extend([label])
    return _safe_branch_labels(labels)


def _safe_session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ApiError(
            "P4_SESSION_ID_INVALID",
            "Session id must use letters, numbers, hyphens or underscores.",
        )
    return session_id


def _safe_title(value: Any) -> str:
    title = str(value or "").strip()
    if not title or len(title) > 200:
        raise ApiError("P4_SESSION_TITLE_INVALID", "Title must be 1-200 characters.")
    return title


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_glific_result(value: Any) -> dict[str, Any]:
    """Persist only the confirmed public flow identity, never remote extras."""

    if not isinstance(value, dict) or value.get("status") != "published":
        raise ApiError(
            "P4_GLIFIC_PUBLISH_FAILED",
            "Glific did not confirm publication.",
            HTTPStatus.BAD_GATEWAY,
        )
    flow_uuid = value.get("flow_uuid")
    flow_name = value.get("flow_name")
    if not isinstance(flow_uuid, str) or not flow_uuid or not isinstance(flow_name, str) or not flow_name:
        raise ApiError(
            "P4_GLIFIC_FLOW_IDENTITY_FAILED",
            "Glific confirmed publication without returning a usable flow identity.",
            HTTPStatus.BAD_GATEWAY,
        )
    safe: dict[str, Any] = {
        "flow_uuid": flow_uuid,
        "flow_name": flow_name,
        "status": "published",
    }
    flow_id = value.get("flow_id")
    if isinstance(flow_id, (str, int)) and not isinstance(flow_id, bool):
        safe["flow_id"] = str(flow_id)
    return safe


def _atomic_write_json(path: Path, value: Any) -> None:
    import os as _os
    import tempfile

    _assert_non_evidence_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".p4-workbench-", dir=path.parent)
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            _os.fsync(handle.fileno())
        _os.replace(temporary, path)
    except Exception:
        try:
            _os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ApiError("P4_NOT_FOUND", "AutoGlific resource not found.", HTTPStatus.NOT_FOUND) from exc
    except (OSError, ValueError) as exc:
        raise ApiError("P4_WORKBENCH_ARTIFACT_INVALID", "Stored AutoGlific data is invalid.", HTTPStatus.INTERNAL_SERVER_ERROR) from exc


def _error_code(exc: Exception) -> str:
    explicit = getattr(exc, "code", None)
    if explicit:
        return str(explicit)
    message = str(exc)
    match = re.match(r"(P4_[A-Z0-9_]+)", message)
    return match.group(1) if match else "P4_WORKBENCH_OPERATION_FAILED"


def _error_message(exc: Exception) -> str:
    message = str(exc)
    return message or exc.__class__.__name__


def _api_error_from_exception(
    exc: Exception,
    *,
    status: int = HTTPStatus.BAD_REQUEST,
) -> ApiError:
    return ApiError(
        _error_code(exc),
        _error_message(exc),
        status,
        request_id=getattr(exc, "request_id", None),
        validation_fingerprint=getattr(exc, "validation_fingerprint", None),
        network_subtype=getattr(exc, "network_subtype", None),
    )


def _safe_error_message(error: ApiError) -> str:
    """Return a public message without provider details or exception text."""

    code = error.code
    if code == "P4_SEMANTIC_CONFIGURATION_MISSING":
        return "AutoGlific semantic setup is unavailable."
    if code in {
        "P4_SEMANTIC_AUTHENTICATION_FAILED",
        "P4_SEMANTIC_PROJECT_ACCESS_FAILED",
        "P4_SEMANTIC_MODEL_UNAVAILABLE",
        "P4_SEMANTIC_QUOTA_EXCEEDED",
        "P4_SEMANTIC_RATE_LIMITED",
        "P4_SEMANTIC_NETWORK_FAILURE",
        "P4_SEMANTIC_PROVIDER_UNAVAILABLE",
        "P4_SEMANTIC_PROVIDER_FAILURE",
    }:
        return "AutoGlific could not reach its semantic service."
    if code in {
        "P4_SEMANTIC_PROVIDER_RESPONSE_INVALID",
        "P4_SEMANTIC_PROVIDER_RESPONSE_EMPTY",
    }:
        return "AutoGlific received an unreadable semantic response."
    if code == "P4_TRANSLATION_AMBIGUOUS":
        return "AutoGlific could not determine the intended branch."
    if code == "P4_TRANSLATION_TRIGGER_ONLY":
        return "AutoGlific needs an authored flow action for that trigger."
    if code == "P4_TRANSLATION_CHOICE_SOURCE_MISMATCH":
        return "AutoGlific could not validate the choice options."
    if code == "P4_TRANSLATION_SEGMENT_NON_LINEAR_MIDPOINT":
        return "AutoGlific created branches from that choice."
    if code.startswith("P4_TRANSLATION_"):
        return "AutoGlific could not validate that instruction."
    if code.startswith("P4_WORKBENCH_"):
        return "AutoGlific could not complete that action."
    return error.message


class _UnavailableSemanticModelClient:
    """Fail closed when live semantic configuration is unavailable."""

    def __init__(self, reason: str):
        self.reason = reason

    def interpret(self, **_: Any) -> dict[str, Any]:
        raise SemanticTranslationError(self.reason)

    def clarify_semantics(self, **_: Any) -> dict[str, Any]:
        raise SemanticTranslationError(self.reason)


def _build_semantic_client() -> Any:
    try:
        return IncrementalSemanticModelClient.from_environment()
    except SemanticTranslationError as exc:
        # Starting a local session remains possible so the UI can explain the
        # missing setup; proposing a step never falls back to keyword matching.
        return _UnavailableSemanticModelClient(str(exc))


class WorkbenchApp:
    def __init__(
        self,
        *,
        semantic_client: Any | None = None,
        offline: bool = False,
        glific_client_factory: Any | None = None,
        storage: Any | None = None,
    ) -> None:
        if offline:
            client = None
            self.semantic_status = "offline test mode"
        else:
            client = semantic_client or _build_semantic_client()
            self.semantic_status = (
                "missing OPENAI_API_KEY"
                if isinstance(client, _UnavailableSemanticModelClient)
                and client.reason.startswith("P4_SEMANTIC_CONFIGURATION_MISSING")
                else "not ready"
                if isinstance(client, _UnavailableSemanticModelClient)
                else "ready"
            )
        interpreter = RegistryInterpreter(client)
        self.service = AuthoringService(interpreter, workbench_mode=True)
        self.storage = storage or build_storage(
            data_root=DATA_ROOT,
            sessions_root=SESSIONS_ROOT,
            artifacts_root=ARTIFACTS_ROOT,
            confirmations_root=CONFIRMATIONS_ROOT,
            glific_results_root=GLIFIC_RESULTS_ROOT,
        )
        self._locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)
        self._glific_client_factory = glific_client_factory or GlificClient.from_environment
        self._publish_guard = threading.Lock()
        self._glific_inflight: set[str] = set()
        self._glific_progress: dict[str, str] = {}
        self._session_tokens: dict[str, int | str] = {}

    def _lock(self, session_id: str) -> threading.RLock:
        return self._locks[session_id]

    @staticmethod
    def _session_path(session_id: str) -> Path:
        return SESSIONS_ROOT / f"{session_id}.json"

    @staticmethod
    def _confirmation_path(session_id: str) -> Path:
        return CONFIRMATIONS_ROOT / f"{session_id}.json"

    @staticmethod
    def _pipeline_path(session_id: str) -> Path:
        return ARTIFACTS_ROOT / session_id / "latest.json"

    @staticmethod
    def _glific_result_path(session_id: str) -> Path:
        return GLIFIC_RESULTS_ROOT / f"{session_id}.json"

    def _load_session(self, session_id: str) -> AuthoringSession:
        try:
            session, token = self.storage.load_session_with_token(session_id)
            if token is not None:
                self._session_tokens[session_id] = token
            return session
        except StorageNotFound as exc:
            raise ApiError("P4_SESSION_NOT_FOUND", "Session does not exist.", HTTPStatus.NOT_FOUND) from exc
        except StorageError as exc:
            raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
        except Exception as exc:
            raise ApiError("P4_SESSION_INVALID", "Stored session is invalid.", HTTPStatus.INTERNAL_SERVER_ERROR) from exc

    @staticmethod
    def _require_revision(session: AuthoringSession, body: dict[str, Any]) -> None:
        revision = body.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ApiError("P4_REVISION_REQUIRED", "Request must include the current revision.")
        if revision != session.revision:
            raise ApiError(
                "P4_REVISION_CONFLICT",
                f"Stale revision: expected {session.revision}, got {revision}.",
                HTTPStatus.CONFLICT,
            )

    def _save(self, session: AuthoringSession, expected_revision: int | None) -> None:
        try:
            expected_generation = (
                self._session_tokens.get(session.id)
                if expected_revision is not None
                else None
            )
            self.storage.save_session(
                session,
                expected_revision,
                expected_generation=expected_generation,
            )
        except StorageRevisionConflict as exc:
            raise ApiError("P4_REVISION_CONFLICT", "Session changed in another request.", HTTPStatus.CONFLICT) from exc
        except StorageError as exc:
            raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc

    def _replace_session_and_clear_derived(
        self,
        session: AuthoringSession,
        expected_revision: int | None,
        *,
        expected_generation: int | str | None = None,
    ) -> None:
        try:
            self.storage.replace_session_and_clear_derived(
                session,
                expected_revision,
                expected_generation=expected_generation,
            )
        except PublishLeaseBusy as exc:
            raise ApiError(exc.code, exc.message, HTTPStatus.CONFLICT) from exc
        except StorageRevisionConflict as exc:
            raise ApiError(
                "P4_REVISION_CONFLICT",
                "Session changed in another request.",
                HTTPStatus.CONFLICT,
            ) from exc
        except StorageError as exc:
            raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc

    def _replace_pipeline_and_invalidate_result(
        self,
        session: AuthoringSession,
        pipeline: dict[str, Any],
        *,
        expected_old_pipeline_artifact_hash: str | None,
        expected_old_result: dict[str, Any] | None,
    ) -> None:
        try:
            self.storage.replace_pipeline_and_invalidate_result(
                session.id,
                pipeline,
                expected_revision=session.revision,
                expected_frozen_hash=session.frozen_hash,
                expected_old_pipeline_artifact_hash=expected_old_pipeline_artifact_hash,
                expected_old_result=expected_old_result,
                expected_generation=self._session_tokens.get(session.id),
            )
        except PublishLeaseBusy as exc:
            raise ApiError(exc.code, exc.message, HTTPStatus.CONFLICT) from exc
        except StorageRevisionConflict as exc:
            raise ApiError(
                "P4_REVISION_CONFLICT",
                "Session changed in another request.",
                HTTPStatus.CONFLICT,
            ) from exc
        except StorageError as exc:
            raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc

    def _load_pipeline(self, session_id: str, session: AuthoringSession) -> dict[str, Any] | None:
        try:
            pipeline = self.storage.load_document(session_id, "pipeline")
        except StorageError as exc:
            if exc.code == "P4_WORKBENCH_ARTIFACT_INVALID":
                return None
            raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
        if pipeline is None:
            return None
        if (
            pipeline.get("session_revision") != session.revision
            or pipeline.get("frozen_package_hash") != session.frozen_hash
        ):
            return None
        return pipeline

    def _load_glific_result(
        self, session_id: str, session: AuthoringSession, pipeline: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not pipeline:
            return None
        try:
            stored = self.storage.load_document(session_id, "glific_result")
        except StorageError as exc:
            if exc.code == "P4_WORKBENCH_ARTIFACT_INVALID":
                return None
            raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
        if stored is None:
            return None
        artifact_stage = next(
            (item for item in pipeline.get("stages", []) if item.get("name") == "engine3_glific_artifact"),
            None,
        )
        if (
            stored.get("session_revision") != session.revision
            or stored.get("frozen_package_hash") != session.frozen_hash
            or not artifact_stage
            or stored.get("artifact_hash") != artifact_stage.get("canonical_hash")
        ):
            return None
        result = stored.get("result")
        return result if isinstance(result, dict) else None

    def _set_glific_progress(self, session_id: str, phase: str | None) -> None:
        with self._publish_guard:
            if phase is None:
                self._glific_progress.pop(session_id, None)
            else:
                self._glific_progress[session_id] = phase

    def _checkpoint(self, session: AuthoringSession) -> dict[str, Any] | None:
        if session.state is not SessionState.FROZEN:
            return None
        if not session.frozen_package or not session.frozen_hash:
            raise ApiError("P4_FROZEN_PACKAGE_MISSING", "Frozen session has no package.", HTTPStatus.INTERNAL_SERVER_ERROR)
        try:
            package = validate_frozen_package(session.frozen_package)
            package_hash = canonical_authoring_package_hash(package)
            if package_hash != session.frozen_hash:
                raise ValueError("P4_FROZEN_PACKAGE_HASH_MISMATCH")
            mermaid = authored_mermaid_review(session, require_frozen=True)
            presentation_mermaid = authored_presentation_mermaid(session, require_frozen=True)
        except Exception as exc:
            raise ApiError("P4_FROZEN_CHECKPOINT_INVALID", str(exc), HTTPStatus.INTERNAL_SERVER_ERROR) from exc
        return {
            "label": "Frozen semantic package",
            "frozen_package_hash": package_hash,
            "authored_nodes": [
                {
                    "id": node.id,
                    "capability": node.capability,
                    "config": node.config,
                }
                for node in session.nodes
            ],
            "authored_edges": [edge.model_dump(mode="json") for edge in session.edges],
            "open_branch_count": len(session.open_positions),
            "package_validation_status": "passed",
            "freeze_status": "frozen",
            "authored_mermaid": mermaid,
            "presentation_mermaid": presentation_mermaid,
            "semantic_verification_note": (
                "This checkpoint supports human semantic verification and proves "
                "authoring topology/config completeness; Mermaid alone does not "
                "mathematically prove that message meaning is correct."
            ),
        }

    def list_sessions(self) -> dict[str, Any]:
        """Return safe sidebar summaries for locally persisted workbench flows."""

        summaries: list[tuple[int, dict[str, Any]]] = []
        for sort_key, session in self.storage.list_sessions():
            keywords = (
                [item.value for item in session.flow_trigger_metadata.keywords]
                if session.flow_trigger_metadata
                else []
            )
            summaries.append((
                sort_key,
                {
                    "id": session.id,
                    "title": session.title,
                    "state": session.state.value,
                    "revision": session.revision,
                    "segment_count": len(
                        {
                            node.source_statement.strip()
                            for node in session.nodes
                            if node.source_statement.strip()
                        }
                    ),
                    "keywords": keywords,
                    "published": self._load_glific_result(
                        session.id,
                        session,
                        self._load_pipeline(session.id, session),
                    )
                    is not None,
                },
            ))
        summaries.sort(key=lambda item: (-item[0], item[1]["title"].casefold(), item[1]["id"]))
        return {"sessions": [item[1] for item in summaries]}

    @staticmethod
    def settings() -> dict[str, Any]:
        """Expose only non-secret Glific connection details to the local UI."""

        raw_base = (
            os.environ.get("GLIFIC_BASE_URL")
            or os.environ.get("GLIFIC_PRODUCTION_BASE_URL")
            or ""
        ).strip()
        glific_url = None
        if raw_base:
            try:
                glific_url, _ = _normalize_base_url(raw_base)
            except GlificClientError:
                glific_url = None
        mobile_configured = bool(os.environ.get("GLIFIC_PHONE", "").strip())
        password_configured = bool(os.environ.get("GLIFIC_PASSWORD", "").strip())
        try:
            GlificConfig.from_environment()
        except GlificClientError:
            return {
                "configured": False,
                "glific_url": glific_url,
                "mobile_number": "Configured" if mobile_configured else "Not configured",
                "password": "******" if password_configured else "Not configured",
            }
        return {
            "configured": True,
            "glific_url": glific_url,
            "mobile_number": "Configured" if mobile_configured else "Not configured",
            "password": "******",
        }

    def view(self, session: AuthoringSession) -> dict[str, Any]:
        review_text = None
        expanded = None
        if session.state in {SessionState.READY_FOR_REVIEW, SessionState.FROZEN}:
            try:
                review_text = text_review(session)
                expanded = expanded_mermaid_review(session)
            except Exception as exc:  # noqa: BLE001 - preserve review error without mutating state
                review_text = f"Review unavailable: {_error_message(exc)}"
        authored = authored_mermaid_review(session) if session.nodes else "flowchart TD\n  empty[\"No authored nodes yet\"]"
        presentation = (
            authored_presentation_mermaid(session)
            if session.state in {SessionState.READY_FOR_REVIEW, SessionState.FROZEN} and session.nodes
            else ""
        )
        current_question = (
            session.pending_questions[0].model_dump(mode="json")
            if session.pending_questions
            else None
        )
        confirmation = None
        stored = self.storage.load_document(session.id, "confirmation")
        if stored is not None and stored.get("revision") == session.revision:
            confirmation = {
                "revision": stored["revision"],
                "hash": stored["hash"],
            }
        pipeline = self._load_pipeline(session.id, session)
        return {
            "session": session.model_dump(mode="json"),
            "current_question": current_question,
            "segment_remaining_count": len(session.queued_proposals),
            "open_positions": [item.model_dump(mode="json") for item in session.open_positions],
            "live_authored_mermaid": authored,
            "live_presentation_mermaid": presentation,
            "review_text": review_text,
            "expanded_mermaid": expanded,
            "prepared_confirmation": confirmation,
            "checkpoint": self._checkpoint(session),
            "pipeline": pipeline,
            "glific_publish": self._load_glific_result(session.id, session, pipeline),
            "glific_publish_status": self._glific_progress.get(session.id),
        }

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        session_id = _safe_session_id(body.get("session_id"))
        title = _safe_title(body.get("title"))
        reset = bool(body.get("reset"))
        original_brief = body.get("original_brief")
        if original_brief is not None and not isinstance(original_brief, str):
            raise ApiError("P4_ORIGINAL_BRIEF_INVALID", "Original brief must be text.")
        with self._lock(session_id):
            existing = self._load_session(session_id) if self.storage.session_exists(session_id) else None
            if existing and not reset:
                raise ApiError("P4_SESSION_EXISTS", "Use reset to replace the existing session.", HTTPStatus.CONFLICT)
            if existing:
                expected = body.get("revision")
                if expected != existing.revision:
                    raise ApiError("P4_REVISION_CONFLICT", "Reset requires the current revision.", HTTPStatus.CONFLICT)
            session = self.service.start(
                session_id,
                title,
                original_brief=original_brief,
            )
            self._replace_session_and_clear_derived(
                session,
                existing.revision if existing else None,
                expected_generation=(
                    self._session_tokens.get(session_id) if existing else None
                ),
            )
            return self.view(session)

    def propose(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        statement = body.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise ApiError("P4_INSTRUCTION_REQUIRED", "Instruction must be non-empty.")
        with self._lock(session_id):
            session = self._load_session(session_id)
            self._require_revision(session, body)
            before = session.revision
            try:
                updated = self.service.propose(session, statement.strip())
            except Exception as exc:
                error = _api_error_from_exception(exc)
                if error.code == "P4_TRANSLATION_AMBIGUOUS":
                    error.available_branches = _open_branch_labels(session)
                raise error from exc
            self._save(updated, before)
            return self.view(updated)

    @staticmethod
    def _typed_answer(session: AuthoringSession, body: dict[str, Any]) -> QuestionAnswer:
        question_id = body.get("question_id")
        question = next((item for item in session.pending_questions if item.id == question_id), None)
        if question is None:
            raise ApiError("P4_UNKNOWN_QUESTION", "Question is not pending.")
        decision_source = body.get("decision_source", "confirmed_user_decision")
        if decision_source != "confirmed_user_decision":
            raise ApiError("P4_EVALUATION_DECISION_NOT_ALLOWED", "AutoGlific answers must be confirmed user decisions.")
        value = body.get("value")
        if question.answer_type == "boolean" and not isinstance(value, bool):
            raise ApiError("P4_BOOLEAN_ANSWER_INVALID", "Answer must be true or false.")
        if question.answer_type == "options" and (
            not isinstance(value, str) or (question.options and value not in question.options)
        ):
            raise ApiError("P4_OPTION_ANSWER_INVALID", "Answer must be one of the offered options.")
        if question.field_path == "options" and isinstance(value, str):
            parsed_options = []
            for item in value.split(","):
                label, separator, stable_value = item.partition("=")
                if not separator or not label.strip() or not stable_value.strip():
                    raise ApiError(
                        "P4_CHOICE_OPTIONS_ANSWER_INVALID",
                        "Use comma-separated label=value pairs.",
                    )
                parsed_options.append(
                    {"label": label.strip(), "value": stable_value.strip()}
                )
            value = parsed_options
        if question.answer_type == "json" and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ApiError("P4_INVALID_JSON_ANSWER", "Answer must be valid JSON.") from exc
        return QuestionAnswer(
            question_id=question.id,
            value=value,
            rationale=body.get("rationale"),
            answered_at=body.get("answered_at"),
        )

    def answer(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        with self._lock(session_id):
            session = self._load_session(session_id)
            self._require_revision(session, body)
            before = session.revision
            answer = self._typed_answer(session, body)
            try:
                updated = self.service.answer(session, answer)
            except Exception as exc:
                raise _api_error_from_exception(exc) from exc
            self._save(updated, before)
            return self.view(updated)

    def prepare(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        with self._lock(session_id):
            session = self._load_session(session_id)
            self._require_revision(session, body)
            try:
                package, digest = prepare_confirmation(session, confirmed_by="workbench-user")
            except Exception as exc:
                raise _api_error_from_exception(exc) from exc
            try:
                self.storage.save_document(
                    session_id,
                    "confirmation",
                    {"revision": session.revision, "hash": digest, "package": package},
                    expected_revision=session.revision,
                    expected_frozen_hash=session.frozen_hash,
                    expected_generation=self._session_tokens.get(session_id),
                )
            except StorageRevisionConflict as exc:
                raise ApiError(
                    "P4_REVISION_CONFLICT",
                    "Session changed in another request.",
                    HTTPStatus.CONFLICT,
                ) from exc
            except StorageError as exc:
                raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
            response = self.view(session)
            response["prepared_package"] = package
            response["prepared_hash"] = digest
            return response

    def freeze(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        confirmed_hash = body.get("confirmed_hash")
        if not isinstance(confirmed_hash, str):
            raise ApiError("P4_CONFIRMATION_HASH_REQUIRED", "Confirmed hash is required.")
        with self._lock(session_id):
            session = self._load_session(session_id)
            self._require_revision(session, body)
            prepared = self.storage.load_document(session_id, "confirmation")
            if prepared is None:
                raise ApiError("P4_CONFIRMATION_STALE", "Prepared confirmation is stale.", HTTPStatus.CONFLICT)
            if prepared.get("revision") != session.revision:
                raise ApiError("P4_CONFIRMATION_STALE", "Prepared confirmation is stale.", HTTPStatus.CONFLICT)
            if confirmed_hash != prepared.get("hash"):
                raise ApiError("P4_CONFIRMATION_HASH_MISMATCH", "Confirmed hash does not match the prepared package.")
            try:
                package = validate_frozen_package(prepared["package"])
                if canonical_authoring_package_hash(package) != confirmed_hash:
                    raise ValueError("P4_CONFIRMATION_PACKAGE_HASH_MISMATCH")
                updated = freeze(session, confirmed_hash, prepared["package"])
            except Exception as exc:
                raise _api_error_from_exception(exc) from exc
            self._save(updated, session.revision)
            try:
                self.storage.delete_document(
                    session_id,
                    "confirmation",
                    expected_document_revision=session.revision,
                    expected_document_frozen_hash=confirmed_hash,
                    expected_document_payload=prepared,
                )
            except StorageRevisionConflict as exc:
                raise ApiError(
                    "P4_REVISION_CONFLICT",
                    "Session changed in another request.",
                    HTTPStatus.CONFLICT,
                ) from exc
            except StorageError as exc:
                raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
            return self.view(updated)

    def compile(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        with self._lock(session_id):
            session = self._load_session(session_id)
            self._require_revision(session, body)
            if session.state is not SessionState.FROZEN:
                raise ApiError("P4_COMPILE_REQUIRES_FROZEN_SESSION", "Compile is available only after freeze.", HTTPStatus.CONFLICT)
            prior_pipeline = self._load_pipeline(session_id, session)
            prior_artifact_hash = None
            if prior_pipeline:
                prior_stage = next(
                    (
                        item
                        for item in prior_pipeline.get("stages", [])
                        if isinstance(item, dict)
                        and item.get("name") == "engine3_glific_artifact"
                    ),
                    None,
                )
                if isinstance(prior_stage, dict):
                    prior_artifact_hash = prior_stage.get("canonical_hash")
            prior_result = None
            if prior_artifact_hash:
                try:
                    candidate = self.storage.load_document(session_id, "glific_result")
                except StorageError as exc:
                    if exc.code != "P4_WORKBENCH_ARTIFACT_INVALID":
                        raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
                    candidate = None
                if (
                    isinstance(candidate, dict)
                    and candidate.get("session_revision") == session.revision
                    and candidate.get("frozen_package_hash") == session.frozen_hash
                    and candidate.get("artifact_hash") == prior_artifact_hash
                ):
                    prior_result = candidate
            try:
                result = run_pipeline(session)
            except Exception as exc:
                raise _api_error_from_exception(exc) from exc
            self._replace_pipeline_and_invalidate_result(
                session,
                result,
                expected_old_pipeline_artifact_hash=prior_artifact_hash,
                expected_old_result=prior_result,
            )
            return self.view(session)

    def publish(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Explicitly push the passed pipeline artifact to Glific once."""

        session_id = _safe_session_id(session_id)
        lease_owner = uuid.uuid4().hex
        lease_acquired = False
        with self._publish_guard:
            if session_id in self._glific_inflight:
                raise ApiError(
                    "P4_GLIFIC_PUBLISH_IN_PROGRESS",
                    "A Glific publish is already in progress for this flow. Wait for it to finish.",
                    HTTPStatus.CONFLICT,
                )
            self._glific_inflight.add(session_id)
        try:
            with self._lock(session_id):
                session = self._load_session(session_id)
                self._require_revision(session, body)
                if session.state is not SessionState.FROZEN:
                    raise ApiError(
                        "P4_GLIFIC_PUBLISH_REQUIRES_FROZEN_SESSION",
                        "Lock the flow before pushing it to Glific.",
                        HTTPStatus.CONFLICT,
                    )
                pipeline = self._load_pipeline(session_id, session)
                if not pipeline or pipeline.get("all_stages_passed") is not True:
                    raise ApiError(
                        "P4_GLIFIC_PIPELINE_NOT_READY",
                        "Generate Glific JSON successfully before pushing this flow.",
                        HTTPStatus.CONFLICT,
                    )
                artifact_stage = next(
                    (item for item in pipeline.get("stages", []) if item.get("name") == "engine3_glific_artifact"),
                    None,
                )
                artifact = artifact_stage.get("json") if isinstance(artifact_stage, dict) else None
                artifact_hash = artifact_stage.get("canonical_hash") if isinstance(artifact_stage, dict) else None
                if not isinstance(artifact, dict) or not artifact_hash:
                    raise ApiError(
                        "P4_GLIFIC_ARTIFACT_NOT_AVAILABLE",
                        "A passed Glific artifact is not available. Generate the file again.",
                        HTTPStatus.CONFLICT,
                    )
                expected_revision = session.revision
                expected_frozen_hash = session.frozen_hash
                expected_generation = self._session_tokens.get(session_id)
            try:
                self.storage.acquire_publish_lease(
                    session_id,
                    artifact_hash,
                    lease_owner,
                    PUBLISH_LEASE_SECONDS,
                    expected_revision=expected_revision,
                    expected_frozen_hash=expected_frozen_hash,
                    expected_generation=expected_generation,
                )
                lease_acquired = True
            except PublishLeaseBusy as exc:
                raise ApiError(exc.code, exc.message, HTTPStatus.CONFLICT) from exc
            except StorageRevisionConflict as exc:
                raise ApiError(
                    "P4_GLIFIC_LOCAL_STATE_CHANGED",
                    "The local flow changed before publication started. Review the flow and try again.",
                    HTTPStatus.CONFLICT,
                ) from exc
            except StorageError as exc:
                raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
            self._set_glific_progress(session_id, "connecting")
            try:
                client = self._glific_client_factory()
                result = _safe_glific_result(
                    client.publish_artifact(
                        artifact,
                        progress=lambda phase: self._set_glific_progress(session_id, phase),
                    )
                )
            except Exception as exc:
                raise _api_error_from_exception(exc, status=HTTPStatus.BAD_GATEWAY) from exc
            with self._lock(session_id):
                current = self._load_session(session_id)
                if (
                    current.revision != expected_revision
                    or current.frozen_hash != expected_frozen_hash
                    or current.state is not SessionState.FROZEN
                ):
                    raise ApiError(
                        "P4_GLIFIC_LOCAL_STATE_CHANGED",
                        "Glific confirmed the publish, but the local flow changed before the result was saved.",
                        HTTPStatus.CONFLICT,
                    )
                stored_result = {
                    "schema_version": "product4-workbench-glific-publish-1.0",
                    "session_id": session_id,
                    "session_revision": current.revision,
                    "frozen_package_hash": current.frozen_hash,
                    "artifact_hash": artifact_hash,
                    "result": result,
                }
                try:
                    self.storage.record_publish_result(
                        session_id,
                        stored_result,
                        expected_revision=expected_revision,
                        expected_frozen_hash=expected_frozen_hash,
                        artifact_hash=artifact_hash,
                        owner=lease_owner,
                        expected_generation=expected_generation,
                    )
                except StorageRevisionConflict as exc:
                    raise ApiError(
                        "P4_GLIFIC_LOCAL_STATE_CHANGED",
                        "Glific confirmed the publish, but the local flow changed before the result was saved.",
                        HTTPStatus.CONFLICT,
                    ) from exc
                except StorageError as exc:
                    raise ApiError(exc.code, exc.message, HTTPStatus.INTERNAL_SERVER_ERROR) from exc
                self._set_glific_progress(session_id, None)
                return self.view(current)
        finally:
            self._set_glific_progress(session_id, None)
            if lease_acquired:
                try:
                    self.storage.release_publish_lease(session_id, lease_owner)
                except StorageError:
                    pass
            with self._publish_guard:
                self._glific_inflight.discard(session_id)

    def download(self, session_id: str, kind: str) -> tuple[bytes, str, str]:
        session_id = _safe_session_id(session_id)
        with self._lock(session_id):
            session = self._load_session(session_id)
            pipeline = self._load_pipeline(session_id, session)
            if kind == "frozen-package":
                if not session.frozen_package:
                    raise ApiError("P4_FROZEN_PACKAGE_MISSING", "No frozen package is available.", HTTPStatus.CONFLICT)
                payload, filename = session.frozen_package, f"{session_id}-frozen-package.json"
                content_type = "application/json"
            elif kind in {"engine1-graph", "engine2-flow-spec", "glific"}:
                if not pipeline:
                    raise ApiError("P4_PIPELINE_NOT_RUN", "Run the pipeline before downloading this artifact.", HTTPStatus.CONFLICT)
                stage_name = {
                    "engine1-graph": "engine1_graph",
                    "engine2-flow-spec": "engine2_flow_spec",
                    "glific": "engine3_glific_artifact",
                }[kind]
                stage = next((item for item in pipeline["stages"] if item["name"] == stage_name), None)
                if not stage or stage.get("status") != "passed" or stage.get("json") is None:
                    raise ApiError("P4_ARTIFACT_NOT_AVAILABLE", "This stage has no passed artifact.", HTTPStatus.CONFLICT)
                payload, filename = stage["json"], f"{session_id}-{kind}.json"
                content_type = "application/json"
            elif kind == "authored-mermaid":
                payload = authored_mermaid_review(session, require_frozen=session.state is SessionState.FROZEN)
                filename, content_type = f"{session_id}-authored-semantic.mmd", "text/vnd.mermaid"
            elif kind == "presentation-mermaid":
                payload = authored_presentation_mermaid(
                    session,
                    require_frozen=session.state is SessionState.FROZEN,
                )
                filename, content_type = f"{session_id}-presentation.mmd", "text/vnd.mermaid"
            elif kind == "expanded-mermaid":
                payload = expanded_mermaid_review(session)
                filename, content_type = f"{session_id}-expanded-policy.mmd", "text/vnd.mermaid"
            else:
                raise ApiError("P4_DOWNLOAD_KIND_INVALID", "Unknown download artifact.")
            if content_type == "application/json":
                body = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            else:
                body = (str(payload) + "\n").encode("utf-8")
            return body, content_type, filename


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    server_version = "Product4Workbench/1.0"

    @property
    def app(self) -> WorkbenchApp:
        return self.server.workbench_app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the local launch terminal quiet except for explicit failures.
        return

    def _send_json(
        self,
        payload: Any,
        status: int = HTTPStatus.OK,
        *,
        request_id: str | None = None,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if request_id:
            self.send_header("X-AutoGlific-Request-ID", request_id)
        self.end_headers()
        self.wfile.write(raw)

    def _send_error(self, error: ApiError) -> None:
        _LOGGER.warning(
            "AutoGlific request failed code=%s request_id=%s validation=%s network=%s",
            error.code,
            error.request_id,
            error.validation_fingerprint or "-",
            error.network_subtype or "-",
        )
        detail = {
            "code": error.code,
            "message": _safe_error_message(error),
            "request_id": error.request_id,
        }
        if error.available_branches:
            detail["available_branches"] = list(error.available_branches)
        self._send_json(
            {"error": detail},
            error.status,
            request_id=error.request_id,
        )

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError("P4_REQUEST_BODY_INVALID", "Content-Length is invalid.") from exc
        if length > MAX_JSON_BYTES:
            raise ApiError("P4_REQUEST_TOO_LARGE", "Request body is too large.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("P4_INVALID_JSON_REQUEST", "Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ApiError("P4_REQUEST_OBJECT_REQUIRED", "Request body must be a JSON object.")
        return payload

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if path == "/api/health":
                self._send_json({"status": "ok", "data_root": str(DATA_ROOT)})
                return
            if path == "/api/sessions":
                self._send_json(self.app.list_sessions())
                return
            if path == "/api/settings":
                self._send_json(self.app.settings())
                return
            if path == "/" or path == "/index.html":
                self._send_static("index.html", "text/html; charset=utf-8")
                return
            if path.startswith("/static/"):
                name = path.removeprefix("/static/")
                if name not in {
                    "app.js",
                    "styles.css",
                    "vendor/mermaid-11.16.0.min.js",
                }:
                    raise ApiError("P4_STATIC_NOT_FOUND", "Static resource not found.", HTTPStatus.NOT_FOUND)
                content_type = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
                self._send_static(name, content_type)
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "sessions":
                session_id = _safe_session_id(parts[2])
                with self.app._lock(session_id):
                    self._send_json(self.app.view(self.app._load_session(session_id)))
                return
            if len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[3] == "download":
                body, content_type, filename = self.app.download(parts[2], parts[4])
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            raise ApiError("P4_ROUTE_NOT_FOUND", "Route not found.", HTTPStatus.NOT_FOUND)
        except ApiError as exc:
            self._send_error(exc)
        except Exception as exc:  # noqa: BLE001 - last-resort HTTP boundary
            self._send_error(
                _api_error_from_exception(exc, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            )

    def _send_static(self, name: str, content_type: str) -> None:
        path = PROJECT_ROOT / "workbench" / "static" / name
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise ApiError("P4_STATIC_NOT_FOUND", "Static resource not found.", HTTPStatus.NOT_FOUND) from exc
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            body = self._body()
            parts = [part for part in path.split("/") if part]
            if path == "/api/sessions":
                self._send_json(self.app.start(body), HTTPStatus.CREATED)
                return
            if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                session_id, action = parts[2], parts[3]
                actions = {
                    "propose": self.app.propose,
                    "answer": self.app.answer,
                    "prepare-confirmation": self.app.prepare,
                    "freeze": self.app.freeze,
                    "compile": self.app.compile,
                    "publish": self.app.publish,
                }
                if action not in actions:
                    raise ApiError("P4_ROUTE_NOT_FOUND", "Route not found.", HTTPStatus.NOT_FOUND)
                self._send_json(actions[action](session_id, body))
                return
            raise ApiError("P4_ROUTE_NOT_FOUND", "Route not found.", HTTPStatus.NOT_FOUND)
        except ApiError as exc:
            self._send_error(exc)
        except Exception as exc:  # noqa: BLE001 - last-resort HTTP boundary
            self._send_error(
                _api_error_from_exception(exc, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            )


def build_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    semantic_client: Any | None = None,
    offline: bool = False,
    glific_client_factory: Any | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), WorkbenchRequestHandler)
    server.workbench_app = WorkbenchApp(  # type: ignore[attr-defined]
        semantic_client=semantic_client,
        offline=offline,
        glific_client_factory=glific_client_factory,
    )
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Product 4 local pipeline workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = build_server(args.host, args.port)
    print(f"Product 4 workbench: http://{args.host}:{server.server_port}")
    print(f"Workbench data: {DATA_ROOT}")
    print(f"Semantic authoring: {server.workbench_app.semantic_status}")  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
