"""Closed classification of authoring fields that can change topology."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from .capabilities import (
    CANONICAL_CAPABILITIES,
    CapabilityDefinition,
    resolve_capability_concept,
)

FIELD_POLICY_VERSION = "authoring-field-policy-1.0"


class FieldPolicyError(ValueError):
    """Raised when a field policy is malformed or cannot be resolved."""


class UnknownFieldPolicyError(FieldPolicyError):
    """Raised when a field is not present in the closed policy table."""


class FieldClassification(str, Enum):
    """The only classifications allowed by the field policy."""

    TOPOLOGY = "topology"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


# ``FieldPolicy`` is the concise public name used by callers that only need the
# classification values.  The longer name remains useful in type annotations.
FieldPolicy = FieldClassification


def _frozen_fields(
    fields: Mapping[str, FieldClassification],
) -> Mapping[str, FieldClassification]:
    return MappingProxyType(dict(fields))


_RAW_CAPABILITY_FIELD_POLICY: dict[str, Mapping[str, FieldClassification]] = {
    "start": {},
    "send_text_message": {
        "message.copy": FieldClassification.CONFIGURATION,
        "message.locale": FieldClassification.CONFIGURATION,
        "route.next": FieldClassification.TOPOLOGY,
    },
    "capture_user_input": {
        "input.prompt": FieldClassification.CONFIGURATION,
        "input.input_type": FieldClassification.CONFIGURATION,
        "input.save_as": FieldClassification.CONFIGURATION,
        "input.required": FieldClassification.CONFIGURATION,
        "input.validation": FieldClassification.CONFIGURATION,
        "input.route.next": FieldClassification.TOPOLOGY,
    },
    "fixed_choice": {
        "choice.title": FieldClassification.CONFIGURATION,
        "choice.outcomes": FieldClassification.TOPOLOGY,
        "choice.stable_values": FieldClassification.CONFIGURATION,
        "choice.route.default": FieldClassification.TOPOLOGY,
        "choice.route.invalid": FieldClassification.TOPOLOGY,
        "choice.route.timeout": FieldClassification.TOPOLOGY,
    },
    "evaluate_condition": {
        "condition.expression": FieldClassification.CONFIGURATION,
        "condition.outcomes": FieldClassification.TOPOLOGY,
        "condition.routes": FieldClassification.TOPOLOGY,
    },
    "persist_contact_field": {
        "persistence.source_variable": FieldClassification.CONFIGURATION,
        "persistence.field_name": FieldClassification.CONFIGURATION,
        "persistence.success_route": FieldClassification.TOPOLOGY,
        "persistence.failure_route": FieldClassification.TOPOLOGY,
    },
    "end": {
        "end.reason": FieldClassification.CONFIGURATION,
    },
    "join": {
        "join.incoming_routes": FieldClassification.TOPOLOGY,
        "join.next_route": FieldClassification.TOPOLOGY,
    },
    "retry_policy": {
        "retry.max_attempts": FieldClassification.CONFIGURATION,
        "retry.messages": FieldClassification.CONFIGURATION,
        "retry.on_exhausted_route": FieldClassification.TOPOLOGY,
    },
    "no_response_timeout": {
        "timeout.seconds": FieldClassification.CONFIGURATION,
        "timeout.route": FieldClassification.TOPOLOGY,
    },
    "send_media": {
        "media.kind": FieldClassification.CONFIGURATION,
        "media.resource_ref": FieldClassification.CONFIGURATION,
        "media.caption": FieldClassification.CONFIGURATION,
        "route.next": FieldClassification.TOPOLOGY,
    },
    "call_webhook_api": {
        "integration.ref": FieldClassification.CONFIGURATION,
        "integration.method": FieldClassification.CONFIGURATION,
        "integration.url": FieldClassification.CONFIGURATION,
        "route.success": FieldClassification.TOPOLOGY,
        "route.failure": FieldClassification.TOPOLOGY,
    },
    "update_contact": {
        "contact.binding": FieldClassification.CONFIGURATION,
        "contact.fields": FieldClassification.CONFIGURATION,
        "route.success": FieldClassification.TOPOLOGY,
        "route.failure": FieldClassification.TOPOLOGY,
    },
    "collection_mutation": {
        "collection.ref": FieldClassification.CONFIGURATION,
        "collection.operation": FieldClassification.CONFIGURATION,
        "collection.value": FieldClassification.CONFIGURATION,
        "route.success": FieldClassification.TOPOLOGY,
        "route.failure": FieldClassification.TOPOLOGY,
    },
    "delay_schedule": {
        "delay.duration": FieldClassification.CONFIGURATION,
        "delay.schedule": FieldClassification.CONFIGURATION,
        "route.next": FieldClassification.TOPOLOGY,
        "route.failure": FieldClassification.TOPOLOGY,
    },
    "handoff_ticket": {
        "handoff.queue": FieldClassification.CONFIGURATION,
        "handoff.message": FieldClassification.CONFIGURATION,
        "route.success": FieldClassification.TOPOLOGY,
        "route.failure": FieldClassification.TOPOLOGY,
    },
    "enter_subflow": {
        "subflow.ref": FieldClassification.CONFIGURATION,
        "subflow.inputs": FieldClassification.CONFIGURATION,
        "route.return": FieldClassification.TOPOLOGY,
        "route.failure": FieldClassification.TOPOLOGY,
    },
    "template_hsm_message": {
        "template.ref": FieldClassification.CONFIGURATION,
        "template.parameters": FieldClassification.CONFIGURATION,
        "template.locale": FieldClassification.CONFIGURATION,
        "route.next": FieldClassification.TOPOLOGY,
        "route.failure": FieldClassification.TOPOLOGY,
    },
    "operational_action": {
        "action.kind": FieldClassification.CONFIGURATION,
        "action.binding": FieldClassification.CONFIGURATION,
        "route.success": FieldClassification.TOPOLOGY,
        "route.failure": FieldClassification.TOPOLOGY,
    },
}


CAPABILITY_FIELD_POLICY_TABLE: Mapping[str, Mapping[str, FieldClassification]] = MappingProxyType(
    {capability_id: _frozen_fields(fields) for capability_id, fields in _RAW_CAPABILITY_FIELD_POLICY.items()}
)


def _flatten_policy_table(
    table: Mapping[str, Mapping[str, FieldClassification]],
) -> dict[str, FieldClassification]:
    flattened: dict[str, FieldClassification] = {}
    for fields in table.values():
        for field_path, policy in fields.items():
            previous = flattened.get(field_path)
            if previous is not None and previous is not policy:
                raise FieldPolicyError(f"field has conflicting policies: {field_path}")
            flattened[field_path] = policy
    return flattened


# The flat table is the public closed set of fully-qualified field paths.  The
# capability-scoped table above preserves the registry relationship for exact
# coverage checks and for callers that want to disambiguate a field namespace.
FIELD_POLICY_TABLE: Mapping[str, FieldClassification] = MappingProxyType(
    _flatten_policy_table(CAPABILITY_FIELD_POLICY_TABLE)
)
FIELD_POLICY = FIELD_POLICY_TABLE


# These are explicit compatibility spellings from the authoring design note.
# They are lookup aliases, not registered fields, so they do not expand the
# registry/policy coverage set or permit arbitrary path normalization.
_FIELD_PATH_ALIASES: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("retry_policy", "retry.on_exhausted_requirement_id"): "retry.on_exhausted_route",
        ("retry_policy", "retry.on_exhausted_node_id"): "retry.on_exhausted_route",
        ("fixed_choice", "choice.next_requirement_id"): "choice.route.default",
        ("fixed_choice", "choice.next_node_id"): "choice.route.default",
    }
)
_GLOBAL_FIELD_PATH_ALIASES: Mapping[str, str] = MappingProxyType(
    {field_path: canonical for (_, field_path), canonical in _FIELD_PATH_ALIASES.items()}
)

# Capability namespace spellings are also explicit.  In particular, the
# registry calls the persistence capability ``persist_contact_field`` while
# its field namespace is ``persistence``.
_CAPABILITY_NAMESPACE_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "persistence": "persist_contact_field",
    }
)


@dataclass(frozen=True)
class FieldPolicyCoverage:
    """Deterministic comparison between registry fields and policy fields."""

    registry_fields: frozenset[tuple[str, str]]
    policy_fields: frozenset[tuple[str, str]]
    missing_fields: frozenset[tuple[str, str]]
    extra_fields: frozenset[tuple[str, str]]

    @property
    def passed(self) -> bool:
        return not self.missing_fields and not self.extra_fields


def _registry_field_set() -> frozenset[tuple[str, str]]:
    return frozenset(
        (capability.id, field_path)
        for capability in CANONICAL_CAPABILITIES
        for field_path in capability.required_configuration
    )


def _policy_field_set() -> frozenset[tuple[str, str]]:
    return frozenset(
        (capability_id, field_path)
        for capability_id, fields in CAPABILITY_FIELD_POLICY_TABLE.items()
        for field_path in fields
    )


def field_policy_coverage() -> FieldPolicyCoverage:
    """Return the exact registry/policy field-set comparison."""

    registry_fields = _registry_field_set()
    policy_fields = _policy_field_set()
    return FieldPolicyCoverage(
        registry_fields=registry_fields,
        policy_fields=policy_fields,
        missing_fields=registry_fields - policy_fields,
        extra_fields=policy_fields - registry_fields,
    )


def validate_field_policy_coverage() -> FieldPolicyCoverage:
    """Validate that the closed policy covers precisely the registry fields."""

    coverage = field_policy_coverage()
    if not coverage.passed:
        raise FieldPolicyError(
            "field policy coverage mismatch: "
            f"missing={sorted(coverage.missing_fields)!r}, "
            f"extra={sorted(coverage.extra_fields)!r}"
        )
    return coverage


def _resolve_capability_id(value: str | CapabilityDefinition) -> str | None:
    if isinstance(value, CapabilityDefinition):
        return value.id if value.id in CAPABILITY_FIELD_POLICY_TABLE else None
    if not isinstance(value, str) or not value.strip():
        return None
    if value in CAPABILITY_FIELD_POLICY_TABLE:
        return value
    namespace = _CAPABILITY_NAMESPACE_ALIASES.get(value)
    if namespace is not None:
        return namespace
    return resolve_capability_concept(value)


def _canonical_field_path(capability_id: str, field_path: str) -> str:
    return _FIELD_PATH_ALIASES.get((capability_id, field_path), field_path)


def classify_field_path(
    capability_or_path: str | CapabilityDefinition,
    field_path: str | None = None,
) -> FieldClassification:
    """Classify an exact capability field path, returning ``unknown`` closed."""

    if field_path is None:
        if not isinstance(capability_or_path, str):
            return FieldClassification.UNKNOWN
        canonical_path = _GLOBAL_FIELD_PATH_ALIASES.get(capability_or_path, capability_or_path)
        return FIELD_POLICY_TABLE.get(canonical_path, FieldClassification.UNKNOWN)

    capability_id = _resolve_capability_id(capability_or_path)
    if capability_id is None or not isinstance(field_path, str):
        return FieldClassification.UNKNOWN
    canonical_path = _canonical_field_path(capability_id, field_path)
    return CAPABILITY_FIELD_POLICY_TABLE.get(capability_id, {}).get(
        canonical_path,
        FieldClassification.UNKNOWN,
    )


def classify_field(
    capability_or_path: str | CapabilityDefinition,
    field_path: str | None = None,
) -> FieldClassification:
    """Alias for :func:`classify_field_path` used by form/review callers."""

    return classify_field_path(capability_or_path, field_path)


def require_field_policy(
    capability_or_path: str | CapabilityDefinition,
    field_path: str | None = None,
) -> FieldClassification:
    """Return a known policy or reject the field as requiring review."""

    policy = classify_field_path(capability_or_path, field_path)
    if policy is FieldClassification.UNKNOWN:
        if field_path is None:
            field_label = repr(capability_or_path)
        else:
            field_label = f"{capability_or_path!r}.{field_path!r}"
        raise UnknownFieldPolicyError(
            f"unknown field policy for {field_label}; review required"
        )
    return policy


def is_topology_field(
    capability_or_path: str | CapabilityDefinition,
    field_path: str | None = None,
) -> bool:
    """Return whether a known field can change the execution topology."""

    return require_field_policy(capability_or_path, field_path) is FieldClassification.TOPOLOGY


_MISSING = object()


def review_confirmation_stale_for_change(
    capability_or_path: str | CapabilityDefinition,
    field_path: str | None = None,
    old_value: Any = _MISSING,
    new_value: Any = _MISSING,
) -> bool:
    """Return whether a field change requires review reconfirmation.

    Unknown fields fail closed: they are treated as requiring review.  When no
    old/new values are supplied, the call represents a proposed change.
    """

    policy = classify_field_path(capability_or_path, field_path)
    if policy is FieldClassification.UNKNOWN:
        return True
    if policy is FieldClassification.CONFIGURATION:
        return False
    if old_value is _MISSING or new_value is _MISSING:
        return True
    return old_value != new_value


def is_review_confirmation_stale(
    capability_or_path: str | CapabilityDefinition,
    field_path: str | None = None,
    old_value: Any = _MISSING,
    new_value: Any = _MISSING,
) -> bool:
    """Alias for :func:`review_confirmation_stale_for_change`."""

    return review_confirmation_stale_for_change(
        capability_or_path,
        field_path,
        old_value,
        new_value,
    )


def field_change_requires_reconfirmation(
    capability_or_path: str | CapabilityDefinition,
    field_path: str | None = None,
    old_value: Any = _MISSING,
    new_value: Any = _MISSING,
) -> bool:
    """Return whether a proposed field update invalidates review confirmation."""

    return review_confirmation_stale_for_change(
        capability_or_path,
        field_path,
        old_value,
        new_value,
    )


validate_field_policy_coverage()
