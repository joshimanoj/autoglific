from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from product4.contracts.package_boundary import (
    canonical_authoring_package_hash,
    validate_frozen_package,
)
from product4.contracts.session import AuthoringSession, RevisionRecord, SessionState

from .package_builder import build_frozen_package


class ConfirmationMismatchError(ValueError):
    code = "P4_CONFIRMATION_HASH_MISMATCH"


Clock = Callable[[], datetime]


def prepare_confirmation(
    session: AuthoringSession,
    confirmed_by: str = "user",
    *,
    clock: Clock | None = None,
) -> tuple[dict, str]:
    if session.state is not SessionState.READY_FOR_REVIEW:
        raise ValueError("P4_NOT_READY_FOR_REVIEW")
    if any(
        record.source == "simulated_user_evaluation_decision"
        for record in session.answer_records
    ):
        raise ValueError("P4_SIMULATED_USER_CANNOT_CONFIRM_PRODUCTION")
    confirmed_at = (clock or (lambda: datetime.now(timezone.utc)))()
    if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
        raise ValueError("P4_CONFIRMATION_CLOCK_MUST_BE_TIMEZONE_AWARE")
    package = build_frozen_package(
        session,
        confirmed_by,
        confirmed_at=confirmed_at,
    )
    return package, canonical_authoring_package_hash(package)


def freeze(
    session: AuthoringSession,
    confirmation_hash: str,
    prepared_package: dict,
) -> AuthoringSession:
    if session.state is not SessionState.READY_FOR_REVIEW:
        raise ValueError("P4_NOT_READY_FOR_REVIEW")
    package = validate_frozen_package(prepared_package)
    expected = canonical_authoring_package_hash(package)
    if confirmation_hash != expected:
        raise ConfirmationMismatchError(f"{ConfirmationMismatchError.code}: expected {expected}")
    revision = session.revision + 1
    revisions = [
        *session.revisions,
        RevisionRecord(
            revision=revision,
            parent_revision=session.revision,
            operation="freeze",
            canonical_hash=expected,
        ),
    ]
    return AuthoringSession.model_validate({
        **session.model_dump(mode="json"),
        "revision": revision,
        "revisions": [item.model_dump(mode="json") for item in revisions],
        "state": SessionState.FROZEN.value,
        "frozen_package": package.model_dump(mode="json"),
        "frozen_hash": expected,
    })
