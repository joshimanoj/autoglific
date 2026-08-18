"""Small persistence seam for local and hosted Product 4 workbenches.

The filesystem backend is the local default.  The hosted backend uses the
existing JSON payloads as canonical values in PostgreSQL JSONB columns and
adds only the revision, artifact-binding, and publish-lease metadata needed
for safe multi-instance execution.
"""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from product4.authoring.session import SessionStore
from product4.contracts.session import AuthoringSession


class StorageError(RuntimeError):
    """Safe storage failure with a stable Product 4 error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class StorageNotFound(StorageError):
    def __init__(self, message: str = "Workbench resource not found."):
        super().__init__("P4_NOT_FOUND", message)


class StorageRevisionConflict(StorageError):
    def __init__(self, message: str = "Session changed in another request."):
        super().__init__("P4_REVISION_CONFLICT", message)


class PublishLeaseBusy(StorageError):
    def __init__(self):
        super().__init__(
            "P4_GLIFIC_PUBLISH_IN_PROGRESS",
            "A Glific publish is already in progress for this flow. Wait for it to finish.",
        )


class StorageBackend:
    """Minimal interface consumed by ``WorkbenchApp``."""

    def load_session(self, session_id: str, owner_id: str | None = None) -> AuthoringSession:
        raise NotImplementedError

    def load_session_with_token(
        self, session_id: str, owner_id: str | None = None
    ) -> tuple[AuthoringSession, int | str | None]:
        """Load a session and an internal CAS token.

        The token is storage metadata only; it is never part of the session
        contract or an API response.  Backends without a separate token retain
        the revision-only compatibility behavior.
        """

        return self.load_session(session_id, owner_id), None

    def session_exists(self, session_id: str, owner_id: str | None = None) -> bool:
        try:
            self.load_session(session_id, owner_id)
        except StorageNotFound:
            return False
        return True

    def save_session(
        self,
        session: AuthoringSession,
        expected_revision: int | None,
        *,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    def load_document(
        self, session_id: str, kind: str, owner_id: str | None = None
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def save_document(
        self,
        session_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        expected_revision: int | None = None,
        expected_frozen_hash: str | None = None,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    def delete_document(
        self,
        session_id: str,
        kind: str,
        *,
        expected_revision: int | None = None,
        expected_frozen_hash: str | None = None,
        expected_generation: int | str | None = None,
        expected_document_revision: int | None = None,
        expected_document_frozen_hash: str | None = None,
        expected_artifact_hash: str | None = None,
        expected_document_payload: Mapping[str, Any] | None = None,
        owner_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    def clear_derived(
        self,
        session_id: str,
        *,
        expected_revision: int | None = None,
        expected_frozen_hash: str | None = None,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        if expected_revision is None:
            raise StorageRevisionConflict(
                "Derived cleanup requires the current session binding."
            )
        for kind in ("confirmation", "pipeline", "glific_result"):
            self.delete_document(
                session_id,
                kind,
                expected_revision=expected_revision,
                expected_frozen_hash=expected_frozen_hash,
                expected_generation=expected_generation,
                owner_id=owner_id,
            )

    def replace_session_and_clear_derived(
        self,
        session: AuthoringSession,
        expected_revision: int | None,
        *,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        """Replace a session and clear its derived documents atomically."""

        raise NotImplementedError

    def replace_pipeline_and_invalidate_result(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        expected_old_pipeline_artifact_hash: str | None,
        expected_old_result: Mapping[str, Any] | None,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        """Save a pipeline and conditionally invalidate only its old result."""

        raise NotImplementedError

    def list_sessions(self, owner_id: str | None = None) -> list[tuple[float, AuthoringSession]]:
        raise NotImplementedError

    def acquire_publish_lease(
        self,
        session_id: str,
        artifact_hash: str,
        owner: str,
        ttl_seconds: int,
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    def release_publish_lease(
        self, session_id: str, owner: str, owner_id: str | None = None
    ) -> None:
        raise NotImplementedError

    def record_publish_result(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        artifact_hash: str,
        owner: str,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        raise NotImplementedError


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StorageNotFound() from exc
    except (OSError, ValueError) as exc:
        raise StorageError(
            "P4_WORKBENCH_ARTIFACT_INVALID",
            "Stored workbench JSON is invalid.",
        ) from exc
    if not isinstance(value, dict):
        raise StorageError(
            "P4_WORKBENCH_ARTIFACT_INVALID",
            "Stored workbench JSON is invalid.",
        )
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".p4-storage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _pipeline_matches(
    pipeline: Mapping[str, Any] | None,
    *,
    expected_revision: int,
    expected_frozen_hash: str,
    artifact_hash: str,
) -> bool:
    if not pipeline:
        return False
    if (
        pipeline.get("session_revision") != expected_revision
        or pipeline.get("frozen_package_hash") != expected_frozen_hash
        or pipeline.get("all_stages_passed") is not True
    ):
        return False
    stages = pipeline.get("stages")
    if not isinstance(stages, list):
        return False
    artifact = next(
        (
            item
            for item in stages
            if isinstance(item, dict)
            and item.get("name") == "engine3_glific_artifact"
        ),
        None,
    )
    return isinstance(artifact, dict) and artifact.get("canonical_hash") == artifact_hash


def _document_matches_session_binding(
    kind: str,
    payload: Mapping[str, Any],
    *,
    expected_revision: int,
    expected_frozen_hash: str | None,
) -> bool:
    if kind == "confirmation":
        return payload.get("revision") == expected_revision
    if kind in {"pipeline", "glific_result"}:
        return (
            payload.get("session_revision") == expected_revision
            and payload.get("frozen_package_hash") == expected_frozen_hash
        )
    return False


_filesystem_lease_lock = threading.Lock()
_filesystem_leases: dict[tuple[str, str, str], tuple[str, float, str]] = {}
_filesystem_state_lock = threading.RLock()


def _owner_scope(owner_id: str | None) -> str:
    """Return a stable filesystem scope without putting email text in paths."""

    if owner_id is None:
        return "legacy"
    owner = str(owner_id).strip()
    if not owner:
        raise StorageError("P4_AUTH_REQUIRED", "Authenticated account is required.")
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


class FilesystemStorage(StorageBackend):
    """Compatibility backend for the existing local JSON layout."""

    def __init__(
        self,
        data_root: Path,
        sessions_root: Path,
        artifacts_root: Path,
        confirmations_root: Path,
        glific_results_root: Path,
    ):
        self.data_root = data_root
        self.sessions_root = sessions_root
        self.artifacts_root = artifacts_root
        self.confirmations_root = confirmations_root
        self.glific_results_root = glific_results_root

    def _root(self, root: Path, owner_id: str | None) -> Path:
        if owner_id is None:
            return root
        users_root = self.data_root / "users"
        scoped = users_root / _owner_scope(owner_id) / root.name
        scoped.mkdir(parents=True, exist_ok=True)
        for directory in (users_root, scoped.parent, scoped):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        return scoped

    def session_path(self, session_id: str, owner_id: str | None = None) -> Path:
        return self._root(self.sessions_root, owner_id) / f"{session_id}.json"

    def document_path(
        self, session_id: str, kind: str, owner_id: str | None = None
    ) -> Path:
        confirmations_root = self._root(self.confirmations_root, owner_id)
        artifacts_root = self._root(self.artifacts_root, owner_id)
        glific_results_root = self._root(self.glific_results_root, owner_id)
        if kind == "confirmation":
            return confirmations_root / f"{session_id}.json"
        if kind == "pipeline":
            return artifacts_root / session_id / "latest.json"
        if kind == "glific_result":
            return glific_results_root / f"{session_id}.json"
        raise StorageError(
            "P4_STORAGE_KIND_INVALID", "Unknown workbench storage document."
        )

    def load_session(self, session_id: str, owner_id: str | None = None) -> AuthoringSession:
        path = self.session_path(session_id, owner_id)
        if not path.exists():
            raise StorageNotFound("Session does not exist.")
        try:
            return SessionStore(path).load()
        except Exception as exc:
            raise StorageError(
                "P4_SESSION_INVALID", "Stored session is invalid."
            ) from exc

    def load_session_with_token(
        self, session_id: str, owner_id: str | None = None
    ) -> tuple[AuthoringSession, int | str | None]:
        path = self.session_path(session_id, owner_id)
        session = self.load_session(session_id, owner_id)
        try:
            stat = path.stat()
        except OSError as exc:
            raise StorageError(
                "P4_SESSION_INVALID", "Stored session is invalid."
            ) from exc
        return session, f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}"

    def save_session(
        self,
        session: AuthoringSession,
        expected_revision: int | None,
        *,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        with _filesystem_state_lock:
            if expected_generation is not None:
                current, current_generation = self.load_session_with_token(session.id, owner_id)
                if (
                    current.revision != expected_revision
                    or current_generation != expected_generation
                ):
                    raise StorageRevisionConflict()
            try:
                SessionStore(self.session_path(session.id, owner_id)).save(
                    session,
                    expected_revision=expected_revision,
                )
            except ValueError as exc:
                if str(exc) == "P4_REVISION_CONFLICT":
                    raise StorageRevisionConflict() from exc
                raise

    def load_document(
        self, session_id: str, kind: str, owner_id: str | None = None
    ) -> dict[str, Any] | None:
        path = self.document_path(session_id, kind, owner_id)
        if not path.exists():
            return None
        return _read_json(path)

    def save_document(
        self,
        session_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        expected_revision: int | None = None,
        expected_frozen_hash: str | None = None,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        with _filesystem_state_lock:
            if expected_revision is not None:
                current, current_generation = self.load_session_with_token(session_id, owner_id)
                if (
                    current.revision != expected_revision
                    or current.frozen_hash != expected_frozen_hash
                    or (
                        expected_generation is not None
                        and current_generation != expected_generation
                    )
                    or not _document_matches_session_binding(
                        kind,
                        payload,
                        expected_revision=expected_revision,
                        expected_frozen_hash=expected_frozen_hash,
                    )
                ):
                    raise StorageRevisionConflict(
                        "The document is no longer bound to the current flow revision."
                    )
            _atomic_json(self.document_path(session_id, kind, owner_id), payload)

    def delete_document(
        self,
        session_id: str,
        kind: str,
        *,
        expected_revision: int | None = None,
        expected_frozen_hash: str | None = None,
        expected_generation: int | str | None = None,
        expected_document_revision: int | None = None,
        expected_document_frozen_hash: str | None = None,
        expected_artifact_hash: str | None = None,
        expected_document_payload: Mapping[str, Any] | None = None,
        owner_id: str | None = None,
    ) -> None:
        with _filesystem_state_lock:
            if (
                expected_revision is None
                and expected_document_revision is None
                and expected_document_frozen_hash is None
                and expected_artifact_hash is None
                and expected_document_payload is None
            ):
                raise StorageRevisionConflict(
                    "Document cleanup requires an expected binding."
                )
            if expected_revision is not None:
                current, current_generation = self.load_session_with_token(session_id, owner_id)
                if (
                    current.revision != expected_revision
                    or current.frozen_hash != expected_frozen_hash
                    or (
                        expected_generation is not None
                        and current_generation != expected_generation
                    )
                ):
                    raise StorageRevisionConflict(
                        "The document is no longer bound to the current flow revision."
                    )
            if kind in {"pipeline", "glific_result"}:
                key = (str(self.data_root.resolve()), _owner_scope(owner_id), session_id)
                with _filesystem_lease_lock:
                    lease = _filesystem_leases.get(key)
                    if lease is not None and lease[1] > time.monotonic():
                        raise PublishLeaseBusy()
            path = self.document_path(session_id, kind, owner_id)
            if not path.exists():
                return
            if (
                expected_document_revision is not None
                or expected_document_frozen_hash is not None
                or expected_artifact_hash is not None
                or expected_document_payload is not None
            ):
                stored = _read_json(path)
                if expected_document_revision is not None:
                    if kind == "confirmation":
                        if stored.get("revision") != expected_document_revision:
                            return
                    elif stored.get("session_revision") != expected_document_revision:
                        return
                if (
                    expected_document_frozen_hash is not None
                    and (
                        stored.get("hash")
                        if kind == "confirmation"
                        else stored.get("frozen_package_hash")
                    )
                    != expected_document_frozen_hash
                ):
                    return
                if expected_artifact_hash is not None and stored.get("artifact_hash") != expected_artifact_hash:
                    return
                if expected_document_payload is not None and stored != dict(expected_document_payload):
                    return
            path.unlink(missing_ok=True)

    def replace_session_and_clear_derived(
        self,
        session: AuthoringSession,
        expected_revision: int | None,
        *,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        with _filesystem_state_lock:
            key = (str(self.data_root.resolve()), _owner_scope(owner_id), session.id)
            with _filesystem_lease_lock:
                lease = _filesystem_leases.get(key)
                if lease is not None and lease[1] > time.monotonic():
                    raise PublishLeaseBusy()
            self.save_session(
                session,
                expected_revision,
                expected_generation=expected_generation,
                owner_id=owner_id,
            )
            for kind in ("confirmation", "pipeline", "glific_result"):
                self.document_path(session.id, kind, owner_id).unlink(missing_ok=True)

    def replace_pipeline_and_invalidate_result(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        expected_old_pipeline_artifact_hash: str | None,
        expected_old_result: Mapping[str, Any] | None,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        with _filesystem_state_lock:
            key = (str(self.data_root.resolve()), _owner_scope(owner_id), session_id)
            with _filesystem_lease_lock:
                lease = _filesystem_leases.get(key)
                if lease is not None and lease[1] > time.monotonic():
                    raise PublishLeaseBusy()
            old_pipeline = self.load_document(session_id, "pipeline", owner_id)
            old_artifact = None
            if old_pipeline:
                stages = old_pipeline.get("stages")
                if isinstance(stages, list):
                    old_stage = next(
                        (
                            item
                            for item in stages
                            if isinstance(item, dict)
                            and item.get("name") == "engine3_glific_artifact"
                        ),
                        None,
                    )
                    if isinstance(old_stage, dict):
                        old_artifact = old_stage.get("canonical_hash")
            if old_pipeline is None:
                if expected_old_pipeline_artifact_hash is not None:
                    raise StorageRevisionConflict(
                        "The compiled artifact changed before pipeline replacement."
                    )
            elif old_artifact != expected_old_pipeline_artifact_hash:
                raise StorageRevisionConflict(
                    "The compiled artifact changed before pipeline replacement."
                )
            self.save_document(
                session_id,
                "pipeline",
                payload,
                expected_revision=expected_revision,
                expected_frozen_hash=expected_frozen_hash,
                expected_generation=expected_generation,
                owner_id=owner_id,
            )
            if (
                old_artifact
                and expected_old_result is not None
            ):
                result_revision, result_frozen_hash, result_artifact_hash = self._document_binding(
                    "glific_result", expected_old_result
                )
                if result_artifact_hash != old_artifact:
                    raise StorageRevisionConflict(
                        "The publish result is not bound to the previous pipeline."
                    )
                self.delete_document(
                    session_id,
                    "glific_result",
                    expected_document_revision=result_revision,
                    expected_document_frozen_hash=result_frozen_hash,
                    expected_artifact_hash=old_artifact,
                    expected_document_payload=expected_old_result,
                    owner_id=owner_id,
                )

    def list_sessions(self, owner_id: str | None = None) -> list[tuple[float, AuthoringSession]]:
        result: list[tuple[float, AuthoringSession]] = []
        for path in self._root(self.sessions_root, owner_id).glob("*.json"):
            try:
                result.append((path.stat().st_mtime_ns, SessionStore(path).load()))
            except Exception:  # noqa: BLE001, S112 - one damaged draft must not hide the rest
                continue
        return result

    def load_view_documents(
        self, session_id: str, owner_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for kind in ("confirmation", "pipeline", "glific_result"):
            payload = self.load_document(session_id, kind, owner_id)
            if payload is not None:
                result[kind] = payload
        return result

    def acquire_publish_lease(
        self,
        session_id: str,
        artifact_hash: str,
        owner: str,
        ttl_seconds: int,
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        key = (str(self.data_root.resolve()), _owner_scope(owner_id), session_id)
        now = time.monotonic()
        with _filesystem_state_lock, _filesystem_lease_lock:
            current, current_generation = self.load_session_with_token(session_id, owner_id)
            if (
                current.revision != expected_revision
                or current.frozen_hash != expected_frozen_hash
                or (
                    expected_generation is not None
                    and current_generation != expected_generation
                )
            ):
                raise StorageRevisionConflict(
                    "The flow changed before publication started."
                )
            if not _pipeline_matches(
                self.load_document(session_id, "pipeline", owner_id),
                expected_revision=expected_revision,
                expected_frozen_hash=expected_frozen_hash,
                artifact_hash=artifact_hash,
            ):
                raise StorageRevisionConflict(
                    "The compiled artifact is no longer bound to this flow revision."
                )
            existing = _filesystem_leases.get(key)
            if existing is not None and existing[1] > now:
                raise PublishLeaseBusy()
            _filesystem_leases[key] = (owner, now + ttl_seconds, artifact_hash)

    def release_publish_lease(
        self, session_id: str, owner: str, owner_id: str | None = None
    ) -> None:
        key = (str(self.data_root.resolve()), _owner_scope(owner_id), session_id)
        with _filesystem_lease_lock:
            existing = _filesystem_leases.get(key)
            if existing is not None and existing[0] == owner:
                _filesystem_leases.pop(key, None)

    def record_publish_result(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        artifact_hash: str,
        owner: str,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        with _filesystem_state_lock:
            self._record_publish_result_locked(
                session_id,
                payload,
                expected_revision=expected_revision,
                expected_frozen_hash=expected_frozen_hash,
                artifact_hash=artifact_hash,
                owner=owner,
                expected_generation=expected_generation,
                owner_id=owner_id,
            )

    def _record_publish_result_locked(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        artifact_hash: str,
        owner: str,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        session, session_generation = self.load_session_with_token(session_id, owner_id)
        if (
            session.revision != expected_revision
            or session.frozen_hash != expected_frozen_hash
            or (
                expected_generation is not None
                and session_generation != expected_generation
            )
        ):
            raise StorageRevisionConflict(
                "Local flow changed before the publish result was saved."
            )
        pipeline = self.load_document(session_id, "pipeline", owner_id)
        if not _pipeline_matches(
            pipeline,
            expected_revision=expected_revision,
            expected_frozen_hash=expected_frozen_hash,
            artifact_hash=artifact_hash,
        ):
            raise StorageRevisionConflict(
                "The compiled artifact is no longer bound to this flow revision."
            )
        key = (str(self.data_root.resolve()), _owner_scope(owner_id), session_id)
        with _filesystem_lease_lock:
            lease = _filesystem_leases.get(key)
            if (
                lease is None
                or lease[0] != owner
                or lease[1] <= time.monotonic()
                or lease[2] != artifact_hash
            ):
                raise StorageRevisionConflict(
                    "The publish lease is no longer owned by this worker."
                )
        self.save_document(
            session_id,
            "glific_result",
            payload,
            expected_revision=expected_revision,
            expected_frozen_hash=expected_frozen_hash,
            expected_generation=expected_generation,
            owner_id=owner_id,
        )
        self.release_publish_lease(session_id, owner, owner_id)


def _decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise StorageError(
                "P4_WORKBENCH_ARTIFACT_INVALID", "Stored workbench JSON is invalid."
            ) from exc
        if isinstance(decoded, dict):
            return decoded
    raise StorageError(
        "P4_WORKBENCH_ARTIFACT_INVALID", "Stored workbench JSON is invalid."
    )


class NeonStorage(StorageBackend):
    """Transactional PostgreSQL backend for ``DATABASE_URL`` deployments."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        connect_factory: Callable[[], Any] | None = None,
    ):
        self.database_url = database_url or os.environ.get("DATABASE_URL", "").strip()
        self._connect_factory = connect_factory
        if not self.database_url and connect_factory is None:
            raise StorageError(
                "P4_DATABASE_CONFIGURATION_MISSING", "Hosted storage is not configured."
            )

    def _connect(self) -> Any:
        try:
            if self._connect_factory is not None:
                return self._connect_factory()
            try:
                import psycopg
            except ImportError as exc:
                raise StorageError(
                    "P4_DATABASE_DRIVER_MISSING",
                    "Hosted storage dependencies are not installed.",
                ) from exc
            return psycopg.connect(self.database_url)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                "P4_DATABASE_UNAVAILABLE",
                "Hosted storage is temporarily unavailable.",
            ) from exc

    @contextmanager
    def _safe_connection(self):
        from product4.workbench.request_db import current_request_connection

        request_connection = current_request_connection()
        if request_connection is not None:
            yield request_connection
            return
        try:
            with self._connect() as connection:
                yield connection
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                "P4_DATABASE_UNAVAILABLE",
                "Hosted storage is temporarily unavailable.",
            ) from exc

    @staticmethod
    def _payload(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _row_payload(row: Any) -> dict[str, Any]:
        if not row:
            raise StorageNotFound()
        return _decode_json(row[0])

    def load_session(self, session_id: str, owner_id: str | None = None) -> AuthoringSession:
        session, _ = self.load_session_with_token(session_id, owner_id)
        return session

    def load_session_with_token(
        self, session_id: str, owner_id: str | None = None
    ) -> tuple[AuthoringSession, int | str | None]:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if owner_id is None:
                cursor.execute(
                    "SELECT payload, row_generation FROM product4_sessions WHERE session_id = %s",
                    (session_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT payload, row_generation
                    FROM product4_sessions
                    WHERE session_id = %s AND owner_id = %s
                    """,
                    (session_id, owner_id),
                )
            row = cursor.fetchone()
        try:
            return (
                AuthoringSession.model_validate(self._row_payload(row)),
                row[1] if row else None,
            )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                "P4_SESSION_INVALID", "Stored session is invalid."
            ) from exc

    def save_session(
        self,
        session: AuthoringSession,
        expected_revision: int | None,
        *,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        payload = self._payload(session.model_dump(mode="json"))
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if expected_revision is None and owner_id is None:
                cursor.execute(
                    """
                        INSERT INTO product4_sessions(
                          session_id, revision, title, frozen_hash, payload
                        )
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (session_id) DO NOTHING
                        """,
                    (
                        session.id,
                        session.revision,
                        session.title,
                        session.frozen_hash,
                        payload,
                    ),
                )
            elif expected_revision is None:
                cursor.execute(
                    """
                        INSERT INTO product4_sessions(
                          session_id, owner_id, revision, title, frozen_hash, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (session_id) DO NOTHING
                        """,
                    (
                        session.id,
                        owner_id,
                        session.revision,
                        session.title,
                        session.frozen_hash,
                        payload,
                    ),
                )
            elif expected_generation is None and owner_id is None:
                cursor.execute(
                    """
                        UPDATE product4_sessions
                        SET revision = %s, title = %s, frozen_hash = %s,
                            payload = %s::jsonb, row_generation = row_generation + 1,
                            updated_at = NOW()
                        WHERE session_id = %s AND revision = %s
                          AND NOT EXISTS (
                            SELECT 1
                            FROM product4_publish_leases AS active_lease
                            WHERE active_lease.session_id = product4_sessions.session_id
                              AND active_lease.lease_until > NOW()
                          )
                        """,
                    (
                        session.revision,
                        session.title,
                        session.frozen_hash,
                        payload,
                        session.id,
                        expected_revision,
                    ),
                )
            elif owner_id is None:
                cursor.execute(
                    """
                        UPDATE product4_sessions
                        SET revision = %s, title = %s, frozen_hash = %s,
                            payload = %s::jsonb, row_generation = row_generation + 1,
                            updated_at = NOW()
                        WHERE session_id = %s AND revision = %s AND row_generation = %s
                          AND NOT EXISTS (
                            SELECT 1
                            FROM product4_publish_leases AS active_lease
                            WHERE active_lease.session_id = product4_sessions.session_id
                              AND active_lease.lease_until > NOW()
                          )
                        """,
                    (
                        session.revision,
                        session.title,
                        session.frozen_hash,
                        payload,
                        session.id,
                        expected_revision,
                        expected_generation,
                    ),
                )
            elif expected_generation is None:
                cursor.execute(
                    """
                        UPDATE product4_sessions
                        SET revision = %s, title = %s, frozen_hash = %s,
                            payload = %s::jsonb, row_generation = row_generation + 1,
                            updated_at = NOW()
                        WHERE session_id = %s AND owner_id = %s AND revision = %s
                          AND NOT EXISTS (
                            SELECT 1
                            FROM product4_publish_leases AS active_lease
                            WHERE active_lease.session_id = product4_sessions.session_id
                              AND active_lease.lease_until > NOW()
                          )
                        """,
                    (
                        session.revision,
                        session.title,
                        session.frozen_hash,
                        payload,
                        session.id,
                        owner_id,
                        expected_revision,
                    ),
                )
            else:
                cursor.execute(
                    """
                        UPDATE product4_sessions
                        SET revision = %s, title = %s, frozen_hash = %s,
                            payload = %s::jsonb, row_generation = row_generation + 1,
                            updated_at = NOW()
                        WHERE session_id = %s AND owner_id = %s AND revision = %s
                          AND row_generation = %s
                          AND NOT EXISTS (
                            SELECT 1
                            FROM product4_publish_leases AS active_lease
                            WHERE active_lease.session_id = product4_sessions.session_id
                              AND active_lease.lease_until > NOW()
                          )
                        """,
                    (
                        session.revision,
                        session.title,
                        session.frozen_hash,
                        payload,
                        session.id,
                        owner_id,
                        expected_revision,
                        expected_generation,
                    ),
                )
            if cursor.rowcount != 1:
                raise StorageRevisionConflict()

    def load_document(
        self, session_id: str, kind: str, owner_id: str | None = None
    ) -> dict[str, Any] | None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if owner_id is None:
                cursor.execute(
                    "SELECT payload FROM product4_documents WHERE session_id = %s AND kind = %s",
                    (session_id, kind),
                )
            else:
                cursor.execute(
                    """
                    SELECT document.payload
                    FROM product4_documents AS document
                    JOIN product4_sessions AS session ON session.session_id = document.session_id
                    WHERE document.session_id = %s AND document.kind = %s AND session.owner_id = %s
                    """,
                    (session_id, kind, owner_id),
                )
            row = cursor.fetchone()
        return None if row is None else self._row_payload(row)

    def load_view_documents(
        self, session_id: str, owner_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Load the private view artifacts in one owner-scoped round trip."""

        with self._safe_connection() as connection, connection.cursor() as cursor:
            if owner_id is None:
                cursor.execute(
                    """
                    SELECT kind, payload
                    FROM product4_documents
                    WHERE session_id = %s AND kind IN ('confirmation', 'pipeline', 'glific_result')
                    """,
                    (session_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT document.kind, document.payload
                    FROM product4_documents AS document
                    JOIN product4_sessions AS session
                      ON session.session_id = document.session_id
                    WHERE document.session_id = %s
                      AND document.kind IN ('confirmation', 'pipeline', 'glific_result')
                      AND session.owner_id = %s
                    """,
                    (session_id, owner_id),
                )
            rows = cursor.fetchall()
        return {str(kind): _decode_json(payload) for kind, payload in rows}

    @staticmethod
    def _document_binding(
        kind: str, payload: Mapping[str, Any]
    ) -> tuple[int, str | None, str | None]:
        if kind == "confirmation":
            return (
                int(payload.get("revision", -1)),
                str(payload.get("hash")) if payload.get("hash") else None,
                None,
            )
        if kind == "pipeline":
            stages = (
                payload.get("stages") if isinstance(payload.get("stages"), list) else []
            )
            artifact = next(
                (
                    item
                    for item in stages
                    if isinstance(item, dict)
                    and item.get("name") == "engine3_glific_artifact"
                ),
                None,
            )
            return (
                int(payload.get("session_revision", -1)),
                str(payload.get("frozen_package_hash"))
                if payload.get("frozen_package_hash")
                else None,
                str(artifact.get("canonical_hash"))
                if isinstance(artifact, dict) and artifact.get("canonical_hash")
                else None,
            )
        if kind == "glific_result":
            return (
                int(payload.get("session_revision", -1)),
                str(payload.get("frozen_package_hash"))
                if payload.get("frozen_package_hash")
                else None,
                str(payload.get("artifact_hash"))
                if payload.get("artifact_hash")
                else None,
            )
        raise StorageError(
            "P4_STORAGE_KIND_INVALID", "Unknown workbench storage document."
        )

    def save_document(
        self,
        session_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        expected_revision: int | None = None,
        expected_frozen_hash: str | None = None,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        revision, frozen_hash, artifact_hash = self._document_binding(kind, payload)
        if expected_revision is not None and (
            revision != expected_revision
            or (
                kind in {"pipeline", "glific_result"}
                and frozen_hash != expected_frozen_hash
            )
        ):
            raise StorageRevisionConflict(
                "The document payload is not bound to the current flow revision."
            )
        serialized = self._payload(payload)
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if expected_revision is not None:
                if owner_id is None:
                    cursor.execute(
                        """
                            SELECT revision, frozen_hash, row_generation
                            FROM product4_sessions
                            WHERE session_id = %s
                            FOR UPDATE
                            """,
                        (session_id,),
                    )
                else:
                    cursor.execute(
                        """
                            SELECT revision, frozen_hash, row_generation
                            FROM product4_sessions
                            WHERE session_id = %s AND owner_id = %s
                            FOR UPDATE
                            """,
                        (session_id, owner_id),
                    )
                session_row = cursor.fetchone()
                if (
                    session_row is None
                    or session_row[0] != expected_revision
                    or session_row[1] != expected_frozen_hash
                    or (
                        expected_generation is not None
                        and session_row[2] != expected_generation
                    )
                ):
                    raise StorageRevisionConflict(
                        "The document is no longer bound to the current flow revision."
                    )
                if kind in {"pipeline", "glific_result"}:
                    cursor.execute(
                        """
                            SELECT artifact_hash, owner
                            FROM product4_publish_leases
                            WHERE session_id = %s AND lease_until > NOW()
                            FOR UPDATE
                            """,
                        (session_id,),
                    )
                    if cursor.fetchone() is not None:
                        raise PublishLeaseBusy()
            cursor.execute(
                """
                    INSERT INTO product4_documents(session_id, kind, revision, frozen_hash, artifact_hash, payload)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (session_id, kind) DO UPDATE SET
                      revision = EXCLUDED.revision,
                      frozen_hash = EXCLUDED.frozen_hash,
                      artifact_hash = EXCLUDED.artifact_hash,
                      payload = EXCLUDED.payload,
                      updated_at = NOW()
                    """,
                (session_id, kind, revision, frozen_hash, artifact_hash, serialized),
            )

    def delete_document(
        self,
        session_id: str,
        kind: str,
        *,
        expected_revision: int | None = None,
        expected_frozen_hash: str | None = None,
        expected_generation: int | str | None = None,
        expected_document_revision: int | None = None,
        expected_document_frozen_hash: str | None = None,
        expected_artifact_hash: str | None = None,
        expected_document_payload: Mapping[str, Any] | None = None,
        owner_id: str | None = None,
    ) -> None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if (
                expected_revision is None
                and expected_document_revision is None
                and expected_document_frozen_hash is None
                and expected_artifact_hash is None
                and expected_document_payload is None
            ):
                raise StorageRevisionConflict(
                    "Document cleanup requires an expected binding."
                )
            if expected_revision is not None:
                if owner_id is None:
                    cursor.execute(
                        """
                            SELECT revision, frozen_hash, row_generation
                            FROM product4_sessions
                            WHERE session_id = %s
                            FOR UPDATE
                            """,
                        (session_id,),
                    )
                else:
                    cursor.execute(
                        """
                            SELECT revision, frozen_hash, row_generation
                            FROM product4_sessions
                            WHERE session_id = %s AND owner_id = %s
                            FOR UPDATE
                            """,
                        (session_id, owner_id),
                    )
                session_row = cursor.fetchone()
                if (
                    session_row is None
                    or session_row[0] != expected_revision
                    or session_row[1] != expected_frozen_hash
                    or (
                        expected_generation is not None
                        and session_row[2] != expected_generation
                    )
                ):
                    raise StorageRevisionConflict(
                        "The document is no longer bound to the current flow revision."
                    )
            if kind in {"pipeline", "glific_result"}:
                cursor.execute(
                    """
                        SELECT artifact_hash, owner
                        FROM product4_publish_leases
                        WHERE session_id = %s AND lease_until > NOW()
                        FOR UPDATE
                        """,
                    (session_id,),
                )
                if cursor.fetchone() is not None:
                    raise PublishLeaseBusy()
            clauses = ["session_id = %s", "kind = %s"]
            params: list[Any] = [session_id, kind]
            if expected_document_revision is not None:
                clauses.append("revision = %s")
                params.append(expected_document_revision)
            if expected_document_frozen_hash is not None:
                clauses.append("frozen_hash = %s")
                params.append(expected_document_frozen_hash)
            if expected_artifact_hash is not None:
                clauses.append("artifact_hash = %s")
                params.append(expected_artifact_hash)
            if expected_document_payload is not None:
                clauses.append("payload = %s::jsonb")
                params.append(self._payload(dict(expected_document_payload)))
            cursor.execute(
                f"DELETE FROM product4_documents WHERE {' AND '.join(clauses)}",
                tuple(params),
            )

    def clear_derived(
        self,
        session_id: str,
        *,
        expected_revision: int | None = None,
        expected_frozen_hash: str | None = None,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        if expected_revision is None:
            raise StorageRevisionConflict(
                "Derived cleanup requires the current session binding."
            )
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if owner_id is None:
                cursor.execute(
                    """
                        SELECT revision, frozen_hash, row_generation
                        FROM product4_sessions
                        WHERE session_id = %s
                        FOR UPDATE
                        """,
                    (session_id,),
                )
            else:
                cursor.execute(
                    """
                        SELECT revision, frozen_hash, row_generation
                        FROM product4_sessions
                        WHERE session_id = %s AND owner_id = %s
                        FOR UPDATE
                        """,
                    (session_id, owner_id),
                )
            session_row = cursor.fetchone()
            if (
                session_row is None
                or session_row[0] != expected_revision
                or session_row[1] != expected_frozen_hash
                or (
                    expected_generation is not None
                    and session_row[2] != expected_generation
                )
            ):
                raise StorageRevisionConflict(
                    "Derived documents are no longer bound to the current flow revision."
                )
            cursor.execute(
                """
                    SELECT artifact_hash, owner
                    FROM product4_publish_leases
                    WHERE session_id = %s AND lease_until > NOW()
                    FOR UPDATE
                    """,
                (session_id,),
            )
            if cursor.fetchone() is not None:
                raise PublishLeaseBusy()
            cursor.execute(
                """
                    DELETE FROM product4_documents
                    WHERE session_id = %s
                      AND kind IN ('confirmation', 'pipeline', 'glific_result')
                    """,
                (session_id,),
            )

    def replace_session_and_clear_derived(
        self,
        session: AuthoringSession,
        expected_revision: int | None,
        *,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        serialized = self._payload(session.model_dump(mode="json"))
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if expected_revision is None and owner_id is None:
                cursor.execute(
                    """
                        INSERT INTO product4_sessions(
                          session_id, revision, title, frozen_hash, payload
                        )
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (session_id) DO NOTHING
                        """,
                    (
                        session.id,
                        session.revision,
                        session.title,
                        session.frozen_hash,
                        serialized,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StorageRevisionConflict()
            elif expected_revision is None:
                cursor.execute(
                    """
                        INSERT INTO product4_sessions(
                          session_id, owner_id, revision, title, frozen_hash, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (session_id) DO NOTHING
                        """,
                    (
                        session.id,
                        owner_id,
                        session.revision,
                        session.title,
                        session.frozen_hash,
                        serialized,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StorageRevisionConflict()
            else:
                cursor.execute(
                    """
                    SELECT revision, frozen_hash, row_generation
                    FROM product4_sessions
                    WHERE session_id = %s
                    """ + (" AND owner_id = %s" if owner_id is not None else "") + " FOR UPDATE",
                    (session.id, owner_id) if owner_id is not None else (session.id,),
                )
                current = cursor.fetchone()
                if (
                    current is None
                    or current[0] != expected_revision
                    or (
                        expected_generation is not None
                        and current[2] != expected_generation
                    )
                ):
                    raise StorageRevisionConflict()
                cursor.execute(
                    """
                        SELECT artifact_hash, owner
                        FROM product4_publish_leases
                        WHERE session_id = %s AND lease_until > NOW()
                        FOR UPDATE
                        """,
                    (session.id,),
                )
                if cursor.fetchone() is not None:
                    raise PublishLeaseBusy()
                if expected_generation is None and owner_id is None:
                    cursor.execute(
                        """
                            UPDATE product4_sessions
                            SET revision = %s, title = %s, frozen_hash = %s,
                                payload = %s::jsonb, row_generation = row_generation + 1,
                                updated_at = NOW()
                            WHERE session_id = %s AND revision = %s
                            """,
                        (
                            session.revision,
                            session.title,
                            session.frozen_hash,
                            serialized,
                            session.id,
                            expected_revision,
                        ),
                    )
                elif owner_id is None:
                    cursor.execute(
                        """
                            UPDATE product4_sessions
                            SET revision = %s, title = %s, frozen_hash = %s,
                                payload = %s::jsonb, row_generation = row_generation + 1,
                                updated_at = NOW()
                            WHERE session_id = %s AND revision = %s AND row_generation = %s
                            """,
                        (
                            session.revision,
                            session.title,
                            session.frozen_hash,
                            serialized,
                            session.id,
                            expected_revision,
                            expected_generation,
                        ),
                    )
                elif expected_generation is None:
                    cursor.execute(
                        """
                            UPDATE product4_sessions
                            SET revision = %s, title = %s, frozen_hash = %s,
                                payload = %s::jsonb, row_generation = row_generation + 1,
                                updated_at = NOW()
                            WHERE session_id = %s AND owner_id = %s AND revision = %s
                            """,
                        (
                            session.revision,
                            session.title,
                            session.frozen_hash,
                            serialized,
                            session.id,
                            owner_id,
                            expected_revision,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                            UPDATE product4_sessions
                            SET revision = %s, title = %s, frozen_hash = %s,
                                payload = %s::jsonb, row_generation = row_generation + 1,
                                updated_at = NOW()
                            WHERE session_id = %s AND owner_id = %s AND revision = %s
                              AND row_generation = %s
                            """,
                        (
                            session.revision,
                            session.title,
                            session.frozen_hash,
                            serialized,
                            session.id,
                            owner_id,
                            expected_revision,
                            expected_generation,
                        ),
                    )
                if cursor.rowcount != 1:
                    raise StorageRevisionConflict()
            cursor.execute(
                """
                    DELETE FROM product4_documents
                    WHERE session_id = %s
                    AND kind IN ('confirmation', 'pipeline', 'glific_result')
                    """,
                (session.id,),
            )

    def replace_pipeline_and_invalidate_result(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        expected_old_pipeline_artifact_hash: str | None,
        expected_old_result: Mapping[str, Any] | None,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        revision, frozen_hash, artifact_hash = self._document_binding("pipeline", payload)
        if revision != expected_revision or frozen_hash != expected_frozen_hash:
            raise StorageRevisionConflict(
                "The pipeline is not bound to the current flow revision."
            )
        serialized = self._payload(payload)
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if owner_id is None:
                cursor.execute(
                    """
                        SELECT revision, frozen_hash, row_generation
                        FROM product4_sessions
                        WHERE session_id = %s
                        FOR UPDATE
                        """,
                    (session_id,),
                )
            else:
                cursor.execute(
                    """
                        SELECT revision, frozen_hash, row_generation
                        FROM product4_sessions
                        WHERE session_id = %s AND owner_id = %s
                        FOR UPDATE
                        """,
                    (session_id, owner_id),
                )
            session_row = cursor.fetchone()
            if (
                session_row is None
                or session_row[0] != expected_revision
                or session_row[1] != expected_frozen_hash
                or (
                    expected_generation is not None
                    and session_row[2] != expected_generation
                )
            ):
                raise StorageRevisionConflict(
                    "The pipeline is no longer bound to the current flow revision."
                )
            cursor.execute(
                """
                    SELECT artifact_hash, owner
                    FROM product4_publish_leases
                    WHERE session_id = %s AND lease_until > NOW()
                    FOR UPDATE
                    """,
                (session_id,),
            )
            if cursor.fetchone() is not None:
                raise PublishLeaseBusy()
            cursor.execute(
                """
                    SELECT revision, frozen_hash, artifact_hash, payload
                    FROM product4_documents
                    WHERE session_id = %s AND kind = 'pipeline'
                    FOR UPDATE
                    """,
                (session_id,),
            )
            old_pipeline = cursor.fetchone()
            old_artifact = old_pipeline[2] if old_pipeline is not None else None
            if old_pipeline is None:
                if expected_old_pipeline_artifact_hash is not None:
                    raise StorageRevisionConflict(
                        "The compiled artifact changed before pipeline replacement."
                    )
            elif old_artifact != expected_old_pipeline_artifact_hash:
                raise StorageRevisionConflict(
                    "The compiled artifact changed before pipeline replacement."
                )
            cursor.execute(
                """
                    INSERT INTO product4_documents(session_id, kind, revision, frozen_hash, artifact_hash, payload)
                    VALUES (%s, 'pipeline', %s, %s, %s, %s::jsonb)
                    ON CONFLICT (session_id, kind) DO UPDATE SET
                      revision = EXCLUDED.revision,
                      frozen_hash = EXCLUDED.frozen_hash,
                      artifact_hash = EXCLUDED.artifact_hash,
                      payload = EXCLUDED.payload,
                      updated_at = NOW()
                    """,
                (session_id, revision, frozen_hash, artifact_hash, serialized),
            )
            if old_artifact is not None and expected_old_result is not None:
                result_revision, result_frozen_hash, result_artifact_hash = self._document_binding(
                    "glific_result", expected_old_result
                )
                if result_artifact_hash != old_artifact:
                    raise StorageRevisionConflict(
                        "The publish result is not bound to the previous pipeline."
                    )
                cursor.execute(
                    """
                        DELETE FROM product4_documents
                        WHERE session_id = %s
                          AND kind = 'glific_result'
                          AND revision = %s
                          AND frozen_hash = %s
                          AND artifact_hash = %s
                          AND payload = %s::jsonb
                        """,
                    (
                        session_id,
                        result_revision,
                        result_frozen_hash,
                        old_artifact,
                        self._payload(dict(expected_old_result)),
                    ),
                )

    def list_sessions(self, owner_id: str | None = None) -> list[tuple[float, AuthoringSession]]:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if owner_id is None:
                cursor.execute(
                    "SELECT payload, EXTRACT(EPOCH FROM updated_at) FROM product4_sessions"
                )
            else:
                cursor.execute(
                    """
                    SELECT payload, EXTRACT(EPOCH FROM updated_at)
                    FROM product4_sessions
                    WHERE owner_id = %s
                    """,
                    (owner_id,),
                )
            rows = cursor.fetchall()
        result: list[tuple[float, AuthoringSession]] = []
        for row in rows:
            try:
                result.append(
                    (
                        float(row[1] or 0),
                        AuthoringSession.model_validate(self._row_payload((row[0],))),
                    )
                )
            except Exception:  # noqa: BLE001, S112 - one damaged draft must not hide the rest
                continue
        return result

    def list_session_summaries(
        self, owner_id: str | None = None, *, published_only: bool = False
    ) -> list[tuple[float, dict[str, Any]]]:
        """Return library fields and publication existence without hydrating drafts."""

        with self._safe_connection() as connection, connection.cursor() as cursor:
            publication_exists = """
              EXISTS (
                SELECT 1
                FROM product4_documents AS pipeline
                JOIN product4_documents AS result
                  ON result.session_id = pipeline.session_id
                 AND result.kind = 'glific_result'
                 AND result.revision = session.revision
                 AND result.frozen_hash IS NOT DISTINCT FROM session.frozen_hash
                 AND result.artifact_hash = pipeline.artifact_hash
                 AND result.payload->'result'->>'status' = 'published'
                WHERE pipeline.session_id = session.session_id
                  AND pipeline.kind = 'pipeline'
                  AND pipeline.revision = session.revision
                  AND pipeline.frozen_hash IS NOT DISTINCT FROM session.frozen_hash
                  AND pipeline.payload->>'all_stages_passed' = 'true'
                  AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(COALESCE(pipeline.payload->'stages', '[]'::jsonb)) AS stage
                    WHERE stage->>'name' = 'engine3_glific_artifact'
                      AND stage->>'canonical_hash' = result.artifact_hash
                  )
              )
            """
            conditions = []
            params: tuple[Any, ...] = ()
            if owner_id is not None:
                conditions.append("session.owner_id = %s")
                params = (owner_id,)
            if published_only:
                conditions.extend(["session.payload->>'state' = 'frozen'", publication_exists])
            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
            cursor.execute(
                f"""
                SELECT
                  session.session_id,
                  session.title,
                  session.revision,
                  session.updated_at,
                  session.payload->>'state' AS state,
                  COALESCE((
                    SELECT COUNT(DISTINCT node->>'source_statement')
                    FROM jsonb_array_elements(COALESCE(session.payload->'nodes', '[]'::jsonb)) AS node
                    WHERE BTRIM(COALESCE(node->>'source_statement', '')) <> ''
                  ), 0) AS segment_count,
                  COALESCE(session.payload->'flow_trigger_metadata'->'keywords', '[]'::jsonb) AS keywords,
                  {publication_exists} AS published
                FROM product4_sessions AS session
                {where_clause}
                ORDER BY session.updated_at DESC, session.session_id
                """,
                params,
            )
            rows = cursor.fetchall()
        summaries: list[tuple[float, dict[str, Any]]] = []
        for session_id, title, revision, updated_at, state, segment_count, keywords, published in rows:
            try:
                keyword_values = keywords if isinstance(keywords, list) else json.loads(keywords)
            except (TypeError, json.JSONDecodeError):
                keyword_values = []
            summaries.append(
                (
                    float(updated_at.timestamp()) if updated_at is not None else 0.0,
                    {
                        "id": session_id,
                        "title": title,
                        "state": state,
                        "revision": revision,
                        "segment_count": int(segment_count or 0),
                        "keywords": [
                            item.get("value") for item in keyword_values
                            if isinstance(item, dict) and item.get("value")
                        ],
                        "published": bool(published),
                    },
                )
            )
        return summaries

    def acquire_publish_lease(
        self,
        session_id: str,
        artifact_hash: str,
        owner: str,
        ttl_seconds: int,
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            # The session row lock makes the binding check and lease grant one
            # transaction.  No caller can mutate the session between these
            # checks and the external Glific request.
            if owner_id is None:
                cursor.execute(
                    """
                        SELECT revision, frozen_hash, row_generation
                        FROM product4_sessions
                        WHERE session_id = %s
                        FOR UPDATE
                        """,
                    (session_id,),
                )
            else:
                cursor.execute(
                    """
                        SELECT revision, frozen_hash, row_generation
                        FROM product4_sessions
                        WHERE session_id = %s AND owner_id = %s
                        FOR UPDATE
                        """,
                    (session_id, owner_id),
                )
            session_row = cursor.fetchone()
            if (
                session_row is None
                or session_row[0] != expected_revision
                or session_row[1] != expected_frozen_hash
                or (
                    expected_generation is not None
                    and session_row[2] != expected_generation
                )
            ):
                raise StorageRevisionConflict(
                    "The flow changed before publication started."
                )
            cursor.execute(
                """
                    SELECT revision, frozen_hash, artifact_hash, payload
                    FROM product4_documents
                    WHERE session_id = %s AND kind = 'pipeline'
                    FOR UPDATE
                    """,
                (session_id,),
            )
            pipeline_row = cursor.fetchone()
            pipeline = None
            if pipeline_row is not None:
                pipeline = _decode_json(pipeline_row[3])
            if (
                pipeline_row is None
                or pipeline_row[0] != expected_revision
                or pipeline_row[1] != expected_frozen_hash
                or pipeline_row[2] != artifact_hash
                or not _pipeline_matches(
                    pipeline,
                    expected_revision=expected_revision,
                    expected_frozen_hash=expected_frozen_hash,
                    artifact_hash=artifact_hash,
                )
            ):
                raise StorageRevisionConflict(
                    "The compiled artifact is no longer bound to this flow revision."
                )
            cursor.execute(
                """
                    INSERT INTO product4_publish_leases(session_id, artifact_hash, owner, lease_until)
                    VALUES (%s, %s, %s, NOW() + (%s * INTERVAL '1 second'))
                    ON CONFLICT (session_id) DO UPDATE SET
                      artifact_hash = EXCLUDED.artifact_hash,
                      owner = EXCLUDED.owner,
                      lease_until = EXCLUDED.lease_until,
                      acquired_at = NOW()
                    WHERE product4_publish_leases.lease_until <= NOW()
                    """,
                (session_id, artifact_hash, owner, ttl_seconds),
            )
            if cursor.rowcount != 1:
                raise PublishLeaseBusy()

    def release_publish_lease(
        self, session_id: str, owner: str, owner_id: str | None = None
    ) -> None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM product4_publish_leases WHERE session_id = %s AND owner = %s",
                (session_id, owner),
            )

    def record_publish_result(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        expected_frozen_hash: str,
        artifact_hash: str,
        owner: str,
        expected_generation: int | str | None = None,
        owner_id: str | None = None,
    ) -> None:
        revision, frozen_hash, stored_artifact_hash = self._document_binding(
            "glific_result", payload
        )
        if (
            revision != expected_revision
            or frozen_hash != expected_frozen_hash
            or stored_artifact_hash != artifact_hash
        ):
            raise StorageRevisionConflict(
                "The publish result is not bound to the current flow artifact."
            )
        serialized = self._payload(payload)
        with self._safe_connection() as connection, connection.cursor() as cursor:
            if owner_id is None:
                cursor.execute(
                    """
                        SELECT revision, frozen_hash, row_generation
                        FROM product4_sessions
                        WHERE session_id = %s
                        FOR UPDATE
                        """,
                    (session_id,),
                )
            else:
                cursor.execute(
                    """
                        SELECT revision, frozen_hash, row_generation
                        FROM product4_sessions
                        WHERE session_id = %s AND owner_id = %s
                        FOR UPDATE
                        """,
                    (session_id, owner_id),
                )
            row = cursor.fetchone()
            if (
                row is None
                or row[0] != expected_revision
                or row[1] != expected_frozen_hash
                or (
                    expected_generation is not None
                    and row[2] != expected_generation
                )
            ):
                raise StorageRevisionConflict(
                    "Local flow changed before the publish result was saved."
                )
            cursor.execute(
                """
                    SELECT artifact_hash, revision, frozen_hash, payload
                    FROM product4_documents
                    WHERE session_id = %s AND kind = 'pipeline'
                    FOR UPDATE
                    """,
                (session_id,),
            )
            pipeline = cursor.fetchone()
            pipeline_payload = None if pipeline is None else _decode_json(pipeline[3])
            if (
                pipeline is None
                or pipeline[0] != artifact_hash
                or pipeline[1] != expected_revision
                or pipeline[2] != expected_frozen_hash
                or not _pipeline_matches(
                    pipeline_payload,
                    expected_revision=expected_revision,
                    expected_frozen_hash=expected_frozen_hash,
                    artifact_hash=artifact_hash,
                )
            ):
                raise StorageRevisionConflict(
                    "The compiled artifact is no longer bound to this flow revision."
                )
            cursor.execute(
                """
                    SELECT artifact_hash, owner
                    FROM product4_publish_leases
                    WHERE session_id = %s AND lease_until > NOW()
                    FOR UPDATE
                    """,
                (session_id,),
            )
            lease = cursor.fetchone()
            if lease is None or lease[0] != artifact_hash or lease[1] != owner:
                raise StorageRevisionConflict(
                    "The publish lease is no longer owned by this worker."
                )
            cursor.execute(
                """
                    INSERT INTO product4_documents(session_id, kind, revision, frozen_hash, artifact_hash, payload)
                    VALUES (%s, 'glific_result', %s, %s, %s, %s::jsonb)
                    ON CONFLICT (session_id, kind) DO UPDATE SET
                      revision = EXCLUDED.revision,
                      frozen_hash = EXCLUDED.frozen_hash,
                      artifact_hash = EXCLUDED.artifact_hash,
                      payload = EXCLUDED.payload,
                      updated_at = NOW()
                    """,
                (session_id, revision, frozen_hash, stored_artifact_hash, serialized),
            )
            cursor.execute(
                "DELETE FROM product4_publish_leases WHERE session_id = %s AND owner = %s",
                (session_id, owner),
            )


def build_storage(
    *,
    data_root: Path,
    sessions_root: Path,
    artifacts_root: Path,
    confirmations_root: Path,
    glific_results_root: Path,
) -> StorageBackend:
    """Select hosted storage only when ``DATABASE_URL`` is explicitly set."""

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        return NeonStorage(database_url)
    return FilesystemStorage(
        data_root,
        sessions_root,
        artifacts_root,
        confirmations_root,
        glific_results_root,
    )


__all__ = [
    "FilesystemStorage",
    "NeonStorage",
    "PublishLeaseBusy",
    "StorageBackend",
    "StorageError",
    "StorageNotFound",
    "StorageRevisionConflict",
    "build_storage",
]
