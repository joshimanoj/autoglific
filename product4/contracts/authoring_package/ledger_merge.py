"""T07 integration for typed ledger merging, confirmation, and freezing.

T05 and T06 own their capability-specific requirement templates.  This module
owns the one combined requirement union and the lifecycle operations that sit
between clarification and package generation.  It deliberately does not
change any legacy Product 3 production path.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any, TypeAlias

from pydantic import (
    BaseModel,
    Field,
    RootModel,
    TypeAdapter,
    model_validator,
)

from .field_policy import (
    FieldClassification,
    UnknownFieldPolicyError,
    require_field_policy,
)
from .ledger_contracts import (
    Confirmation,
    ConfirmationStatus,
    Decision,
    DecisionStatus,
    LedgerStatus,
    RequirementsLedger,
    RequirementStatus,
    canonical_ledger_hash,
    canonical_ledger_json,
    serialize_ledger_json,
)
from .ledger_core_templates import CoreLedgerRequirementUnion
from .ledger_pending_templates import CompilerPendingLedgerRequirement

T07_SCHEMA_VERSION = "requirements-ledger-merge-freeze-1.0"


CombinedLedgerRequirementUnion: TypeAlias = Annotated[
    CoreLedgerRequirementUnion | CompilerPendingLedgerRequirement,
    Field(discriminator="capability"),
]

# Public aliases use the singular/plural spellings used by the implementation
# note and by downstream clarification code.
CombinedRequirementUnion: TypeAlias = CombinedLedgerRequirementUnion
LedgerRequirementUnion: TypeAlias = CombinedLedgerRequirementUnion
MergedRequirementUnion: TypeAlias = CombinedLedgerRequirementUnion


class CombinedLedgerRequirementDocument(RootModel[CombinedLedgerRequirementUnion]):
    """Root adapter for parsing one requirement from the combined union."""


class CombinedRequirementsLedger(RequirementsLedger):
    """The single typed requirements ledger consumed after T07."""

    requirements: list[CombinedLedgerRequirementUnion] = Field(min_length=1)

    @model_validator(mode="after")
    def route_references_resolve(self) -> CombinedRequirementsLedger:
        requirement_ids = {requirement.id for requirement in self.requirements}
        for requirement in self.requirements:
            route_ids = getattr(requirement.payload, "route_requirement_ids", ())
            missing = sorted({route_id for route_id in route_ids if route_id not in requirement_ids})
            if missing:
                raise ValueError(
                    f"requirement {requirement.id} references unknown route requirements: {missing}"
                )
        _validate_all_decisions(self.decisions, self.requirements)
        return self


CombinedRequirementsLedgerModel = CombinedRequirementsLedger
MergedRequirementsLedger = CombinedRequirementsLedger
CombinedLedger = CombinedRequirementsLedger

COMBINED_REQUIREMENT_ADAPTER = TypeAdapter(CombinedLedgerRequirementUnion)


class LedgerIssueCode(str, Enum):
    """Closed issue vocabulary for T07 merge and lifecycle failures."""

    CONFLICTING_DECISION = "CONFLICTING_DECISION"
    DUPLICATE_DECISION_ID = "DUPLICATE_DECISION_ID"
    CONFLICTING_REQUIREMENT = "CONFLICTING_REQUIREMENT"
    LEDGER_ID_CONFLICT = "LEDGER_ID_CONFLICT"
    SOURCE_HASH_CONFLICT = "SOURCE_HASH_CONFLICT"
    UNKNOWN_FIELD_POLICY = "UNKNOWN_FIELD_POLICY"
    AMBIGUOUS_FIELD_POLICY = "AMBIGUOUS_FIELD_POLICY"
    INVALID_DECISION_TARGET = "INVALID_DECISION_TARGET"
    LEDGER_ALREADY_FROZEN = "LEDGER_ALREADY_FROZEN"
    LEDGER_NOT_CONFIRMED = "LEDGER_NOT_CONFIRMED"
    CONFIRMATION_STALE = "CONFIRMATION_STALE"
    CONFIRMATION_HASH_MISMATCH = "CONFIRMATION_HASH_MISMATCH"
    INVALID_LIFECYCLE = "INVALID_LIFECYCLE"


@dataclass(frozen=True)
class LedgerIssue:
    """One typed, path-aware T07 issue."""

    code: LedgerIssueCode
    path: str
    message: str

    @property
    def issue_code(self) -> str:
        return self.code.value


class LedgerMergeError(ValueError):
    """Raised when a merge or lifecycle operation cannot be accepted."""

    def __init__(self, issues: Sequence[LedgerIssue] | LedgerIssue):
        normalized = (issues,) if isinstance(issues, LedgerIssue) else tuple(issues)
        if not normalized:
            raise ValueError("LedgerMergeError requires at least one issue")
        self.issues = normalized
        self.issue_codes = tuple(issue.code for issue in normalized)
        self.code = normalized[0].code
        detail = "; ".join(
            f"{issue.code.value} at {issue.path}: {issue.message}" for issue in normalized
        )
        super().__init__(detail)


class LedgerConflictError(LedgerMergeError):
    """Raised for an incompatible duplicate decision or requirement."""


class LedgerLifecycleError(LedgerMergeError):
    """Raised when confirmation or freeze lifecycle preconditions fail."""


class ConfirmationPolicy(str, Enum):
    """Policy for a configuration-only update to a reviewed ledger."""

    CONFIGURATION_PRESERVES_CONFIRMATION = "configuration_preserves_confirmation"
    REQUIRE_RECONFIRMATION = "require_reconfirmation"


ConfigurationConfirmationPolicy = ConfirmationPolicy
LedgerConfirmationPolicy = ConfirmationPolicy


def _issue(code: LedgerIssueCode, path: str, message: str) -> LedgerIssue:
    return LedgerIssue(code=code, path=path, message=message)


def _raise_merge(*issues: LedgerIssue) -> None:
    if any(issue.code in {
        LedgerIssueCode.CONFLICTING_DECISION,
        LedgerIssueCode.DUPLICATE_DECISION_ID,
        LedgerIssueCode.CONFLICTING_REQUIREMENT,
    } for issue in issues):
        raise LedgerConflictError(issues)
    raise LedgerMergeError(issues)


def _raise_lifecycle(*issues: LedgerIssue) -> None:
    raise LedgerLifecycleError(issues)


def _canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


def _as_combined_ledger(value: Any) -> CombinedRequirementsLedger:
    """Validate any supported ledger input through the combined union."""

    if isinstance(value, FrozenCombinedRequirementsLedger):
        return value.to_mutable_ledger()
    if isinstance(value, CombinedRequirementsLedger):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        return CombinedRequirementsLedger.model_validate_json(value)
    if isinstance(value, RequirementsLedger):
        return CombinedRequirementsLedger.model_validate(value.model_dump(mode="json"))
    return CombinedRequirementsLedger.model_validate(value)


def parse_combined_requirement(
    value: Mapping[str, Any] | str | bytes | bytearray,
) -> CombinedLedgerRequirementUnion:
    """Parse one requirement through the closed T05/T06 discriminator."""

    if isinstance(value, (str, bytes, bytearray)):
        return COMBINED_REQUIREMENT_ADAPTER.validate_json(value)
    return COMBINED_REQUIREMENT_ADAPTER.validate_python(value)


def parse_combined_ledger_json(value: str | bytes | bytearray) -> CombinedRequirementsLedger:
    """Parse a complete typed combined ledger."""

    return CombinedRequirementsLedger.model_validate_json(value)


def parse_frozen_combined_ledger_json(
    value: str | bytes | bytearray,
) -> FrozenCombinedRequirementsLedger:
    """Reload a frozen JSON document into the immutable public view."""

    return freeze_ledger(parse_combined_ledger_json(value))


def serialize_combined_ledger_json(value: Any) -> str:
    """Serialize a combined or frozen ledger deterministically."""

    return serialize_ledger_json(_as_combined_ledger(value))


def canonical_confirmed_ledger_json(value: Any) -> str:
    """Serialize substantive ledger content deterministically for hashing."""

    return canonical_ledger_json(_as_combined_ledger(value))


def _requirement_semantics(requirement: Any) -> dict[str, Any]:
    payload = _model_dump(requirement)
    for field_name in ("status", "provenance", "decision_ids", "cross_references"):
        payload.pop(field_name, None)
    return payload


def _merge_unique_models(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = _canonical_json(value)
        if marker not in seen:
            result.append(value)
            seen.add(marker)
    return result


def _merge_requirement(existing: Any, incoming: Any) -> Any:
    if _canonical_json(_requirement_semantics(existing)) != _canonical_json(
        _requirement_semantics(incoming)
    ):
        _raise_merge(
            _issue(
                LedgerIssueCode.CONFLICTING_REQUIREMENT,
                f"requirements.{existing.id}",
                "duplicate requirement IDs carry different typed content",
            )
        )
    existing_dump = existing.model_dump(mode="python")
    existing_dump["provenance"] = _merge_unique_models(
        [*existing.provenance, *incoming.provenance]
    )
    existing_dump["decision_ids"] = list(
        dict.fromkeys([*existing.decision_ids, *incoming.decision_ids])
    )
    existing_dump["cross_references"] = _merge_unique_models(
        [*existing.cross_references, *incoming.cross_references]
    )
    if existing.status is not incoming.status:
        # Explicit confirmation is the only lifecycle advancement accepted
        # during a merge; a proposed duplicate must not downgrade confirmed
        # content.
        if RequirementStatus.CONFIRMED in {existing.status, incoming.status}:
            existing_dump["status"] = RequirementStatus.CONFIRMED
        elif existing.status is RequirementStatus.UNRESOLVED or incoming.status is RequirementStatus.UNRESOLVED:
            existing_dump["status"] = RequirementStatus.UNRESOLVED
    return type(existing).model_validate(existing_dump)


def _decision_target(decision: Decision) -> tuple[tuple[str, ...], str]:
    return tuple(sorted(decision.requirement_ids)), decision.field_path


def _decision_value_equal(left: Decision, right: Decision) -> bool:
    return _canonical_json(left.value) == _canonical_json(right.value)


def _decision_equal(left: Decision, right: Decision) -> bool:
    return (
        _decision_target(left) == _decision_target(right)
        and _decision_value_equal(left, right)
        and left.source is right.source
        and left.status is right.status
        and _canonical_json(left.provenance) == _canonical_json(right.provenance)
        and tuple(sorted(left.cross_references, key=lambda item: item.root)) == tuple(
            sorted(right.cross_references, key=lambda item: item.root)
        )
    )


def _validate_decision_policy(
    decision: Decision,
    requirements_by_id: Mapping[str, Any],
) -> FieldClassification:
    """Resolve a decision's field policy and fail closed on ambiguity."""

    if not decision.requirement_ids:
        try:
            return require_field_policy(decision.field_path)
        except UnknownFieldPolicyError as exc:
            _raise_merge(
                _issue(LedgerIssueCode.UNKNOWN_FIELD_POLICY, f"decisions.{decision.id}.field_path", str(exc))
            )

    classifications: set[FieldClassification] = set()
    for requirement_id in decision.requirement_ids:
        requirement = requirements_by_id.get(requirement_id)
        if requirement is None:
            _raise_merge(
                _issue(
                    LedgerIssueCode.INVALID_DECISION_TARGET,
                    f"decisions.{decision.id}.requirement_ids",
                    f"unknown requirement target {requirement_id}",
                )
            )
        try:
            classifications.add(require_field_policy(requirement.capability, decision.field_path))
        except UnknownFieldPolicyError as exc:
            _raise_merge(
                _issue(LedgerIssueCode.UNKNOWN_FIELD_POLICY, f"decisions.{decision.id}.field_path", str(exc))
            )
    if len(classifications) != 1:
        _raise_merge(
            _issue(
                LedgerIssueCode.AMBIGUOUS_FIELD_POLICY,
                f"decisions.{decision.id}.field_path",
                "one decision cannot target fields with different topology policies",
            )
        )
    return next(iter(classifications))


def _validate_all_decisions(
    decisions: Sequence[Decision],
    requirements: Sequence[Any],
) -> dict[str, FieldClassification]:
    requirements_by_id = {requirement.id: requirement for requirement in requirements}
    return {
        decision.id: _validate_decision_policy(decision, requirements_by_id)
        for decision in decisions
    }


def _parse_decision(value: Decision | Mapping[str, Any]) -> Decision:
    return value if isinstance(value, Decision) else Decision.model_validate(value)


def _coerce_decision_sequence(value: Any) -> list[Decision]:
    if isinstance(value, Decision):
        return [value]
    if isinstance(value, Mapping):
        if "field_path" not in value and "id" not in value and "decision_id" not in value:
            _raise_merge(
                _issue(
                    LedgerIssueCode.INVALID_DECISION_TARGET,
                    "decisions",
                    "a decision object is required",
                )
            )
        return [_parse_decision(value)]
    if isinstance(value, (str, bytes, bytearray)):
        _raise_merge(
            _issue(
                LedgerIssueCode.INVALID_DECISION_TARGET,
                "decisions",
                "decision input must be a typed decision or sequence",
            )
        )
    if not isinstance(value, Iterable):
        _raise_merge(
            _issue(
                LedgerIssueCode.INVALID_DECISION_TARGET,
                "decisions",
                "decision input must be a typed decision or sequence",
            )
        )
    return [_parse_decision(item) for item in value]


def _merge_decisions(
    existing: Sequence[Decision],
    incoming: Sequence[Decision],
) -> list[Decision]:
    merged = list(existing)
    by_id = {decision.id: decision for decision in merged}
    for candidate in incoming:
        prior = by_id.get(candidate.id)
        if prior is not None:
            if _decision_equal(prior, candidate):
                continue
            if _decision_target(prior) == _decision_target(candidate) and not _decision_value_equal(
                prior, candidate
            ):
                _raise_merge(
                    _issue(
                        LedgerIssueCode.CONFLICTING_DECISION,
                        f"decisions.{candidate.id}",
                        "duplicate decision ID changes the value for the same field",
                    )
                )
            _raise_merge(
                _issue(
                    LedgerIssueCode.DUPLICATE_DECISION_ID,
                    f"decisions.{candidate.id}",
                    "duplicate decision ID carries incompatible metadata",
                )
            )
        for other in merged:
            if _decision_target(other) == _decision_target(candidate) and not _decision_value_equal(
                other, candidate
            ):
                _raise_merge(
                    _issue(
                        LedgerIssueCode.CONFLICTING_DECISION,
                        f"decisions.{candidate.id}",
                        "decisions for the same requirement field disagree",
                    )
                )
        merged.append(candidate)
        by_id[candidate.id] = candidate
    return sorted(merged, key=lambda decision: decision.id)


def _merge_requirements(
    existing: Sequence[Any],
    incoming: Sequence[Any],
) -> tuple[list[Any], bool]:
    by_id = {requirement.id: requirement for requirement in existing}
    added = False
    for candidate in incoming:
        prior = by_id.get(candidate.id)
        if prior is None:
            by_id[candidate.id] = candidate
            added = True
        else:
            by_id[candidate.id] = _merge_requirement(prior, candidate)
    return sorted(by_id.values(), key=lambda requirement: requirement.id), added


def _sync_requirement_decision_ids(
    requirements: Sequence[Any],
    decisions: Sequence[Decision],
) -> list[Any]:
    decisions_by_requirement: dict[str, list[str]] = {}
    for decision in decisions:
        for requirement_id in decision.requirement_ids:
            decisions_by_requirement.setdefault(requirement_id, []).append(decision.id)
    decision_ids = {decision.id for decision in decisions}
    result: list[Any] = []
    for requirement in requirements:
        current_ids = [item for item in requirement.decision_ids if item in decision_ids]
        for decision_id in decisions_by_requirement.get(requirement.id, []):
            if decision_id not in current_ids:
                current_ids.append(decision_id)
        current_ids.sort()
        if current_ids != sorted(requirement.decision_ids):
            result.append(requirement.model_copy(update={"decision_ids": current_ids}))
        else:
            result.append(requirement)
    return result


def _current_confirmation_is_valid(ledger: CombinedRequirementsLedger) -> bool:
    confirmation = ledger.confirmation
    return bool(
        ledger.status is LedgerStatus.CONFIRMED
        and confirmation is not None
        and confirmation.status is ConfirmationStatus.CONFIRMED
        and confirmation.source_hash == ledger.source_hash
        and confirmation.ledger_hash == canonical_ledger_hash(ledger)
    )


def _stale_confirmation(confirmation: Confirmation | None) -> dict[str, Any] | None:
    if confirmation is None:
        return None
    data = confirmation.model_dump(mode="json")
    data["status"] = ConfirmationStatus.STALE.value
    return data


def _rebuild_after_decision_change(
    base: CombinedRequirementsLedger,
    requirements: Sequence[Any],
    decisions: Sequence[Decision],
    *,
    topology_changed: bool,
    confirmation_policy: ConfirmationPolicy,
) -> CombinedRequirementsLedger:
    requirements = _sync_requirement_decision_ids(requirements, decisions)
    classifications = _validate_all_decisions(decisions, requirements)
    if not topology_changed:
        old_by_target = {_decision_target(item): item for item in base.decisions}
        new_by_target = {_decision_target(item): item for item in decisions}
        for target in set(old_by_target) | set(new_by_target):
            old = old_by_target.get(target)
            new = new_by_target.get(target)
            if old is None or new is None or not _decision_value_equal(old, new):
                changed_decision = new or old
                topology_changed |= (
                    classifications.get(changed_decision.id)
                    or _validate_decision_policy(
                        changed_decision,
                        {item.id: item for item in requirements},
                    )
                ) is FieldClassification.TOPOLOGY

    changed = (
        [item.id for item in base.requirements] != [item.id for item in requirements]
        or [_canonical_json(item) for item in base.decisions]
        != [_canonical_json(item) for item in decisions]
        or any(
            _canonical_json(left) != _canonical_json(right)
            for left, right in zip(base.requirements, requirements)
        )
    )
    if not changed:
        return CombinedRequirementsLedger.model_validate(base.model_dump(mode="json"))

    data = base.model_dump(mode="json")
    data["requirements"] = [item.model_dump(mode="json") for item in requirements]
    data["decisions"] = [item.model_dump(mode="json") for item in decisions]
    data["revision"] = base.revision + 1
    data["frozen_hash"] = None

    if topology_changed:
        data["status"] = LedgerStatus.DRAFT.value
        data["confirmation"] = _stale_confirmation(base.confirmation)
    elif (
        confirmation_policy is ConfirmationPolicy.CONFIGURATION_PRESERVES_CONFIRMATION
        and _current_confirmation_is_valid(base)
    ):
        data["status"] = LedgerStatus.CONFIRMED.value
        confirmation = base.confirmation.model_dump(mode="json") if base.confirmation else None
        if confirmation is not None:
            # The decision was an explicit Phase 2 answer.  The review
            # confirmation remains current under this declared policy, but its
            # canonical content binding must move with the ledger revision.
            candidate = CombinedRequirementsLedger.model_validate(
                {
                    **data,
                    "status": LedgerStatus.DRAFT.value,
                    "confirmation": None,
                }
            )
            confirmation["ledger_hash"] = canonical_ledger_hash(candidate)
            data["confirmation"] = confirmation
    else:
        data["status"] = LedgerStatus.DRAFT.value
        data["confirmation"] = _stale_confirmation(base.confirmation)

    return CombinedRequirementsLedger.model_validate(data)


def _configuration_input(
    value: Any,
) -> tuple[list[Any], list[Decision], str | None, str | None]:
    """Return requirements, decisions, ledger ID, and source hash for input."""

    if isinstance(value, FrozenCombinedRequirementsLedger):
        value = value.to_mutable_ledger()
    if isinstance(value, (RequirementsLedger, Mapping)) and not (
        isinstance(value, Mapping) and "field_path" in value
    ):
        ledger = _as_combined_ledger(value)
        return list(ledger.requirements), list(ledger.decisions), ledger.id, ledger.source_hash
    decisions = _coerce_decision_sequence(value)
    return [], decisions, None, None


def merge_topology_and_configuration(
    topology_ledger: Any,
    configuration: Any = None,
    *,
    confirmation_policy: ConfirmationPolicy | str = ConfirmationPolicy.CONFIGURATION_PRESERVES_CONFIRMATION,
) -> CombinedRequirementsLedger:
    """Merge Phase 1 requirements with Phase 2 decisions.

    Equal duplicate entries are merged idempotently.  Incompatible decisions
    or requirements fail with typed issue codes before any result is returned.
    """

    base = _as_combined_ledger(topology_ledger)
    if base.status is LedgerStatus.FROZEN:
        _raise_merge(
            _issue(
                LedgerIssueCode.LEDGER_ALREADY_FROZEN,
                "status",
                "a frozen ledger cannot be changed through public merge APIs",
            )
        )
    try:
        policy = (
            confirmation_policy
            if isinstance(confirmation_policy, ConfirmationPolicy)
            else ConfirmationPolicy(confirmation_policy)
        )
    except ValueError as exc:
        _raise_merge(
            _issue(LedgerIssueCode.INVALID_LIFECYCLE, "confirmation_policy", str(exc))
        )

    incoming_requirements, incoming_decisions, incoming_id, incoming_source_hash = _configuration_input(
        configuration
    ) if configuration is not None else ([], [], None, None)
    if incoming_id is not None and incoming_id != base.id:
        _raise_merge(
            _issue(
                LedgerIssueCode.LEDGER_ID_CONFLICT,
                "ledger_id",
                f"cannot merge {incoming_id} into {base.id}",
            )
        )
    if incoming_source_hash is not None and incoming_source_hash != base.source_hash:
        _raise_merge(
            _issue(
                LedgerIssueCode.SOURCE_HASH_CONFLICT,
                "source_hash",
                "topology and configuration phases must share the confirmed source hash",
            )
        )

    requirements, requirements_added = _merge_requirements(base.requirements, incoming_requirements)
    decisions = _merge_decisions(base.decisions, incoming_decisions)
    return _rebuild_after_decision_change(
        base,
        requirements,
        decisions,
        topology_changed=requirements_added,
        confirmation_policy=policy,
    )


def merge_requirements_ledgers(
    topology_ledger: Any,
    configuration_ledger: Any,
    *,
    confirmation_policy: ConfirmationPolicy | str = ConfirmationPolicy.CONFIGURATION_PRESERVES_CONFIRMATION,
) -> CombinedRequirementsLedger:
    """Merge two phase ledgers while retaining all typed provenance."""

    return merge_topology_and_configuration(
        topology_ledger,
        configuration_ledger,
        confirmation_policy=confirmation_policy,
    )


def merge_ledger_decisions(
    ledger: Any,
    decisions: Any,
    *,
    confirmation_policy: ConfirmationPolicy | str = ConfirmationPolicy.CONFIGURATION_PRESERVES_CONFIRMATION,
) -> CombinedRequirementsLedger:
    """Idempotently add Phase 2 decisions to a typed ledger."""

    return merge_topology_and_configuration(
        ledger,
        decisions,
        confirmation_policy=confirmation_policy,
    )


def _replace_decisions(
    base: CombinedRequirementsLedger,
    incoming: Sequence[Decision],
) -> list[Decision]:
    result = list(base.decisions)
    for candidate in incoming:
        target = _decision_target(candidate)
        result = [
            prior
            for prior in result
            if prior.id != candidate.id and _decision_target(prior) != target
        ]
        result.append(candidate)
    return sorted(result, key=lambda decision: decision.id)


def update_ledger_decisions(
    ledger: Any,
    decisions: Any,
    *,
    confirmation_policy: ConfirmationPolicy | str = ConfirmationPolicy.CONFIGURATION_PRESERVES_CONFIRMATION,
) -> CombinedRequirementsLedger:
    """Replace explicit decision targets and apply confirmation invalidation.

    This is the update API for a new user answer.  The merge API remains
    intentionally conflict-rejecting for two independent phase outputs.
    """

    base = _as_combined_ledger(ledger)
    if base.status is LedgerStatus.FROZEN:
        _raise_merge(
            _issue(
                LedgerIssueCode.LEDGER_ALREADY_FROZEN,
                "status",
                "a frozen ledger cannot be changed through public update APIs",
            )
        )
    incoming = _coerce_decision_sequence(decisions)
    if not incoming:
        return CombinedRequirementsLedger.model_validate(base.model_dump(mode="json"))
    requirements = list(base.requirements)
    final_decisions = _replace_decisions(base, incoming)
    _validate_all_decisions(final_decisions, requirements)
    topology_changed = False
    old_targets = {_decision_target(item): item for item in base.decisions}
    new_targets = {_decision_target(item): item for item in final_decisions}
    for target in set(old_targets) | set(new_targets):
        old = old_targets.get(target)
        new = new_targets.get(target)
        if old is None or new is None or not _decision_value_equal(old, new):
            policy = _validate_decision_policy(new or old, {item.id: item for item in requirements})
            topology_changed |= policy is FieldClassification.TOPOLOGY
    try:
        parsed_policy = (
            confirmation_policy
            if isinstance(confirmation_policy, ConfirmationPolicy)
            else ConfirmationPolicy(confirmation_policy)
        )
    except ValueError as exc:
        _raise_merge(_issue(LedgerIssueCode.INVALID_LIFECYCLE, "confirmation_policy", str(exc)))
    return _rebuild_after_decision_change(
        base,
        requirements,
        final_decisions,
        topology_changed=topology_changed,
        confirmation_policy=parsed_policy,
    )


replace_ledger_decision = update_ledger_decisions
apply_decision_update = update_ledger_decisions


def _coerce_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("confirmed_at must include a timezone")
    return value


def _default_confirmation_id(ledger_hash: str) -> str:
    return f"CONF-{ledger_hash[:16].upper()}"


def _confirmed_entry_models(ledger: CombinedRequirementsLedger) -> tuple[list[Any], list[Decision]]:
    requirements: list[Any] = []
    for requirement in ledger.requirements:
        if requirement.status in {
            RequirementStatus.UNRESOLVED,
            RequirementStatus.REJECTED,
            RequirementStatus.SUPERSEDED,
        }:
            _raise_lifecycle(
                _issue(
                    LedgerIssueCode.INVALID_LIFECYCLE,
                    f"requirements.{requirement.id}.status",
                    f"cannot confirm a {requirement.status.value} requirement",
                )
            )
        requirements.append(requirement.model_copy(update={"status": RequirementStatus.CONFIRMED}))
    decisions: list[Decision] = []
    for decision in ledger.decisions:
        if decision.status in {DecisionStatus.REJECTED, DecisionStatus.SUPERSEDED}:
            _raise_lifecycle(
                _issue(
                    LedgerIssueCode.INVALID_LIFECYCLE,
                    f"decisions.{decision.id}.status",
                    f"cannot confirm a {decision.status.value} decision",
                )
            )
        decisions.append(decision.model_copy(update={"status": DecisionStatus.CONFIRMED}))
    return requirements, decisions


def confirm_ledger(
    ledger: Any,
    *,
    confirmed_by: str | None = None,
    confirmation_id: str | None = None,
    confirmed_at: datetime | str | None = None,
) -> CombinedRequirementsLedger:
    """Explicitly confirm a combined ledger and bind its canonical hash."""

    base = _as_combined_ledger(ledger)
    if base.status is LedgerStatus.FROZEN:
        _raise_lifecycle(
            _issue(
                LedgerIssueCode.LEDGER_ALREADY_FROZEN,
                "status",
                "a frozen ledger cannot be reconfirmed",
            )
        )
    if confirmed_by is None:
        confirmed_by = base.confirmation.confirmed_by if base.confirmation else "user"
    requirements, decisions = _confirmed_entry_models(base)
    data = base.model_dump(mode="json")
    data["requirements"] = [item.model_dump(mode="json") for item in requirements]
    data["decisions"] = [item.model_dump(mode="json") for item in decisions]
    data["status"] = LedgerStatus.DRAFT.value
    data["confirmation"] = None
    data["frozen_hash"] = None
    candidate = CombinedRequirementsLedger.model_validate(data)
    ledger_hash = canonical_ledger_hash(candidate)
    confirmation = Confirmation(
        id=confirmation_id or _default_confirmation_id(ledger_hash),
        status=ConfirmationStatus.CONFIRMED,
        confirmed_by=confirmed_by,
        confirmed_at=_coerce_datetime(confirmed_at),
        ledger_hash=ledger_hash,
        source_hash=candidate.source_hash,
    )
    return CombinedRequirementsLedger.model_validate(
        {
            **data,
            "status": LedgerStatus.CONFIRMED.value,
            "confirmation": confirmation.model_dump(mode="json"),
        }
    )


def freeze_ledger(ledger: Any) -> FrozenCombinedRequirementsLedger:
    """Freeze a currently confirmed ledger and return a deep immutable view."""

    if isinstance(ledger, FrozenCombinedRequirementsLedger):
        return ledger
    base = _as_combined_ledger(ledger)
    if base.status is LedgerStatus.FROZEN:
        ledger_hash = canonical_ledger_hash(base)
        if (
            base.confirmation is None
            or base.confirmation.status is not ConfirmationStatus.CONFIRMED
            or base.confirmation.ledger_hash != ledger_hash
            or base.confirmation.source_hash != base.source_hash
            or base.frozen_hash != ledger_hash
        ):
            _raise_lifecycle(
                _issue(
                    LedgerIssueCode.CONFIRMATION_HASH_MISMATCH,
                    "frozen_hash",
                    "frozen ledger is not bound to its canonical current hash",
                )
            )
        return FrozenCombinedRequirementsLedger(base)
    if base.status is not LedgerStatus.CONFIRMED or base.confirmation is None:
        _raise_lifecycle(
            _issue(
                LedgerIssueCode.LEDGER_NOT_CONFIRMED,
                "status",
                "freeze requires a confirmed ledger and current confirmation",
            )
        )
    _validate_all_decisions(base.decisions, base.requirements)
    ledger_hash = canonical_ledger_hash(base)
    if base.confirmation.status is not ConfirmationStatus.CONFIRMED:
        _raise_lifecycle(
            _issue(
                LedgerIssueCode.CONFIRMATION_STALE,
                "confirmation.status",
                "freeze requires a current confirmation",
            )
        )
    if base.confirmation.ledger_hash != ledger_hash or base.confirmation.source_hash != base.source_hash:
        _raise_lifecycle(
            _issue(
                LedgerIssueCode.CONFIRMATION_HASH_MISMATCH,
                "confirmation.ledger_hash",
                "confirmation is not bound to the canonical current ledger",
            )
        )
    if any(item.status is not RequirementStatus.CONFIRMED for item in base.requirements):
        _raise_lifecycle(
            _issue(
                LedgerIssueCode.LEDGER_NOT_CONFIRMED,
                "requirements",
                "every requirement must be confirmed before freeze",
            )
        )
    if any(item.status is not DecisionStatus.CONFIRMED for item in base.decisions):
        _raise_lifecycle(
            _issue(
                LedgerIssueCode.LEDGER_NOT_CONFIRMED,
                "decisions",
                "every decision must be confirmed before freeze",
            )
        )
    data = base.model_dump(mode="json")
    data["status"] = LedgerStatus.FROZEN.value
    data["frozen_hash"] = ledger_hash
    frozen = CombinedRequirementsLedger.model_validate(data)
    return FrozenCombinedRequirementsLedger(frozen)


def canonical_confirmed_ledger_hash(value: Any) -> str:
    """Hash the substantive typed ledger, independent of lifecycle metadata."""

    return canonical_ledger_hash(_as_combined_ledger(value))


canonical_confirmation_hash = canonical_confirmed_ledger_hash
canonical_freeze_hash = canonical_confirmed_ledger_hash


# Compatibility names keep the lifecycle boundary discoverable without adding
# a second implementation or a second set of contracts.
merge_ledgers = merge_requirements_ledgers
merge_ledger = merge_topology_and_configuration
combine_ledgers = merge_topology_and_configuration
confirm_requirements_ledger = confirm_ledger
freeze_requirements_ledger = freeze_ledger


def _freeze_public_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _FrozenModelView(value)
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_public_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_public_value(item) for item in value)
    return value


class _FrozenModelView:
    """Read-only recursive view of a Pydantic model."""

    __slots__ = ("_model",)

    def __init__(self, model: BaseModel):
        object.__setattr__(self, "_model", model)

    def __getattr__(self, name: str) -> Any:
        return _freeze_public_value(getattr(self._model, name))

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("frozen ledger contents are immutable")

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(self._model.model_dump(*args, **kwargs))

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        return self._model.model_dump_json(*args, **kwargs)

    def __repr__(self) -> str:
        return f"FrozenModelView({self._model!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _FrozenModelView):
            return self._model == other._model
        return self._model == other


class FrozenCombinedRequirementsLedger:
    """Deep immutable public representation of a frozen combined ledger."""

    __slots__ = ("_ledger",)

    def __init__(self, ledger: CombinedRequirementsLedger):
        if ledger.status is not LedgerStatus.FROZEN:
            raise ValueError("FrozenCombinedRequirementsLedger requires status=frozen")
        object.__setattr__(self, "_ledger", ledger.model_copy(deep=True))

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("frozen ledger is immutable")

    def __getattr__(self, name: str) -> Any:
        return _freeze_public_value(getattr(self._ledger, name))

    def to_mutable_ledger(self) -> CombinedRequirementsLedger:
        """Return a detached mutable copy for serialization or inspection."""

        return self._ledger.model_copy(deep=True)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(self._ledger.model_dump(*args, **kwargs))

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        return self._ledger.model_dump_json(*args, **kwargs)

    def __repr__(self) -> str:
        return f"FrozenCombinedRequirementsLedger({self._ledger!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenCombinedRequirementsLedger):
            return self._ledger == other._ledger
        if isinstance(other, CombinedRequirementsLedger):
            return self._ledger == other
        return False


FrozenLedger = FrozenCombinedRequirementsLedger
ImmutableCombinedRequirementsLedger = FrozenCombinedRequirementsLedger


__all__ = [
    "COMBINED_REQUIREMENT_ADAPTER",
    "T07_SCHEMA_VERSION",
    "CombinedLedger",
    "CombinedLedgerRequirementDocument",
    "CombinedLedgerRequirementUnion",
    "CombinedRequirementUnion",
    "CombinedRequirementsLedger",
    "CombinedRequirementsLedgerModel",
    "ConfigurationConfirmationPolicy",
    "ConfirmationPolicy",
    "FrozenCombinedRequirementsLedger",
    "FrozenLedger",
    "ImmutableCombinedRequirementsLedger",
    "LedgerConfirmationPolicy",
    "LedgerConflictError",
    "LedgerIssue",
    "LedgerIssueCode",
    "LedgerLifecycleError",
    "LedgerMergeError",
    "LedgerRequirementUnion",
    "MergedRequirementUnion",
    "MergedRequirementsLedger",
    "apply_decision_update",
    "canonical_confirmation_hash",
    "canonical_confirmed_ledger_hash",
    "canonical_confirmed_ledger_json",
    "canonical_freeze_hash",
    "combine_ledgers",
    "confirm_ledger",
    "confirm_requirements_ledger",
    "freeze_ledger",
    "freeze_requirements_ledger",
    "merge_ledger",
    "merge_ledger_decisions",
    "merge_ledgers",
    "merge_requirements_ledgers",
    "merge_topology_and_configuration",
    "parse_combined_ledger_json",
    "parse_combined_requirement",
    "parse_frozen_combined_ledger_json",
    "replace_ledger_decision",
    "serialize_combined_ledger_json",
    "update_ledger_decisions",
]
