"""Versioned capability inventory and retained-corpus coverage checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

CAPABILITY_PROFILE_VERSION = "authoring-capability-profile-1.0"
MINIMUM_CORPUS_COVERAGE = 0.80


class CapabilityRegistryError(ValueError):
    """Raised when a capability definition or coverage report is invalid."""


class CapabilityStatus(str, Enum):
    ENABLED = "enabled"
    PENDING = "pending"
    STRUCTURAL_ONLY = "structural_only"
    INTERACTION_MODIFIER = "interaction_modifier"
    DISABLED = "disabled"


_ALLOWED_STATUS_TRANSITIONS: dict[CapabilityStatus, frozenset[CapabilityStatus]] = {
    CapabilityStatus.DISABLED: frozenset(
        {
            CapabilityStatus.DISABLED,
            CapabilityStatus.PENDING,
            CapabilityStatus.STRUCTURAL_ONLY,
            CapabilityStatus.INTERACTION_MODIFIER,
        }
    ),
    CapabilityStatus.PENDING: frozenset(
        {CapabilityStatus.PENDING, CapabilityStatus.ENABLED, CapabilityStatus.DISABLED}
    ),
    CapabilityStatus.STRUCTURAL_ONLY: frozenset(
        {CapabilityStatus.STRUCTURAL_ONLY, CapabilityStatus.ENABLED, CapabilityStatus.DISABLED}
    ),
    CapabilityStatus.INTERACTION_MODIFIER: frozenset(
        {
            CapabilityStatus.INTERACTION_MODIFIER,
            CapabilityStatus.ENABLED,
            CapabilityStatus.DISABLED,
        }
    ),
    CapabilityStatus.ENABLED: frozenset({CapabilityStatus.ENABLED, CapabilityStatus.DISABLED}),
}


def _coerce_status(value: CapabilityStatus | str) -> CapabilityStatus:
    try:
        return value if isinstance(value, CapabilityStatus) else CapabilityStatus(value)
    except ValueError as exc:
        raise CapabilityRegistryError(f"unknown capability status: {value!r}") from exc


def is_valid_status_transition(
    previous: CapabilityStatus | str,
    current: CapabilityStatus | str,
) -> bool:
    """Return whether one independently tracked status may advance to another."""

    previous_status = _coerce_status(previous)
    current_status = _coerce_status(current)
    return current_status in _ALLOWED_STATUS_TRANSITIONS[previous_status]


@dataclass(frozen=True)
class CapabilityDefinition:
    """One canonical capability and its independently verified engine states."""

    id: str
    label: str
    aliases: tuple[str, ...]
    required_configuration: tuple[str, ...]
    target_limitations: tuple[str, ...]
    authoring_status: CapabilityStatus
    engine1_status: CapabilityStatus
    engine2_status: CapabilityStatus
    engine3_status: CapabilityStatus
    end_to_end_status: CapabilityStatus

    @property
    def compiler_pending(self) -> bool:
        return any(
            _coerce_status(status) is CapabilityStatus.PENDING
            for status in (self.engine1_status, self.engine2_status, self.engine3_status)
        )

    @property
    def end_to_end_enabled(self) -> bool:
        return _coerce_status(self.end_to_end_status) is CapabilityStatus.ENABLED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "aliases": list(self.aliases),
            "required_configuration": list(self.required_configuration),
            "target_limitations": list(self.target_limitations),
            "status": {
                "authoring": _coerce_status(self.authoring_status).value,
                "engine1": _coerce_status(self.engine1_status).value,
                "engine2": _coerce_status(self.engine2_status).value,
                "engine3": _coerce_status(self.engine3_status).value,
                "end_to_end": _coerce_status(self.end_to_end_status).value,
            },
        }


def _enabled_capability(
    id: str,
    label: str,
    aliases: tuple[str, ...],
    required_configuration: tuple[str, ...],
    target_limitations: tuple[str, ...] = (),
) -> CapabilityDefinition:
    return CapabilityDefinition(
        id=id,
        label=label,
        aliases=aliases,
        required_configuration=required_configuration,
        target_limitations=target_limitations,
        authoring_status=CapabilityStatus.ENABLED,
        engine1_status=CapabilityStatus.ENABLED,
        engine2_status=CapabilityStatus.ENABLED,
        engine3_status=CapabilityStatus.ENABLED,
        end_to_end_status=CapabilityStatus.ENABLED,
    )


def _pending_capability(
    id: str,
    label: str,
    aliases: tuple[str, ...],
    required_configuration: tuple[str, ...],
    limitation: str,
) -> CapabilityDefinition:
    return CapabilityDefinition(
        id=id,
        label=label,
        aliases=aliases,
        required_configuration=required_configuration,
        target_limitations=(limitation,),
        authoring_status=CapabilityStatus.ENABLED,
        engine1_status=CapabilityStatus.PENDING,
        engine2_status=CapabilityStatus.PENDING,
        engine3_status=CapabilityStatus.PENDING,
        end_to_end_status=CapabilityStatus.PENDING,
    )


CANONICAL_CAPABILITIES: tuple[CapabilityDefinition, ...] = (
    _enabled_capability(
        "start",
        "flow start",
        ("entry", "start_node"),
        (),
        ("Structural entry marker; not a user-facing operation.",),
    ),
    _enabled_capability(
        "send_text_message",
        "send text message",
        ("message", "send_message", "send text", "exact message"),
        ("message.copy", "message.locale", "route.next"),
    ),
    _enabled_capability(
        "capture_user_input",
        "capture user input",
        ("input", "ask_input", "capture", "wait for a reply"),
        (
            "input.prompt",
            "input.input_type",
            "input.save_as",
            "input.required",
            "input.validation",
            "input.route.next",
        ),
        ("The target input contract must retain retry and timeout routes.",),
    ),
    _enabled_capability(
        "fixed_choice",
        "fixed choice",
        ("choice", "ask_choice", "quick reply", "list choice"),
        (
            "choice.title",
            "choice.outcomes",
            "choice.stable_values",
            "choice.route.default",
            "choice.route.invalid",
            "choice.route.timeout",
        ),
        ("Interactive choices are bounded by the verified renderer budget.",),
    ),
    CapabilityDefinition(
        "evaluate_condition",
        "evaluate condition",
        ("decision", "evaluate", "condition", "branch", "router"),
        ("condition.expression", "condition.outcomes", "condition.routes"),
        ("Product 3 mapping evidence is still required before end-to-end enablement.",),
        CapabilityStatus.ENABLED,
        CapabilityStatus.PENDING,
        CapabilityStatus.ENABLED,
        CapabilityStatus.ENABLED,
        CapabilityStatus.PENDING,
    ),
    _enabled_capability(
        "persist_contact_field",
        "persist to contact field",
        ("record", "record_request", "persist", "contact field", "update contact field"),
        (
            "persistence.source_variable",
            "persistence.field_name",
            "persistence.success_route",
            "persistence.failure_route",
        ),
        ("The destination must be an explicitly bound contact-field resource.",),
    ),
    _enabled_capability(
        "end",
        "end flow",
        ("terminal", "ending"),
        ("end.reason",),
    ),
    CapabilityDefinition(
        "join",
        "join routes",
        ("merge",),
        ("join.incoming_routes", "join.next_route"),
        ("Structural normalization only; it cannot create a missing route." ,),
        CapabilityStatus.STRUCTURAL_ONLY,
        CapabilityStatus.STRUCTURAL_ONLY,
        CapabilityStatus.STRUCTURAL_ONLY,
        CapabilityStatus.STRUCTURAL_ONLY,
        CapabilityStatus.STRUCTURAL_ONLY,
    ),
    CapabilityDefinition(
        "retry_policy",
        "retry policy",
        ("retry", "retries", "retry exhaustion"),
        ("retry.max_attempts", "retry.messages", "retry.on_exhausted_route"),
        ("Interaction modifier; it must not invent an exhaustion destination." ,),
        CapabilityStatus.INTERACTION_MODIFIER,
        CapabilityStatus.INTERACTION_MODIFIER,
        CapabilityStatus.INTERACTION_MODIFIER,
        CapabilityStatus.INTERACTION_MODIFIER,
        CapabilityStatus.INTERACTION_MODIFIER,
    ),
    CapabilityDefinition(
        "no_response_timeout",
        "no-response timeout",
        ("timeout", "no response", "wait", "timed wait"),
        ("timeout.seconds", "timeout.route"),
        ("Interaction modifier; a timeout route and duration must be explicit." ,),
        CapabilityStatus.INTERACTION_MODIFIER,
        CapabilityStatus.INTERACTION_MODIFIER,
        CapabilityStatus.INTERACTION_MODIFIER,
        CapabilityStatus.INTERACTION_MODIFIER,
        CapabilityStatus.INTERACTION_MODIFIER,
    ),
    _pending_capability(
        "send_media",
        "send media",
        ("media", "send_media"),
        ("media.kind", "media.resource_ref", "media.caption", "route.next"),
        "No verified Product 2 media compiler mapping.",
    ),
    _pending_capability(
        "call_webhook_api",
        "call webhook/API",
        ("webhook", "call_webhook", "api", "http request"),
        ("integration.ref", "integration.method", "integration.url", "route.success", "route.failure"),
        "Only a secret reference may be captured; no verified compiler lowering exists.",
    ),
    _pending_capability(
        "update_contact",
        "update contact",
        ("contact update", "update_contact"),
        ("contact.binding", "contact.fields", "route.success", "route.failure"),
        "No verified standalone update-contact compiler mapping.",
    ),
    _pending_capability(
        "collection_mutation",
        "collection mutation",
        ("collection", "mutate collection", "collection action"),
        ("collection.ref", "collection.operation", "collection.value", "route.success", "route.failure"),
        "No verified collection mutation compiler mapping.",
    ),
    _pending_capability(
        "delay_schedule",
        "delay/schedule",
        ("delay", "schedule", "wait until"),
        ("delay.duration", "delay.schedule", "route.next", "route.failure"),
        "No verified local delay or schedule compiler mapping.",
    ),
    _pending_capability(
        "handoff_ticket",
        "handoff/ticket",
        ("handoff", "ticket", "human takeover"),
        ("handoff.queue", "handoff.message", "route.success", "route.failure"),
        "No verified human-handoff or ticket compiler mapping.",
    ),
    _pending_capability(
        "enter_subflow",
        "enter subflow",
        ("subflow", "child flow", "enter child flow"),
        ("subflow.ref", "subflow.inputs", "route.return", "route.failure"),
        "No verified child-flow compiler mapping.",
    ),
    _pending_capability(
        "template_hsm_message",
        "template/HSM message",
        ("template", "hsm", "template message", "template_hsm"),
        ("template.ref", "template.parameters", "template.locale", "route.next", "route.failure"),
        "No verified template/HSM compiler mapping.",
    ),
    _pending_capability(
        "operational_action",
        "operational action",
        ("action", "operation"),
        ("action.kind", "action.binding", "route.success", "route.failure"),
        "Generic actions require a verified typed operation contract.",
    ),
)


def _normalize_concept(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def validate_registry(
    registry: Sequence[CapabilityDefinition] = CANONICAL_CAPABILITIES,
) -> None:
    """Validate uniqueness, status semantics, and compiler enablement claims."""

    if not registry:
        raise CapabilityRegistryError("capability registry cannot be empty")
    ids: set[str] = set()
    concepts: dict[str, str] = {}
    for capability in registry:
        if capability.id in ids:
            raise CapabilityRegistryError(f"duplicate capability ID: {capability.id}")
        ids.add(capability.id)
        if not _normalize_concept(capability.id):
            raise CapabilityRegistryError("capability IDs must contain a concept")
        _require = (capability.label, *capability.aliases)
        for raw_concept in _require:
            concept = _normalize_concept(raw_concept)
            if not concept:
                raise CapabilityRegistryError(f"empty concept alias for {capability.id}")
            previous = concepts.get(concept)
            if previous is not None and previous != capability.id:
                raise CapabilityRegistryError(
                    f"concept alias {raw_concept!r} maps to both {previous} and {capability.id}"
                )
            concepts[concept] = capability.id
        statuses = (
            capability.authoring_status,
            capability.engine1_status,
            capability.engine2_status,
            capability.engine3_status,
            capability.end_to_end_status,
        )
        for status in statuses:
            _coerce_status(status)
        if capability.end_to_end_enabled and any(
            _coerce_status(status) is not CapabilityStatus.ENABLED
            for status in (
                capability.authoring_status,
                capability.engine1_status,
                capability.engine2_status,
                capability.engine3_status,
            )
        ):
            raise CapabilityRegistryError(
                f"end-to-end enabled capability {capability.id} has an unverified engine status"
            )
        if capability.compiler_pending and capability.end_to_end_enabled:
            raise CapabilityRegistryError(
                f"compiler-pending capability {capability.id} cannot be end-to-end enabled"
            )
        if any(not isinstance(item, str) or not item.strip() for item in capability.required_configuration):
            raise CapabilityRegistryError(f"invalid required configuration for {capability.id}")
        if any(not isinstance(item, str) or not item.strip() for item in capability.target_limitations):
            raise CapabilityRegistryError(f"invalid target limitation for {capability.id}")


def capability_registry_payload(
    registry: Sequence[CapabilityDefinition] = CANONICAL_CAPABILITIES,
) -> dict[str, Any]:
    validate_registry(registry)
    return {
        "profile_version": CAPABILITY_PROFILE_VERSION,
        "capabilities": [item.to_dict() for item in sorted(registry, key=lambda item: item.id)],
    }


def canonical_registry_json(
    registry: Sequence[CapabilityDefinition] = CANONICAL_CAPABILITIES,
) -> str:
    return json.dumps(
        capability_registry_payload(registry),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_registry_hash(
    registry: Sequence[CapabilityDefinition] = CANONICAL_CAPABILITIES,
) -> str:
    return hashlib.sha256(canonical_registry_json(registry).encode("utf-8")).hexdigest()


CAPABILITY_REGISTRY = {item.id: item for item in CANONICAL_CAPABILITIES}


def _concept_index(
    registry: Sequence[CapabilityDefinition] = CANONICAL_CAPABILITIES,
) -> dict[str, str]:
    index: dict[str, str] = {}
    for capability in registry:
        for concept in (capability.id, capability.label, *capability.aliases):
            index[_normalize_concept(concept)] = capability.id
    return index


def resolve_capability_concept(
    concept: str,
    registry: Sequence[CapabilityDefinition] = CANONICAL_CAPABILITIES,
) -> str | None:
    if not isinstance(concept, str) or not concept.strip():
        return None
    return _concept_index(registry).get(_normalize_concept(concept))


@dataclass(frozen=True)
class CorpusEntry:
    evidence_path: str
    concepts: tuple[str, ...]


COMMITTED_CORPUS: tuple[CorpusEntry, ...] = (
    CorpusEntry(
        "product3/tests/test_capability_pinning.py",
        ("send_message", "input", "choice", "end"),
    ),
    CorpusEntry(
        "product3/tests/test_source_flow_adapter.py",
        ("message", "choice", "input", "record", "join", "retry", "timeout"),
    ),
    CorpusEntry(
        "product3/tests/test_edge_role_annotator.py",
        ("decision", "choice", "no response", "retry"),
    ),
    CorpusEntry(
        "product3/tests/test_pipeline.py",
        ("message", "capture", "persist", "record_request", "end"),
    ),
    CorpusEntry(
        "product3/tests/test_timeout_policy_gate.py",
        ("timeout",),
    ),
    CorpusEntry(
        "product3/mapping_registry.py",
        (
            "start",
            "message",
            "input",
            "decision",
            "handoff",
            "end",
            "media",
            "action",
            "delay",
            "subflow",
            "join",
        ),
    ),
    CorpusEntry(
        "product2/backend/tests/unit/test_flow_spec_pipeline.py",
        ("evaluate", "choice", "input", "record", "retry", "timeout", "end"),
    ),
    CorpusEntry(
        "product2/contracts/glific-capabilities-verified-0.1.json",
        ("webhook", "update_contact", "collection", "template", "hsm"),
    ),
)


def build_corpus_coverage_report(
    entries: Iterable[CorpusEntry] = COMMITTED_CORPUS,
    *,
    repository_root: Path | str | None = None,
    minimum_coverage: float = MINIMUM_CORPUS_COVERAGE,
) -> dict[str, Any]:
    """Classify retained concepts and report the deterministic coverage gate."""

    if not 0 < minimum_coverage <= 1:
        raise CapabilityRegistryError("minimum_coverage must be between zero and one")
    root = Path(repository_root) if repository_root is not None else None
    index = _concept_index()
    normalized_entries: list[dict[str, Any]] = []
    unknown_concepts: list[str] = []
    recognized_count = 0
    total_count = 0
    represented: set[str] = set()
    for entry in entries:
        if not isinstance(entry, CorpusEntry):
            raise CapabilityRegistryError("corpus entries must be CorpusEntry values")
        if root is not None and not (root / entry.evidence_path).is_file():
            raise CapabilityRegistryError(f"corpus evidence file is missing: {entry.evidence_path}")
        recognized: list[dict[str, str]] = []
        unknown: list[str] = []
        for raw_concept in entry.concepts:
            total_count += 1
            capability_id = index.get(_normalize_concept(raw_concept))
            if capability_id is None:
                unknown.append(raw_concept)
                unknown_concepts.append(raw_concept)
                continue
            recognized_count += 1
            represented.add(capability_id)
            recognized.append({"concept": raw_concept, "capability_id": capability_id})
        normalized_entries.append(
            {
                "evidence_path": entry.evidence_path,
                "recognized": recognized,
                "unknown": unknown,
            }
        )
    coverage = recognized_count / total_count if total_count else 0.0
    report = {
        "schema_version": "capability-corpus-coverage-0.1",
        "profile_version": CAPABILITY_PROFILE_VERSION,
        "registry_hash": canonical_registry_hash(),
        "minimum_coverage": minimum_coverage,
        "total_concepts": total_count,
        "recognized_concepts": recognized_count,
        "unknown_concepts": len(unknown_concepts),
        "coverage_ratio": coverage,
        "unknown_ratio": 1.0 - coverage,
        "represented_capability_ids": sorted(represented),
        "represented_capability_count": len(represented),
        "registry_capability_count": len(CANONICAL_CAPABILITIES),
        "representation_ratio": len(represented) / len(CANONICAL_CAPABILITIES),
        "unresolved_concepts": sorted(set(unknown_concepts)),
        "entries": normalized_entries,
        "passed": coverage >= minimum_coverage,
    }
    return report


def current_product3_concept_report() -> dict[str, Any]:
    """Resolve every source and target concept currently exposed by Product 3."""

    from ..mapping_registry import RULES, UNSUPPORTED_SOURCE_TYPES

    concepts = set(RULES) | set(UNSUPPORTED_SOURCE_TYPES)
    for rule in RULES.values():
        concepts.update(rule.target_types)
    resolved = {
        concept: resolve_capability_concept(concept)
        for concept in sorted(concepts)
    }
    unresolved = sorted(concept for concept, capability_id in resolved.items() if capability_id is None)
    return {
        "schema_version": "product3-capability-resolution-0.1",
        "profile_version": CAPABILITY_PROFILE_VERSION,
        "registry_hash": canonical_registry_hash(),
        "resolved": resolved,
        "unresolved": unresolved,
        "passed": not unresolved,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_corpus_coverage_report(repository_root=args.repository_root)
    report["product3_concepts"] = current_product3_concept_report()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["passed"] and report["product3_concepts"]["passed"] else 1


validate_registry()


if __name__ == "__main__":
    raise SystemExit(main())
