"""Encrypted per-user provider credentials.

Only ciphertext is handed to the durable user store.  Plaintext values exist
only in the request handler/runtime call that needs them and are never part of
an API response.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from product4.workbench.auth import (
    AuthStoreError,
    BOOTSTRAP_EMAIL_ENV,
    UserStore,
    normalize_email,
)
from product4.workbench.glific_client import GlificClientError, _normalize_base_url


FIXED_MODEL = "gpt-5.4"
ENCRYPTION_KEY_ENV = "PRODUCT4_CREDENTIAL_ENCRYPTION_KEY"
BOOTSTRAP_OPENAI_ENV = "PRODUCT4_BOOTSTRAP_OPENAI_API_KEY"
BOOTSTRAP_OPENAI_PROJECT_ID_ENV = "PRODUCT4_BOOTSTRAP_OPENAI_PROJECT_ID"
BOOTSTRAP_GLIFIC_URL_ENV = "PRODUCT4_BOOTSTRAP_GLIFIC_BASE_URL"
BOOTSTRAP_GLIFIC_PHONE_ENV = "PRODUCT4_BOOTSTRAP_GLIFIC_PHONE"
BOOTSTRAP_GLIFIC_PASSWORD_ENV = "PRODUCT4_BOOTSTRAP_GLIFIC_PASSWORD"
BOOTSTRAP_CREDENTIALS_ROTATE_ENV = "PRODUCT4_BOOTSTRAP_CREDENTIALS_ROTATE"
BOOTSTRAP_OPENAI_ROTATE_ENV = "PRODUCT4_BOOTSTRAP_OPENAI_ROTATE"
LOCAL_ENCRYPTION_KEY_FILENAME = "credential-encryption.key"
_MAX_OPENAI_KEY = 512
_MAX_OPENAI_PROJECT_ID = 128
_MAX_GLIFIC_URL = 2048
_MAX_MOBILE = 64
_MAX_GLIFIC_PASSWORD = 512
_MOBILE_RE = re.compile(r"^[+0-9()\-\s]{3,64}$")
_OPENAI_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class CredentialError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _local_encryption_key(path: Path) -> bytes:
    """Load or create a private local Fernet key with restrictive permissions."""

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    if path.is_symlink():
        raise CredentialError(
            "P4_CREDENTIAL_ENCRYPTION_KEY_INVALID",
            "The local credential encryption key is invalid.",
            503,
        )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            key = path.read_bytes().strip()
            os.chmod(path, 0o600)
        except OSError as exc:
            raise CredentialError(
                "P4_CREDENTIAL_ENCRYPTION_KEY_INVALID",
                "The local credential encryption key is invalid.",
                503,
            ) from exc
        return key
    try:
        key = Fernet.generate_key()
        os.write(descriptor, key)
        os.fsync(descriptor)
        os.chmod(path, 0o600)
        return key
    except OSError as exc:
        raise CredentialError(
            "P4_CREDENTIAL_ENCRYPTION_KEY_INVALID",
            "The local credential encryption key is invalid.",
            503,
        ) from exc
    finally:
        os.close(descriptor)


def _mask_secret(value: str | None) -> str:
    return "*****" if value else "Not configured"


def _mask_mobile(value: str | None) -> str:
    if not value:
        return "Not configured"
    compact = "".join(character for character in value if character.isdigit())
    suffix = compact[-4:] if len(compact) >= 4 else compact
    return f"••••{suffix}" if suffix else "••••"


def _validate_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise CredentialError(f"P4_{field.upper()}_INVALID", f"{field} must be text.")
    value = value.strip()
    if not value or len(value) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CredentialError(
            f"P4_{field.upper()}_INVALID",
            f"{field} must be 1-{maximum} characters.",
        )
    return value


class CredentialVault:
    """Fernet-encrypted user credential payloads with explicit update semantics."""

    FIELDS = (
        "openai_api_key",
        "openai_project_id",
        "glific_base_url",
        "mobile_number",
        "glific_password",
    )

    def __init__(
        self,
        store: UserStore,
        *,
        encryption_key: str | bytes | None = None,
        test_mode: bool = False,
    ):
        self.store = store
        self._cipher: Fernet | None = None
        if encryption_key:
            try:
                self._cipher = Fernet(
                    encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key
                )
            except (TypeError, ValueError) as exc:
                raise CredentialError(
                    "P4_CREDENTIAL_ENCRYPTION_KEY_INVALID",
                    "The credential encryption key is invalid.",
                    503,
                ) from exc
        elif test_mode:
            self._cipher = Fernet(Fernet.generate_key())

    @classmethod
    def from_environment(
        cls,
        store: UserStore,
        *,
        test_mode: bool = False,
        local_key_path: Path | None = None,
        allow_local_key: bool = False,
    ) -> CredentialVault:
        configured_key = os.environ.get(ENCRYPTION_KEY_ENV, "").strip() or None
        if not configured_key and allow_local_key and not test_mode:
            configured_key = _local_encryption_key(
                local_key_path
                or Path(".workbench-data") / LOCAL_ENCRYPTION_KEY_FILENAME
            ).decode("ascii")
        return cls(
            store,
            encryption_key=configured_key,
            test_mode=test_mode,
        )

    def _require_cipher(self) -> Fernet:
        if self._cipher is None:
            raise CredentialError(
                "P4_CREDENTIAL_ENCRYPTION_KEY_MISSING",
                "Credential encryption is not configured on the server.",
                503,
            )
        return self._cipher

    @staticmethod
    def _validated_url(value: Any) -> str:
        raw = _validate_text(value, field="glific_base_url", maximum=_MAX_GLIFIC_URL)
        try:
            normalized, _ = _normalize_base_url(raw)
        except GlificClientError as exc:
            raise CredentialError("P4_GLIFIC_BASE_URL_INVALID", exc.message, 400) from exc
        return normalized

    @staticmethod
    def _validated_mobile(value: Any) -> str:
        mobile = _validate_text(value, field="mobile_number", maximum=_MAX_MOBILE)
        if not _MOBILE_RE.fullmatch(mobile) or sum(character.isdigit() for character in mobile) < 3:
            raise CredentialError(
                "P4_MOBILE_NUMBER_INVALID",
                "Mobile number must contain at least three digits.",
                400,
            )
        return mobile

    @staticmethod
    def _validated_api_key(value: Any) -> str:
        return _validate_text(value, field="openai_api_key", maximum=_MAX_OPENAI_KEY)

    @staticmethod
    def _validated_project_id(value: Any) -> str:
        project_id = _validate_text(
            value,
            field="openai_project_id",
            maximum=_MAX_OPENAI_PROJECT_ID,
        )
        if not _OPENAI_PROJECT_ID_RE.fullmatch(project_id):
            raise CredentialError(
                "P4_OPENAI_PROJECT_ID_INVALID",
                "OpenAI project ID contains invalid characters.",
                400,
            )
        return project_id

    @staticmethod
    def _validated_password(value: Any) -> str:
        return _validate_text(value, field="glific_password", maximum=_MAX_GLIFIC_PASSWORD)

    def _decrypt(self, user_id: str) -> dict[str, str]:
        cipher = self._require_cipher()
        ciphertext = self.store.load_credentials(user_id)
        if not ciphertext:
            return {}
        try:
            raw = cipher.decrypt(ciphertext.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CredentialError(
                "P4_CREDENTIALS_UNAVAILABLE",
                "Saved credentials could not be opened.",
                503,
            ) from exc
        if not isinstance(payload, dict) or any(
            key not in self.FIELDS or not isinstance(value, str)
            for key, value in payload.items()
        ):
            raise CredentialError("P4_CREDENTIALS_UNAVAILABLE", "Saved credentials are invalid.", 503)
        if "openai_project_id" in payload:
            try:
                self._validated_project_id(payload["openai_project_id"])
            except CredentialError as exc:
                raise CredentialError(
                    "P4_CREDENTIALS_UNAVAILABLE",
                    "Saved credentials are invalid.",
                    503,
                ) from exc
        return {str(key): str(value) for key, value in payload.items()}

    def _encrypt(self, values: Mapping[str, str]) -> str:
        import json

        cipher = self._require_cipher()
        return cipher.encrypt(
            json.dumps(dict(values), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

    def update(self, user_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        allowed = set(self.FIELDS) | {f"clear_{field}" for field in self.FIELDS}
        unknown = set(body) - allowed
        if unknown:
            raise CredentialError("P4_SETTINGS_FIELD_INVALID", "Settings contain an unknown field.", 400)
        values = self._decrypt(user_id)
        for field in self.FIELDS:
            clear = body.get(f"clear_{field}")
            if clear is not None and not isinstance(clear, bool):
                raise CredentialError("P4_SETTINGS_FIELD_INVALID", "Clear flags must be true or false.", 400)
            if clear is True:
                values.pop(field, None)
                continue
            if field not in body:
                continue
            candidate = body.get(field)
            # Blank/omitted secret inputs intentionally preserve the existing
            # value.  Clearing is explicit through clear_<field>=true.
            if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
                continue
            if field == "openai_api_key":
                values[field] = self._validated_api_key(candidate)
            elif field == "openai_project_id":
                values[field] = self._validated_project_id(candidate)
            elif field == "glific_base_url":
                values[field] = self._validated_url(candidate)
            elif field == "mobile_number":
                values[field] = self._validated_mobile(candidate)
            elif field == "glific_password":
                values[field] = self._validated_password(candidate)
        self.store.save_credentials(user_id, self._encrypt(values))
        return self.status(user_id)

    def values(self, user_id: str) -> dict[str, str]:
        return self._decrypt(user_id)

    def status(self, user_id: str) -> dict[str, Any]:
        values = self._decrypt(user_id)
        return {
            "model": FIXED_MODEL,
            "authoring_configured": bool(values.get("openai_api_key")),
            "glific_configured": all(
                values.get(field)
                for field in ("glific_base_url", "mobile_number", "glific_password")
            ),
            "openai_api_key": {
                "configured": bool(values.get("openai_api_key")),
                "masked": _mask_secret(values.get("openai_api_key")),
            },
            "openai_project_id": values.get("openai_project_id"),
            "glific_url": values.get("glific_base_url"),
            "mobile_number": {
                "configured": bool(values.get("mobile_number")),
                "masked": _mask_mobile(values.get("mobile_number")),
            },
            "password": {
                "configured": bool(values.get("glific_password")),
                "masked": _mask_secret(values.get("glific_password")),
            },
        }


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _bootstrap_credential_values(*, hosted: bool) -> dict[str, str]:
    explicit_key = os.environ.get(BOOTSTRAP_OPENAI_ENV, "").strip()
    explicit_project = os.environ.get(BOOTSTRAP_OPENAI_PROJECT_ID_ENV, "").strip()
    selected: dict[str, str] = {}

    # A project ID is paired only with the key source that supplied it. In
    # particular, never combine an ambient OPENAI_API_KEY with a project ID
    # from another environment file.
    if explicit_key:
        selected["openai_api_key"] = explicit_key
        if explicit_project:
            selected["openai_project_id"] = explicit_project
    elif not hosted:
        legacy_key = os.environ.get("LLM_API_KEY", "").strip()
        if legacy_key:
            selected["openai_api_key"] = legacy_key
            legacy_project = os.environ.get("LLM_PROJECT_ID", "").strip()
            if legacy_project:
                selected["openai_project_id"] = legacy_project
        else:
            ambient_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if ambient_key:
                selected["openai_api_key"] = ambient_key

    if hosted:
        glific_values = {
            "glific_base_url": os.environ.get(BOOTSTRAP_GLIFIC_URL_ENV, "").strip(),
            "mobile_number": os.environ.get(BOOTSTRAP_GLIFIC_PHONE_ENV, "").strip(),
            "glific_password": os.environ.get(BOOTSTRAP_GLIFIC_PASSWORD_ENV, "").strip(),
        }
    else:
        glific_values = {
            "glific_base_url": (
                os.environ.get(BOOTSTRAP_GLIFIC_URL_ENV, "").strip()
                or os.environ.get("GLIFIC_PRODUCTION_BASE_URL", "").strip()
                or os.environ.get("GLIFIC_BASE_URL", "").strip()
            ),
            "mobile_number": (
                os.environ.get(BOOTSTRAP_GLIFIC_PHONE_ENV, "").strip()
                or os.environ.get("GLIFIC_PHONE", "").strip()
            ),
            "glific_password": (
                os.environ.get(BOOTSTRAP_GLIFIC_PASSWORD_ENV, "").strip()
                or os.environ.get("GLIFIC_PASSWORD", "").strip()
            ),
        }
    selected.update({key: value for key, value in glific_values.items() if value})
    return selected


def seed_bootstrap_credentials(
    store: UserStore,
    vault: CredentialVault,
    *,
    email: Any | None = None,
    hosted: bool = False,
    rotate: bool | None = None,
    rotate_openai: bool | None = None,
) -> bool:
    """Seed only the configured bootstrap owner's missing credential fields."""

    configured_email = os.environ.get(BOOTSTRAP_EMAIL_ENV, "") if email is None else email
    if not configured_email:
        return False
    normalized_email = normalize_email(configured_email)
    user = store.find_user_by_email(normalized_email)
    if user is None:
        return False
    should_rotate = _truthy(os.environ.get(BOOTSTRAP_CREDENTIALS_ROTATE_ENV)) if rotate is None else rotate
    should_rotate_openai = (
        _truthy(os.environ.get(BOOTSTRAP_OPENAI_ROTATE_ENV))
        if rotate_openai is None
        else rotate_openai
    ) or should_rotate
    candidates = _bootstrap_credential_values(hosted=hosted)
    if not candidates:
        return False
    user_id = str(user.get("user_id") or "")
    if not user_id:
        raise CredentialError(
            "P4_CREDENTIALS_UNAVAILABLE",
            "The bootstrap account credentials could not be identified.",
            503,
        )
    try:
        existing = vault.values(user_id)
        updates: dict[str, Any] = {
            field: value
            for field, value in candidates.items()
            if (
                should_rotate
                or (should_rotate_openai and field in {"openai_api_key", "openai_project_id"})
                or not existing.get(field)
            )
        }
        if (
            should_rotate_openai
            and "openai_api_key" in candidates
            and "openai_project_id" not in candidates
            and existing.get("openai_project_id")
        ):
            updates["clear_openai_project_id"] = True
        if updates:
            vault.update(user_id, updates)
            store.mark_bootstrap_credentials_seeded(user_id)
        elif user.get("bootstrap_credentials_seeded") is not True:
            store.mark_bootstrap_credentials_seeded(user_id)
    except AuthStoreError as exc:
        raise CredentialError(
            "P4_CREDENTIALS_UNAVAILABLE",
            "Bootstrap credentials could not be saved.",
            503,
        ) from exc
    return bool(updates)


__all__ = [
    "BOOTSTRAP_CREDENTIALS_ROTATE_ENV",
    "BOOTSTRAP_GLIFIC_PASSWORD_ENV",
    "BOOTSTRAP_GLIFIC_PHONE_ENV",
    "BOOTSTRAP_GLIFIC_URL_ENV",
    "BOOTSTRAP_OPENAI_ENV",
    "BOOTSTRAP_OPENAI_ROTATE_ENV",
    "BOOTSTRAP_OPENAI_PROJECT_ID_ENV",
    "CredentialError",
    "CredentialVault",
    "ENCRYPTION_KEY_ENV",
    "FIXED_MODEL",
    "LOCAL_ENCRYPTION_KEY_FILENAME",
    "seed_bootstrap_credentials",
]
