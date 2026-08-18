"""Native account authentication and the replaceable identity boundary.

The workbench deliberately keeps identity independent from the authoring and
Glific code.  The current adapter is native email/password authentication;
future identity providers can implement the same ``IdentityProvider`` seam
without changing ownership checks or credential handling.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


AUTH_SESSION_COOKIE = "autoglific_session"
CSRF_COOKIE = "autoglific_csrf"
AUTH_SESSION_SECONDS = 7 * 24 * 60 * 60
CSRF_TOKEN_BYTES = 32
SESSION_TOKEN_BYTES = 32
MAX_EMAIL_LENGTH = 254
MAX_PASSWORD_LENGTH = 128
MIN_PASSWORD_LENGTH = 8
BOOTSTRAP_EMAIL_ENV = "PRODUCT4_BOOTSTRAP_EMAIL"
BOOTSTRAP_PASSWORD_ENV = "PRODUCT4_BOOTSTRAP_PASSWORD"
BOOTSTRAP_NAME_ENV = "PRODUCT4_BOOTSTRAP_NAME"
DISPLAY_NAME_MIN_LENGTH = 2
DISPLAY_NAME_MAX_LENGTH = 100
NEUTRAL_DISPLAY_NAME = "Account"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class AuthError(Exception):
    """Safe authentication failure with a stable API code."""

    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class AuthStoreError(RuntimeError):
    """Safe persistence failure for identity records."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    auth_method: str = "native-email-password"
    display_name: str = NEUTRAL_DISPLAY_NAME


@dataclass(frozen=True)
class StoredSession:
    user_id: str
    csrf_hash: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthResult:
    payload: dict[str, Any]
    principal: Principal | None = None
    session_token: str | None = None
    csrf_token: str | None = None
    clear_session: bool = False


class IdentityProvider(Protocol):
    """Provider-independent request-to-principal boundary."""

    def resolve(
        self,
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
    ) -> Principal:
        """Return the authenticated principal or raise ``AuthError``."""


class UserStore(Protocol):
    """Durable account/session/credential persistence required by auth."""

    def create_user(
        self,
        email: str,
        password_hash: str,
        display_name: str = NEUTRAL_DISPLAY_NAME,
    ) -> dict[str, Any]: ...

    def update_password_hash(self, user_id: str, password_hash: str) -> None: ...

    def update_display_name(self, user_id: str, display_name: str) -> None: ...

    def mark_bootstrap_credentials_seeded(self, user_id: str) -> None: ...

    def find_user_by_email(self, email: str) -> dict[str, Any] | None: ...

    def get_user(self, user_id: str) -> dict[str, Any] | None: ...

    def create_session(
        self,
        session_hash: str,
        user_id: str,
        csrf_hash: str,
        expires_at: datetime,
    ) -> None: ...

    def get_session(self, session_hash: str) -> StoredSession | None: ...

    def delete_session(self, session_hash: str) -> None: ...

    def load_credentials(self, user_id: str) -> str | None: ...

    def save_credentials(self, user_id: str, ciphertext: str) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_email(value: Any) -> str:
    if not isinstance(value, str):
        raise AuthError("P4_AUTH_EMAIL_INVALID", "Enter a valid email address.", 400)
    email = value.strip().casefold()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.fullmatch(email):
        raise AuthError("P4_AUTH_EMAIL_INVALID", "Enter a valid email address.", 400)
    return email


def validate_password(value: Any) -> str:
    if not isinstance(value, str):
        raise AuthError("P4_AUTH_PASSWORD_INVALID", "Password must be text.", 400)
    if not MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH:
        raise AuthError(
            "P4_AUTH_PASSWORD_INVALID",
            f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters.",
            400,
        )
    return value


def normalize_display_name(value: Any) -> str:
    """Validate a human-facing account label without changing its casing."""

    if not isinstance(value, str):
        raise AuthError(
            "P4_AUTH_DISPLAY_NAME_INVALID",
            "Enter a display name.",
            400,
        )
    raw = value.strip()
    if any(unicodedata.category(character).startswith("C") for character in raw):
        raise AuthError(
            "P4_AUTH_DISPLAY_NAME_INVALID",
            "Display name contains an invalid control character.",
            400,
        )
    normalized = " ".join(raw.split())
    if not DISPLAY_NAME_MIN_LENGTH <= len(normalized) <= DISPLAY_NAME_MAX_LENGTH:
        raise AuthError(
            "P4_AUTH_DISPLAY_NAME_INVALID",
            f"Display name must be {DISPLAY_NAME_MIN_LENGTH}-{DISPLAY_NAME_MAX_LENGTH} characters.",
            400,
        )
    return normalized


def _stored_display_name(value: Any) -> str:
    try:
        return normalize_display_name(value)
    except AuthError:
        return NEUTRAL_DISPLAY_NAME


def bootstrap_owner_account(
    store: UserStore,
    *,
    email: Any | None = None,
    password: Any | None = None,
    display_name: Any | None = None,
) -> bool:
    """Create or reconcile only the configured owner account.

    The configured password is accepted only from the caller/environment and
    is hashed immediately.  An existing account is updated only when its
    stored hash does not verify against that configured password; all other
    accounts and credential ciphertext remain untouched.
    """

    configured_email = (
        os.environ.get(BOOTSTRAP_EMAIL_ENV, "") if email is None else email
    )
    configured_password = (
        os.environ.get(BOOTSTRAP_PASSWORD_ENV, "") if password is None else password
    )
    configured_display_name = (
        os.environ.get(BOOTSTRAP_NAME_ENV, "")
        if display_name is None
        else display_name
    )
    if not configured_email and not configured_password:
        return False
    if not configured_email or not configured_password:
        raise AuthStoreError(
            "P4_BOOTSTRAP_CONFIGURATION_INVALID",
            "Owner bootstrap requires both server-side email and password settings.",
        )

    normalized_email = normalize_email(configured_email)
    validated_password = validate_password(configured_password)
    normalized_display_name = (
        normalize_display_name(configured_display_name)
        if configured_display_name
        else None
    )

    def reconcile_name(existing: Mapping[str, Any]) -> bool:
        if not normalized_display_name:
            return False
        current = _stored_display_name(existing.get("display_name"))
        if existing.get("display_name") and current != NEUTRAL_DISPLAY_NAME:
            return False
        user_id = _safe_user_id(existing.get("user_id"))
        store.update_display_name(user_id, normalized_display_name)
        return True

    def reconcile_existing(existing: Mapping[str, Any]) -> bool:
        changed = False
        password_hasher = PasswordHasher()
        try:
            matches = password_hasher.verify(
                str(existing.get("password_hash") or ""),
                validated_password,
            )
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            matches = False
        if not matches:
            user_id = _safe_user_id(existing.get("user_id"))
            store.update_password_hash(user_id, password_hasher.hash(validated_password))
            changed = True
        return reconcile_name(existing) or changed

    try:
        existing = store.find_user_by_email(normalized_email)
    except AuthStoreError:
        raise
    password_hasher = PasswordHasher()
    if existing is not None:
        return reconcile_existing(existing)

    password_hash = password_hasher.hash(validated_password)
    try:
        store.create_user(
            normalized_email,
            password_hash,
            normalized_display_name or NEUTRAL_DISPLAY_NAME,
        )
    except AuthStoreError as exc:
        # A concurrent startup may win the insert between the lookup and the
        # create. Re-read and reconcile the same configured account.
        if exc.code == "P4_AUTH_EMAIL_EXISTS":
            existing = store.find_user_by_email(normalized_email)
            if existing is None:
                raise
            return reconcile_existing(existing)
        raise
    return True


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_user_id(value: Any) -> str:
    user_id = str(value or "").strip()
    if not re.fullmatch(r"usr-[A-Za-z0-9_-]{16,80}", user_id):
        raise AuthStoreError("P4_AUTH_STORE_INVALID", "Stored identity is invalid.")
    return user_id


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=".p4-auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AuthStoreError("P4_AUTH_STORE_INVALID", "Stored identity is invalid.") from exc
    if not isinstance(value, dict):
        raise AuthStoreError("P4_AUTH_STORE_INVALID", "Stored identity is invalid.")
    return value


class FilesystemUserStore:
    """Local durable account store kept outside session and evidence files."""

    def __init__(self, data_root: Path):
        self.root = data_root / "auth"
        self.users_root = self.root / "users"
        self.email_index_root = self.root / "email-index"
        self.sessions_root = self.root / "sessions"
        for path in (self.root, self.users_root, self.email_index_root, self.sessions_root):
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        self._lock = threading.RLock()

    @staticmethod
    def _email_hash(email: str) -> str:
        return hashlib.sha256(email.encode("utf-8")).hexdigest()

    def _user_path(self, user_id: str) -> Path:
        return self.users_root / f"{_safe_user_id(user_id)}.json"

    def _index_path(self, email: str) -> Path:
        return self.email_index_root / f"{self._email_hash(email)}.txt"

    def _session_path(self, session_hash: str) -> Path:
        if not _HEX_RE.fullmatch(session_hash):
            raise AuthStoreError("P4_AUTH_STORE_INVALID", "Stored session is invalid.")
        return self.sessions_root / f"{session_hash}.json"

    def _hydrate_user(self, path: Path) -> dict[str, Any] | None:
        payload = _read_json(path)
        if payload is None:
            return None
        changed = False
        if not isinstance(payload.get("display_name"), str) or not payload.get("display_name", "").strip():
            payload["display_name"] = NEUTRAL_DISPLAY_NAME
            changed = True
        if not isinstance(payload.get("bootstrap_credentials_seeded"), bool):
            payload["bootstrap_credentials_seeded"] = False
            changed = True
        if changed:
            payload["updated_at"] = _utc_now().isoformat()
            _atomic_json(path, payload)
        return payload

    def create_user(
        self,
        email: str,
        password_hash: str,
        display_name: str = NEUTRAL_DISPLAY_NAME,
    ) -> dict[str, Any]:
        with self._lock:
            index = self._index_path(email)
            if index.exists():
                raise AuthStoreError("P4_AUTH_EMAIL_EXISTS", "An account already exists for that email.")
            user_id = f"usr-{uuid.uuid4().hex}"
            now = _utc_now().isoformat()
            payload = {
                "user_id": user_id,
                "email": email,
                "display_name": _stored_display_name(display_name),
                "password_hash": password_hash,
                "credentials_ciphertext": None,
                "bootstrap_credentials_seeded": False,
                "created_at": now,
                "updated_at": now,
            }
            _atomic_json(self._user_path(user_id), payload)
            try:
                index.write_text(user_id, encoding="ascii")
                os.chmod(index, 0o600)
            except Exception:
                self._user_path(user_id).unlink(missing_ok=True)
                raise
            return payload

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._lock:
            path = self._user_path(user_id)
            payload = _read_json(path)
            if payload is None:
                raise AuthStoreError("P4_AUTH_REQUIRED", "Authenticated account not found.")
            payload["password_hash"] = password_hash
            payload["updated_at"] = _utc_now().isoformat()
            _atomic_json(path, payload)

    def update_display_name(self, user_id: str, display_name: str) -> None:
        with self._lock:
            path = self._user_path(user_id)
            payload = _read_json(path)
            if payload is None:
                raise AuthStoreError("P4_AUTH_REQUIRED", "Authenticated account not found.")
            payload["display_name"] = _stored_display_name(display_name)
            payload["updated_at"] = _utc_now().isoformat()
            _atomic_json(path, payload)

    def mark_bootstrap_credentials_seeded(self, user_id: str) -> None:
        with self._lock:
            path = self._user_path(user_id)
            payload = _read_json(path)
            if payload is None:
                raise AuthStoreError("P4_AUTH_REQUIRED", "Authenticated account not found.")
            payload["bootstrap_credentials_seeded"] = True
            payload["updated_at"] = _utc_now().isoformat()
            _atomic_json(path, payload)

    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                user_id = self._index_path(email).read_text(encoding="ascii").strip()
            except FileNotFoundError:
                return None
            except (OSError, UnicodeDecodeError) as exc:
                raise AuthStoreError("P4_AUTH_STORE_INVALID", "Stored identity is invalid.") from exc
            return self._hydrate_user(self._user_path(user_id))

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._hydrate_user(self._user_path(user_id))

    def create_session(
        self,
        session_hash: str,
        user_id: str,
        csrf_hash: str,
        expires_at: datetime,
    ) -> None:
        with self._lock:
            if self.get_user(user_id) is None:
                raise AuthStoreError("P4_AUTH_STORE_INVALID", "Stored identity is invalid.")
            _atomic_json(
                self._session_path(session_hash),
                {
                    "user_id": user_id,
                    "csrf_hash": csrf_hash,
                    "expires_at": expires_at.isoformat(),
                },
            )

    def get_session(self, session_hash: str) -> StoredSession | None:
        with self._lock:
            payload = _read_json(self._session_path(session_hash))
            if payload is None:
                return None
            try:
                expires_at = datetime.fromisoformat(str(payload["expires_at"]))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                session = StoredSession(
                    user_id=_safe_user_id(payload["user_id"]),
                    csrf_hash=str(payload["csrf_hash"]),
                    expires_at=expires_at.astimezone(UTC),
                )
            except (KeyError, TypeError, ValueError, AuthStoreError) as exc:
                raise AuthStoreError("P4_AUTH_STORE_INVALID", "Stored session is invalid.") from exc
            if session.expires_at <= _utc_now():
                self._session_path(session_hash).unlink(missing_ok=True)
                return None
            return session

    def delete_session(self, session_hash: str) -> None:
        with self._lock:
            self._session_path(session_hash).unlink(missing_ok=True)

    def load_credentials(self, user_id: str) -> str | None:
        with self._lock:
            payload = _read_json(self._user_path(user_id))
            if payload is None:
                return None
            value = payload.get("credentials_ciphertext")
            return value if isinstance(value, str) and value else None

    def save_credentials(self, user_id: str, ciphertext: str) -> None:
        with self._lock:
            path = self._user_path(user_id)
            payload = _read_json(path)
            if payload is None:
                raise AuthStoreError("P4_AUTH_REQUIRED", "Authenticated account not found.")
            payload["credentials_ciphertext"] = ciphertext
            payload["updated_at"] = _utc_now().isoformat()
            _atomic_json(path, payload)


class NeonUserStore:
    """PostgreSQL implementation for hosted account/session data."""

    def __init__(self, database_url: str | None = None, *, connect_factory: Any | None = None):
        self.database_url = database_url or os.environ.get("DATABASE_URL", "").strip()
        self._connect_factory = connect_factory
        if not self.database_url and connect_factory is None:
            raise AuthStoreError("P4_DATABASE_CONFIGURATION_MISSING", "Hosted storage is not configured.")

    def _connect(self) -> Any:
        try:
            if self._connect_factory is not None:
                return self._connect_factory()
            import psycopg

            return psycopg.connect(self.database_url)
        except AuthStoreError:
            raise
        except Exception as exc:
            raise AuthStoreError("P4_DATABASE_UNAVAILABLE", "Hosted storage is temporarily unavailable.") from exc

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
        except AuthStoreError:
            raise
        except Exception as exc:
            raise AuthStoreError("P4_DATABASE_UNAVAILABLE", "Hosted storage is temporarily unavailable.") from exc

    @staticmethod
    def _user(row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "user_id": str(row[0]),
            "email": str(row[1]),
            "password_hash": str(row[2]),
            "credentials_ciphertext": row[3] if isinstance(row[3], str) else None,
            "display_name": _stored_display_name(row[4] if len(row) > 4 else None),
            "bootstrap_credentials_seeded": bool(row[5]) if len(row) > 5 else False,
        }

    def create_user(
        self,
        email: str,
        password_hash: str,
        display_name: str = NEUTRAL_DISPLAY_NAME,
    ) -> dict[str, Any]:
        user_id = f"usr-{uuid.uuid4().hex}"
        try:
            with self._safe_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO product4_users(user_id, email, password_hash, display_name)
                    VALUES (%s, %s, %s, %s)
                    RETURNING user_id, email, password_hash, credentials_ciphertext,
                              display_name, bootstrap_credentials_seeded
                    """,
                    (user_id, email, password_hash, _stored_display_name(display_name)),
                )
                row = cursor.fetchone()
        except Exception as exc:
            try:
                from psycopg.errors import UniqueViolation
            except ImportError:
                UniqueViolation = ()  # type: ignore[assignment]
            database_error: BaseException | None = exc
            while database_error is not None:
                if UniqueViolation and isinstance(database_error, UniqueViolation):
                    raise AuthStoreError(
                        "P4_AUTH_EMAIL_EXISTS",
                        "An account already exists for that email.",
                    ) from exc
                database_error = database_error.__cause__ or database_error.__context__
            if isinstance(exc, AuthStoreError):
                raise
            raise AuthStoreError("P4_DATABASE_UNAVAILABLE", "Hosted storage is temporarily unavailable.") from exc
        value = self._user(row)
        if value is None:
            raise AuthStoreError("P4_AUTH_STORE_INVALID", "Stored identity is invalid.")
        return value

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE product4_users
                SET password_hash = %s, updated_at = NOW()
                WHERE user_id = %s
                """,
                (password_hash, user_id),
            )
            if cursor.rowcount != 1:
                raise AuthStoreError("P4_AUTH_REQUIRED", "Authenticated account not found.")

    def update_display_name(self, user_id: str, display_name: str) -> None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE product4_users
                SET display_name = %s, updated_at = NOW()
                WHERE user_id = %s
                """,
                (_stored_display_name(display_name), user_id),
            )
            if cursor.rowcount != 1:
                raise AuthStoreError("P4_AUTH_REQUIRED", "Authenticated account not found.")

    def mark_bootstrap_credentials_seeded(self, user_id: str) -> None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE product4_users
                SET bootstrap_credentials_seeded = TRUE, updated_at = NOW()
                WHERE user_id = %s
                """,
                (user_id,),
            )
            if cursor.rowcount != 1:
                raise AuthStoreError("P4_AUTH_REQUIRED", "Authenticated account not found.")

    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, email, password_hash, credentials_ciphertext,
                       display_name, bootstrap_credentials_seeded
                FROM product4_users WHERE email = %s
                """,
                (email,),
            )
            return self._user(cursor.fetchone())

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, email, password_hash, credentials_ciphertext,
                       display_name, bootstrap_credentials_seeded
                FROM product4_users WHERE user_id = %s
                """,
                (user_id,),
            )
            return self._user(cursor.fetchone())

    def create_session(
        self,
        session_hash: str,
        user_id: str,
        csrf_hash: str,
        expires_at: datetime,
    ) -> None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO product4_auth_sessions(session_hash, user_id, csrf_hash, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (session_hash, user_id, csrf_hash, expires_at),
            )

    def get_session(self, session_hash: str) -> StoredSession | None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT user_id, csrf_hash, expires_at FROM product4_auth_sessions WHERE session_hash = %s",
                (session_hash,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            expires_at = row[2]
            if not isinstance(expires_at, datetime):
                expires_at = datetime.fromisoformat(str(expires_at))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            session = StoredSession(str(row[0]), str(row[1]), expires_at.astimezone(UTC))
            if session.expires_at <= _utc_now():
                cursor.execute(
                    "DELETE FROM product4_auth_sessions WHERE session_hash = %s",
                    (session_hash,),
                )
                return None
            return session

    def get_session_user(
        self, session_hash: str
    ) -> tuple[StoredSession, dict[str, Any]] | None:
        """Resolve an auth session and its owner in one connection/query."""

        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT auth.user_id, auth.csrf_hash, auth.expires_at,
                       users.user_id, users.email, users.password_hash,
                       users.credentials_ciphertext, users.display_name,
                       users.bootstrap_credentials_seeded
                FROM product4_auth_sessions AS auth
                JOIN product4_users AS users ON users.user_id = auth.user_id
                WHERE auth.session_hash = %s
                """,
                (session_hash,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            expires_at = row[2]
            if not isinstance(expires_at, datetime):
                expires_at = datetime.fromisoformat(str(expires_at))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            expires_at = expires_at.astimezone(UTC)
            if expires_at <= _utc_now():
                cursor.execute(
                    "DELETE FROM product4_auth_sessions WHERE session_hash = %s",
                    (session_hash,),
                )
                return None
            return (
                StoredSession(str(row[0]), str(row[1]), expires_at),
                self._user((row[3], row[4], row[5], row[6], row[7], row[8])) or {},
            )

    def delete_session(self, session_hash: str) -> None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM product4_auth_sessions WHERE session_hash = %s",
                (session_hash,),
            )

    def load_credentials(self, user_id: str) -> str | None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT credentials_ciphertext FROM product4_users WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            return row[0] if row and isinstance(row[0], str) and row[0] else None

    def save_credentials(self, user_id: str, ciphertext: str) -> None:
        with self._safe_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE product4_users
                SET credentials_ciphertext = %s, updated_at = NOW()
                WHERE user_id = %s
                """,
                (ciphertext, user_id),
            )
            if cursor.rowcount != 1:
                raise AuthStoreError("P4_AUTH_REQUIRED", "Authenticated account not found.")


def build_user_store(
    *,
    data_root: Path,
    database_url: str | None = None,
    connect_factory: Any | None = None,
) -> UserStore:
    configured_database_url = (database_url or os.environ.get("DATABASE_URL", "")).strip()
    if configured_database_url or connect_factory is not None:
        return NeonUserStore(configured_database_url, connect_factory=connect_factory)
    return FilesystemUserStore(data_root)


class _LoginRateLimiter:
    def __init__(self, *, limit: int = 5, window_seconds: int = 15 * 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            values = [item for item in self._failures.get(key, []) if now - item < self.window_seconds]
            self._failures[key] = values
            if len(values) < self.limit:
                return 0
            return max(1, int(self.window_seconds - (now - values[0])))

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            values = [item for item in self._failures.get(key, []) if now - item < self.window_seconds]
            values.append(now)
            self._failures[key] = values[-self.limit :]

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class NativeEmailPasswordProvider:
    """Native identity provider with server-side opaque sessions."""

    def __init__(
        self,
        store: UserStore,
        *,
        cookie_secure: bool = True,
        test_mode: bool = False,
    ):
        self.store = store
        self.cookie_secure = cookie_secure
        self.test_mode = test_mode
        self.password_hasher = PasswordHasher()
        self.rate_limiter = _LoginRateLimiter()
        self._test_principal = Principal(
            "usr-test-user",
            "test@example.invalid",
            "test",
            "Test user",
        )
        self._test_csrf = "test-csrf-token"

    def _user_principal(self, user: Mapping[str, Any]) -> Principal:
        return Principal(
            _safe_user_id(user.get("user_id")),
            str(user.get("email")),
            display_name=_stored_display_name(user.get("display_name")),
        )

    def _new_session(self, user: Mapping[str, Any]) -> AuthResult:
        session_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        csrf_token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
        self.store.create_session(
            hash_token(session_token),
            _safe_user_id(user.get("user_id")),
            hash_token(csrf_token),
            _utc_now() + timedelta(seconds=AUTH_SESSION_SECONDS),
        )
        principal = self._user_principal(user)
        return AuthResult(
            payload={
                "authenticated": True,
                "user": {
                    "id": principal.user_id,
                    "email": principal.email,
                    "display_name": principal.display_name,
                },
                # CSRF tokens are not credentials; returning the token lets
                # the browser update its in-memory double-submit header after
                # a new session is issued.
                "csrf_token": csrf_token,
            },
            principal=principal,
            session_token=session_token,
            csrf_token=csrf_token,
        )

    def csrf(self, cookies: Mapping[str, str]) -> AuthResult:
        if self.test_mode:
            return AuthResult({"csrf_token": self._test_csrf}, csrf_token=self._test_csrf)
        token = cookies.get(CSRF_COOKIE, "")
        if not token or len(token) < 32 or len(token) > 256:
            token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
        return AuthResult({"csrf_token": token}, csrf_token=token)

    def _require_double_submit(self, cookies: Mapping[str, str], headers: Mapping[str, str]) -> None:
        if self.test_mode:
            return
        cookie_token = str(cookies.get(CSRF_COOKIE) or "")
        header_token = str(headers.get("x-csrf-token") or headers.get("X-CSRF-Token") or "")
        if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
            raise AuthError("P4_CSRF_REQUIRED", "A valid CSRF token is required.", 403)

    def resolve(self, headers: Mapping[str, str], cookies: Mapping[str, str]) -> Principal:
        if self.test_mode:
            return self._test_principal
        token = str(cookies.get(AUTH_SESSION_COOKIE) or "")
        if not token:
            raise AuthError("P4_AUTH_REQUIRED", "Sign in to use AutoGlific.", 401)
        try:
            resolved = self.store.get_session_user(hash_token(token)) if hasattr(self.store, "get_session_user") else None
            if resolved is None and not hasattr(self.store, "get_session_user"):
                stored = self.store.get_session(hash_token(token))
                user = self.store.get_user(stored.user_id) if stored is not None else None
            else:
                stored, user = resolved or (None, None)
        except AuthStoreError as exc:
            raise AuthError(exc.code, exc.message, 503) from exc
        if stored is None:
            raise AuthError("P4_AUTH_INVALID_SESSION", "Your session has expired. Sign in again.", 401)
        if user is None:
            raise AuthError("P4_AUTH_INVALID_SESSION", "Your session has expired. Sign in again.", 401)
        return self._user_principal(user)

    def require_csrf(self, headers: Mapping[str, str], cookies: Mapping[str, str]) -> Principal:
        principal = self.resolve(headers, cookies)
        self._require_double_submit(cookies, headers)
        if self.test_mode:
            return principal
        session_token = str(cookies.get(AUTH_SESSION_COOKIE) or "")
        resolved = self.store.get_session_user(hash_token(session_token)) if hasattr(self.store, "get_session_user") else None
        stored = resolved[0] if resolved is not None else self.store.get_session(hash_token(session_token))
        if stored is None:
            raise AuthError("P4_AUTH_INVALID_SESSION", "Your session has expired. Sign in again.", 401)
        header_token = str(headers.get("x-csrf-token") or headers.get("X-CSRF-Token") or "")
        if not secrets.compare_digest(stored.csrf_hash, hash_token(header_token)):
            raise AuthError("P4_CSRF_REQUIRED", "A valid CSRF token is required.", 403)
        return principal

    @staticmethod
    def _client_key(email: str, client_ip: str | None) -> str:
        return f"{email}|{client_ip or 'unknown'}"

    def register(
        self,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
    ) -> AuthResult:
        self._require_double_submit(cookies, headers)
        display_name = normalize_display_name(body.get("display_name"))
        email = normalize_email(body.get("email"))
        password = validate_password(body.get("password"))
        try:
            password_hash = self.password_hasher.hash(password)
            user = self.store.create_user(email, password_hash, display_name)
            return self._new_session(user)
        except AuthStoreError as exc:
            if exc.code == "P4_AUTH_EMAIL_EXISTS":
                raise AuthError(exc.code, exc.message, 409) from exc
            raise AuthError(exc.code, exc.message, 503) from exc

    def login(
        self,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
        client_ip: str | None = None,
    ) -> AuthResult:
        self._require_double_submit(cookies, headers)
        email = normalize_email(body.get("email"))
        password = validate_password(body.get("password"))
        key = self._client_key(email, client_ip)
        retry_after = self.rate_limiter.check(key)
        if retry_after:
            raise AuthError("P4_AUTH_RATE_LIMITED", "Too many sign-in attempts. Try again later.", 429)
        try:
            user = self.store.find_user_by_email(email)
        except AuthStoreError as exc:
            raise AuthError(exc.code, exc.message, 503) from exc
        valid = False
        if user is not None:
            try:
                valid = self.password_hasher.verify(str(user.get("password_hash") or ""), password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                valid = False
        if not valid:
            self.rate_limiter.record_failure(key)
            raise AuthError("P4_AUTH_INVALID_CREDENTIALS", "Email or password is incorrect.", 401)
        self.rate_limiter.clear(key)
        try:
            return self._new_session(user)
        except AuthStoreError as exc:
            raise AuthError(exc.code, exc.message, 503) from exc

    def logout(self, *, headers: Mapping[str, str], cookies: Mapping[str, str]) -> AuthResult:
        principal = self.require_csrf(headers, cookies)
        if not self.test_mode:
            token = str(cookies.get(AUTH_SESSION_COOKIE) or "")
            try:
                self.store.delete_session(hash_token(token))
            except AuthStoreError as exc:
                raise AuthError(exc.code, exc.message, 503) from exc
        return AuthResult(
            {"authenticated": False},
            principal=principal,
            clear_session=True,
            csrf_token=None,
        )

    def me(self, headers: Mapping[str, str], cookies: Mapping[str, str]) -> dict[str, Any]:
        try:
            principal = self.resolve(headers, cookies)
        except AuthError as exc:
            if exc.code in {"P4_AUTH_REQUIRED", "P4_AUTH_INVALID_SESSION"}:
                return {"authenticated": False}
            raise
        return {
            "authenticated": True,
            "user": {
                "id": principal.user_id,
                "email": principal.email,
                "display_name": principal.display_name,
            },
        }


def cookie_attributes(*, secure: bool, http_only: bool, max_age: int | None = None) -> str:
    parts = ["Path=/", "SameSite=Lax"]
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def serialize_set_cookie(
    name: str,
    value: str,
    *,
    secure: bool,
    http_only: bool,
    max_age: int | None = None,
) -> str:
    return f"{name}={value}; {cookie_attributes(secure=secure, http_only=http_only, max_age=max_age)}"


def serialize_delete_cookie(name: str, *, secure: bool, http_only: bool) -> str:
    return serialize_set_cookie(name, "", secure=secure, http_only=http_only, max_age=0)


__all__ = [
    "AUTH_SESSION_COOKIE",
    "AUTH_SESSION_SECONDS",
    "AuthError",
    "AuthResult",
    "AuthStoreError",
    "BOOTSTRAP_EMAIL_ENV",
    "BOOTSTRAP_NAME_ENV",
    "BOOTSTRAP_PASSWORD_ENV",
    "CSRF_COOKIE",
    "DISPLAY_NAME_MAX_LENGTH",
    "DISPLAY_NAME_MIN_LENGTH",
    "FilesystemUserStore",
    "IdentityProvider",
    "MIN_PASSWORD_LENGTH",
    "NEUTRAL_DISPLAY_NAME",
    "NativeEmailPasswordProvider",
    "NeonUserStore",
    "Principal",
    "StoredSession",
    "UserStore",
    "build_user_store",
    "bootstrap_owner_account",
    "cookie_attributes",
    "hash_token",
    "normalize_email",
    "normalize_display_name",
    "serialize_delete_cookie",
    "serialize_set_cookie",
    "validate_password",
]
