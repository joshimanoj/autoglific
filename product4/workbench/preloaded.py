"""The canonical shared demo flow shipped with the Product 4 runtime."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from product4.contracts.package_boundary import (
    canonical_authoring_package_hash,
    validate_frozen_package,
)
from product4.contracts.session import AuthoringSession, SessionState
from product4.workbench.pipeline import run_pipeline


SHARED_DEMO_ID = "flow-msqh0ezo-pot4f"
SHARED_DEMO_TITLE = "Sakhi NGO MCH Demo"
PRELOADED_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "preloaded" / "sakhi-ngo-mch-demo.json"
)
PUBLICATION_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "preloaded"
    / "sakhi-ngo-mch-demo-publication.json"
)
SHARED_DEMO_REVISION = 36
SHARED_DEMO_FROZEN_HASH = "4b332b2bcf40cb48731f5a8c4febc5893ea4a464fbe37557838b5b520f428a11"
SHARED_DEMO_ARTIFACT_HASH = "37f081434063f644a758e4034f907b499beeb117aa552b5ad89ed486320a632b"
SHARED_DEMO_FLOW_ID = "41600"
SHARED_DEMO_FLOW_UUID = "5244a47c-49df-5d65-b045-cc884834376e"
PUBLICATION_SCHEMA_VERSION = "product4-workbench-glific-publish-1.0"


class PreloadedPublicationError(ValueError):
    """The shipped public publication binding failed closed validation."""


def _load_fixture(path: Path) -> AuthoringSession:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("P4_PRELOADED_DEMO_FIXTURE_INVALID") from exc
    session = AuthoringSession.model_validate(raw)
    if session.id != SHARED_DEMO_ID or session.title != SHARED_DEMO_TITLE:
        raise ValueError("P4_PRELOADED_DEMO_IDENTITY_INVALID")
    if session.state is not SessionState.FROZEN:
        raise ValueError("P4_PRELOADED_DEMO_NOT_FROZEN")
    if session.frozen_package is None or session.frozen_hash is None:
        raise ValueError("P4_PRELOADED_DEMO_PACKAGE_MISSING")
    package = validate_frozen_package(session.frozen_package)
    if canonical_authoring_package_hash(package) != session.frozen_hash:
        raise ValueError("P4_PRELOADED_DEMO_HASH_MISMATCH")
    return session


def _load_publication_fixture(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "session_id",
        "session_revision",
        "frozen_package_hash",
        "artifact_hash",
        "result",
    }:
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID")
    result = raw.get("result")
    if not isinstance(result, dict) or set(result) != {
        "flow_id",
        "flow_uuid",
        "flow_name",
        "status",
    }:
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID")
    if (
        raw.get("schema_version") != PUBLICATION_SCHEMA_VERSION
        or raw.get("session_id") != SHARED_DEMO_ID
        or raw.get("session_revision") != SHARED_DEMO_REVISION
        or raw.get("frozen_package_hash") != SHARED_DEMO_FROZEN_HASH
        or raw.get("artifact_hash") != SHARED_DEMO_ARTIFACT_HASH
        or result != {
            "flow_id": SHARED_DEMO_FLOW_ID,
            "flow_uuid": SHARED_DEMO_FLOW_UUID,
            "flow_name": SHARED_DEMO_TITLE,
            "status": "published",
        }
    ):
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID")
    return raw


def load_preloaded_publication(session: AuthoringSession) -> dict[str, object]:
    """Build and validate the exact public publication binding for the demo."""

    if (
        session.id != SHARED_DEMO_ID
        or session.title != SHARED_DEMO_TITLE
        or session.revision != SHARED_DEMO_REVISION
        or session.frozen_hash != SHARED_DEMO_FROZEN_HASH
    ):
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID")
    try:
        pipeline = run_pipeline(session)
    except Exception as exc:  # noqa: BLE001 - public fixture must fail closed
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID") from exc
    stages = pipeline.get("stages")
    if (
        pipeline.get("session_id") != session.id
        or pipeline.get("session_revision") != session.revision
        or pipeline.get("frozen_package_hash") != session.frozen_hash
        or pipeline.get("all_stages_passed") is not True
        or not isinstance(stages, list)
        or [stage.get("name") for stage in stages if isinstance(stage, dict)]
        != [
            "frozen_package",
            "engine1_graph",
            "engine2_flow_spec",
            "engine3_glific_artifact",
        ]
        or any(
            not isinstance(stage, dict) or stage.get("status") != "passed"
            for stage in stages
        )
    ):
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID")
    engine3 = stages[-1]
    if engine3.get("canonical_hash") != SHARED_DEMO_ARTIFACT_HASH:
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID")
    publication = _load_publication_fixture(PUBLICATION_FIXTURE_PATH)
    if (
        publication.get("session_id") != pipeline.get("session_id")
        or publication.get("session_revision") != pipeline.get("session_revision")
        or publication.get("frozen_package_hash") != pipeline.get("frozen_package_hash")
        or publication.get("artifact_hash") != engine3.get("canonical_hash")
    ):
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID")
    result = publication["result"]
    if not isinstance(result, dict):  # pragma: no cover - guarded above
        raise PreloadedPublicationError("P4_PRELOADED_PUBLICATION_INVALID")
    return {
        "pipeline": pipeline,
        "glific_publish": dict(result),
    }


@lru_cache(maxsize=1)
def load_preloaded_sessions() -> dict[str, AuthoringSession]:
    """Load exactly the tracked immutable demo fixture."""

    session = _load_fixture(PRELOADED_FIXTURE_PATH)
    return {session.id: session}


def get_preloaded_session(session_id: str) -> AuthoringSession | None:
    session = load_preloaded_sessions().get(session_id)
    return session.model_copy(deep=True) if session is not None else None


__all__ = [
    "PRELOADED_FIXTURE_PATH",
    "PUBLICATION_FIXTURE_PATH",
    "PUBLICATION_SCHEMA_VERSION",
    "PreloadedPublicationError",
    "SHARED_DEMO_ARTIFACT_HASH",
    "SHARED_DEMO_ID",
    "SHARED_DEMO_FLOW_ID",
    "SHARED_DEMO_FLOW_UUID",
    "SHARED_DEMO_FROZEN_HASH",
    "SHARED_DEMO_REVISION",
    "SHARED_DEMO_TITLE",
    "get_preloaded_session",
    "load_preloaded_publication",
    "load_preloaded_sessions",
]
