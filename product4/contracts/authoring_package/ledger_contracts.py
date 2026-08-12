"""Strict common contracts for the execution-complete requirements ledger.

T04 owns only the ledger's shared envelope.  Capability-specific requirement
payloads are deliberately represented as an opaque JSON object here; typed
payloads belong to the capability-template tasks that follow this contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    model_validator,
)

LEDGER_SCHEMA_VERSION = "requirements-ledger-1.0"
REQUIREMENT_ID_PATTERN = r"^REQ-[A-Z0-9]+(?:[-_][A-Z0-9]+)*$"
DECISION_ID_PATTERN = r"^DEC-[A-Z0-9]+(?:[-_][A-Z0-9]+)*$"
LEDGER_ID_PATTERN = r"^LEDGER-[A-Z0-9]+(?:[-_][A-Z0-9]+)*$"
CONFIRMATION_ID_PATTERN = r"^CONF-[A-Z0-9]+(?:[-_][A-Z0-9]+)*$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
CAPABILITY_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
FIELD_PATH_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)*$"

_REQUIREMENT_REFERENCE_PATTERN = rf"requirement:{REQUIREMENT_ID_PATTERN[1:-1]}"
_DECISION_REFERENCE_PATTERN = rf"decision:{DECISION_ID_PATTERN[1:-1]}"
CROSS_REFERENCE_PATTERN = rf"^(?:{_REQUIREMENT_REFERENCE_PATTERN}|{_DECISION_REFERENCE_PATTERN})$"


RequirementId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=7,
        max_length=80,
        pattern=REQUIREMENT_ID_PATTERN,
    ),
]
DecisionId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=7,
        max_length=80,
        pattern=DECISION_ID_PATTERN,
    ),
]
LedgerId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=80,
        pattern=LEDGER_ID_PATTERN,
    ),
]
ConfirmationId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=6,
        max_length=80,
        pattern=CONFIRMATION_ID_PATTERN,
    ),
]
Sha256Hash: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=64, max_length=64, pattern=SHA256_PATTERN),
]
CapabilityId: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100, pattern=CAPABILITY_ID_PATTERN),
]
CrossReferenceValue: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=16, max_length=170, pattern=CROSS_REFERENCE_PATTERN),
]
RequirementRef: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=17,
        max_length=170,
        pattern=rf"^{_REQUIREMENT_REFERENCE_PATTERN}$",
    ),
]
DecisionRef: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=16,
        max_length=170,
        pattern=rf"^{_DECISION_REFERENCE_PATTERN}$",
    ),
]


class _StrictModel(BaseModel):
    """Base model shared by all object contracts in this module."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class DecisionSource(str, Enum):
    """The only sources allowed to ground a ledger value."""

    CONFIRMED_PROSE = "confirmed_prose"
    CONFIRMED_USER_DECISION = "confirmed_user_decision"
    APPROVED_VERSIONED_POLICY = "approved_versioned_policy"


# These names make the provenance vocabulary explicit to callers without
# creating a second, drift-prone source enum.
ProvenanceSource = DecisionSource
ProvenanceKind = DecisionSource


class RequirementStatus(str, Enum):
    """Lifecycle state of one requirement entry."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    UNRESOLVED = "unresolved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class DecisionStatus(str, Enum):
    """Lifecycle state of one execution decision."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class LedgerStatus(str, Enum):
    """Lifecycle state of the combined requirements ledger."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    FROZEN = "frozen"
    BLOCKED = "blocked"


class ConfirmationStatus(str, Enum):
    """Whether a user confirmation is current and usable."""

    CONFIRMED = "confirmed"
    STALE = "stale"
    REVOKED = "revoked"


class CrossReference(RootModel[CrossReferenceValue]):
    """A stable, typed string reference to a requirement or decision.

    The serialized form is ``requirement:REQ-001`` or
    ``decision:DEC-001``.  Requirement and decision objects still use their
    dedicated ID fields for ordinary lists; this type is for heterogeneous
    common references.
    """

    @property
    def kind(self) -> str:
        return self.root.split(":", 1)[0]

    @property
    def target_id(self) -> str:
        return self.root.split(":", 1)[1]


class Provenance(_StrictModel):
    """Evidence identifying where a ledger value came from.

    Capability-specific grounding rules are intentionally deferred to later
    validators.  T04 only permits the three approved source kinds and requires
    an auditable reference for each value.
    """

    source: DecisionSource
    reference: str = Field(min_length=1, max_length=500)
    quote: str | None = Field(default=None, max_length=10_000)
    source_hash: Sha256Hash | None = None
    policy_version: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def approved_policy_must_be_versioned(self) -> Provenance:
        if self.source is DecisionSource.APPROVED_VERSIONED_POLICY and not self.policy_version:
            raise ValueError("approved_versioned_policy provenance requires policy_version")
        return self


class RequirementEnvelope(_StrictModel):
    """Common wrapper around a future typed capability requirement.

    ``payload`` is intentionally opaque in T04.  No capability-specific fields
    are interpreted here, which keeps the common contract independent from the
    T05/T06 template unions.
    """

    id: RequirementId = Field(alias="requirement_id")
    capability: CapabilityId
    summary: str = Field(min_length=1, max_length=10_000)
    status: RequirementStatus = RequirementStatus.PROPOSED
    provenance: list[Provenance] = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    decision_ids: list[DecisionId] = Field(default_factory=list)
    depends_on: list[RequirementId] = Field(default_factory=list)
    cross_references: list[CrossReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_are_unique(self) -> RequirementEnvelope:
        _require_unique(self.decision_ids, "decision_ids")
        _require_unique(self.depends_on, "depends_on")
        if self.id in self.depends_on:
            raise ValueError(f"requirement {self.id} cannot depend on itself")
        return self


class Decision(_StrictModel):
    """One explicit execution decision attached to a ledger requirement."""

    id: DecisionId = Field(alias="decision_id")
    field_path: str = Field(
        min_length=1,
        max_length=300,
        pattern=FIELD_PATH_PATTERN,
    )
    value: Any
    source: DecisionSource
    status: DecisionStatus = DecisionStatus.PROPOSED
    provenance: Provenance
    requirement_ids: list[RequirementId] = Field(default_factory=list)
    cross_references: list[CrossReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def source_matches_provenance(self) -> Decision:
        if self.source is not self.provenance.source:
            raise ValueError("decision source must match provenance source")
        _require_unique(self.requirement_ids, "requirement_ids")
        return self


class Confirmation(_StrictModel):
    """User confirmation binding a ledger revision to a content hash."""

    id: ConfirmationId = Field(alias="confirmation_id")
    status: ConfirmationStatus
    confirmed_by: str = Field(min_length=1, max_length=300)
    confirmed_at: datetime
    ledger_hash: Sha256Hash
    source_hash: Sha256Hash

    @model_validator(mode="after")
    def confirmation_time_is_timezone_aware(self) -> Confirmation:
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise ValueError("confirmed_at must include a timezone")
        return self


class RequirementsLedger(_StrictModel):
    """The single combined topology/configuration requirements ledger."""

    schema_version: str = Field(pattern=rf"^{re.escape(LEDGER_SCHEMA_VERSION)}$")
    id: LedgerId = Field(alias="ledger_id")
    source_hash: Sha256Hash
    requirements: list[RequirementEnvelope] = Field(min_length=1)
    decisions: list[Decision] = Field(default_factory=list)
    status: LedgerStatus = LedgerStatus.DRAFT
    confirmation: Confirmation | None = None
    frozen_hash: Sha256Hash | None = None
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_identity_and_lifecycle(self) -> RequirementsLedger:
        requirement_ids = [item.id for item in self.requirements]
        decision_ids = [item.id for item in self.decisions]
        _require_unique(requirement_ids, "requirements IDs")
        _require_unique(decision_ids, "decision IDs")

        requirement_id_set = set(requirement_ids)
        decision_id_set = set(decision_ids)
        for requirement in self.requirements:
            missing_decisions = set(requirement.decision_ids) - decision_id_set
            if missing_decisions:
                raise ValueError(
                    f"requirement {requirement.id} references unknown decisions: "
                    f"{sorted(missing_decisions)}"
                )
            missing_requirements = set(requirement.depends_on) - requirement_id_set
            if missing_requirements:
                raise ValueError(
                    f"requirement {requirement.id} references unknown requirements: "
                    f"{sorted(missing_requirements)}"
                )
            _validate_cross_references(requirement.cross_references, requirement_id_set, decision_id_set)

        for decision in self.decisions:
            missing_requirements = set(decision.requirement_ids) - requirement_id_set
            if missing_requirements:
                raise ValueError(
                    f"decision {decision.id} references unknown requirements: "
                    f"{sorted(missing_requirements)}"
                )
            _validate_cross_references(decision.cross_references, requirement_id_set, decision_id_set)

        confirmation_status = self.confirmation.status if self.confirmation else None
        if self.status in {LedgerStatus.CONFIRMED, LedgerStatus.FROZEN} and (
            self.confirmation is None or confirmation_status is not ConfirmationStatus.CONFIRMED
        ):
            raise ValueError("confirmed or frozen ledger requires current confirmation")

        if confirmation_status is ConfirmationStatus.CONFIRMED and self.status not in {
            LedgerStatus.CONFIRMED,
            LedgerStatus.FROZEN,
        }:
            raise ValueError("current confirmation requires confirmed or frozen ledger status")

        if self.status is LedgerStatus.FROZEN:
            if self.frozen_hash is None:
                raise ValueError("frozen ledger requires frozen_hash")
            if self.confirmation is None or self.confirmation.ledger_hash != self.frozen_hash:
                raise ValueError("frozen_hash must match confirmation.ledger_hash")
            if any(item.status is not RequirementStatus.CONFIRMED for item in self.requirements):
                raise ValueError("frozen ledger requires every requirement to be confirmed")
            if any(item.status is not DecisionStatus.CONFIRMED for item in self.decisions):
                raise ValueError("frozen ledger requires every decision to be confirmed")
        elif self.frozen_hash is not None:
            raise ValueError("unfrozen ledger cannot have frozen_hash")

        return self


# Compatibility aliases keep the common vocabulary discoverable while leaving
# one canonical implementation for each contract.
Ledger = RequirementsLedger
Requirement = RequirementEnvelope
RequirementContract = RequirementEnvelope
DecisionContract = Decision
ProvenanceContract = Provenance
ConfirmationContract = Confirmation


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label} are not allowed")


def _validate_cross_references(
    references: list[CrossReference],
    requirement_ids: set[str],
    decision_ids: set[str],
) -> None:
    for reference in references:
        if reference.kind == "requirement" and reference.target_id not in requirement_ids:
            raise ValueError(f"unknown requirement cross-reference: {reference.root}")
        if reference.kind == "decision" and reference.target_id not in decision_ids:
            raise ValueError(f"unknown decision cross-reference: {reference.root}")


def _validated_ledger(value: RequirementsLedger | Mapping[str, Any] | Any) -> RequirementsLedger:
    # T07 exposes a deep immutable frozen view rather than leaking a mutable
    # Pydantic model.  Keep the common serializer/hash APIs usable with that
    # public object without importing the integration module back into T04.
    to_mutable_ledger = getattr(value, "to_mutable_ledger", None)
    if callable(to_mutable_ledger):
        value = to_mutable_ledger()
    if isinstance(value, RequirementsLedger):
        return value
    return RequirementsLedger.model_validate(value)


def canonical_ledger_payload(value: RequirementsLedger | Mapping[str, Any] | Any) -> dict[str, Any]:
    """Return lifecycle-independent content used for ledger identity hashes."""

    ledger = _validated_ledger(value)
    payload = ledger.model_dump(
        mode="json",
        exclude={"status", "confirmation", "frozen_hash"},
    )
    # Requirement and decision statuses are lifecycle metadata as well.  T07
    # may advance proposed entries to confirmed during the explicit confirm
    # operation; that transition must not alter the substantive ledger hash.
    for requirement in payload.get("requirements", []):
        requirement.pop("status", None)
        requirement["provenance"] = sorted(
            requirement.get("provenance", []),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        requirement["decision_ids"] = sorted(requirement.get("decision_ids", []))
        requirement["depends_on"] = sorted(requirement.get("depends_on", []))
        requirement["cross_references"] = sorted(
            requirement.get("cross_references", []),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    for decision in payload.get("decisions", []):
        decision.pop("status", None)
        decision["requirement_ids"] = sorted(decision.get("requirement_ids", []))
        decision["cross_references"] = sorted(
            decision.get("cross_references", []),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    payload["requirements"] = sorted(payload.get("requirements", []), key=lambda item: item["id"])
    payload["decisions"] = sorted(payload.get("decisions", []), key=lambda item: item["id"])
    return payload


def canonical_ledger_json(value: RequirementsLedger | Mapping[str, Any] | Any) -> str:
    """Serialize ledger content deterministically for hashing and comparison."""

    return json.dumps(
        canonical_ledger_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_ledger_hash(value: RequirementsLedger | Mapping[str, Any] | Any) -> str:
    """Return the SHA-256 identity hash of the ledger's substantive content."""

    return hashlib.sha256(canonical_ledger_json(value).encode("utf-8")).hexdigest()


def serialize_ledger_json(value: RequirementsLedger | Mapping[str, Any] | Any) -> str:
    """Serialize the complete ledger document deterministically."""

    ledger = _validated_ledger(value)
    return json.dumps(
        ledger.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_ledger_json(value: str | bytes | bytearray) -> RequirementsLedger:
    """Parse one complete JSON ledger document through the strict contract."""

    return RequirementsLedger.model_validate_json(value)


def canonical_schema_json() -> str:
    """Return the stable JSON Schema snapshot for the common ledger contract."""

    return json.dumps(
        RequirementsLedger.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_schema_hash() -> str:
    """Return a deterministic hash for the common ledger JSON Schema."""

    return hashlib.sha256(canonical_schema_json().encode("utf-8")).hexdigest()


__all__ = [
    "CAPABILITY_ID_PATTERN",
    "CROSS_REFERENCE_PATTERN",
    "DECISION_ID_PATTERN",
    "FIELD_PATH_PATTERN",
    "LEDGER_ID_PATTERN",
    "LEDGER_SCHEMA_VERSION",
    "SHA256_PATTERN",
    "Confirmation",
    "ConfirmationContract",
    "ConfirmationId",
    "ConfirmationStatus",
    "CrossReference",
    "CrossReferenceValue",
    "Decision",
    "DecisionContract",
    "DecisionId",
    "DecisionRef",
    "DecisionSource",
    "DecisionStatus",
    "Ledger",
    "LedgerId",
    "LedgerStatus",
    "Provenance",
    "ProvenanceContract",
    "ProvenanceKind",
    "ProvenanceSource",
    "Requirement",
    "RequirementContract",
    "RequirementEnvelope",
    "RequirementId",
    "RequirementRef",
    "RequirementStatus",
    "RequirementsLedger",
    "Sha256Hash",
    "canonical_ledger_hash",
    "canonical_ledger_json",
    "canonical_ledger_payload",
    "canonical_schema_hash",
    "canonical_schema_json",
    "parse_ledger_json",
    "serialize_ledger_json",
]


# T07 owns the combined union and lifecycle implementation in a focused
# module.  Lazy compatibility exports keep the common contract module useful
# to callers that already import shared ledger names without creating a
# T04/T07 import cycle.
_T07_PUBLIC_EXPORTS = frozenset(
    {
        "CombinedLedgerRequirementUnion",
        "CombinedRequirementsLedger",
        "ConfirmationPolicy",
        "FrozenCombinedRequirementsLedger",
        "LedgerIssue",
        "LedgerIssueCode",
        "LedgerMergeError",
        "canonical_confirmation_hash",
        "canonical_confirmed_ledger_hash",
        "canonical_confirmed_ledger_json",
        "canonical_freeze_hash",
        "confirm_ledger",
        "freeze_ledger",
        "merge_ledger_decisions",
        "merge_requirements_ledgers",
        "merge_topology_and_configuration",
        "parse_combined_ledger_json",
        "parse_combined_requirement",
        "parse_frozen_combined_ledger_json",
        "serialize_combined_ledger_json",
        "update_ledger_decisions",
    }
)
__all__.extend(sorted(_T07_PUBLIC_EXPORTS))


def __getattr__(name: str) -> Any:
    if name in _T07_PUBLIC_EXPORTS:
        from . import ledger_merge

        return getattr(ledger_merge, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
