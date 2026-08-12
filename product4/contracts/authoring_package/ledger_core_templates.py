"""Typed core capability templates for the requirements ledger.

T04 owns the common ledger envelope.  This module owns the closed, core
capability payloads that are carried by that envelope and the discriminated
union used to parse them.  Route fields store requirement IDs; a typed ledger
validator below verifies that those IDs resolve without inventing graph data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from .ledger_contracts import (
    CapabilityId,
    RequirementEnvelope,
    RequirementId,
    RequirementsLedger,
    serialize_ledger_json,
)

CORE_LEDGER_TEMPLATE_VERSION = "requirements-ledger-core-1.0"


class CoreTemplateError(ValueError):
    """Raised when a core ledger template or route reference is invalid."""


class _TemplateModel(BaseModel):
    """Strict base model for capability payloads and nested values."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


NonEmptyText: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=10_000),
]
StableValue: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=300),
]
PositiveInteger: TypeAlias = Annotated[StrictInt, Field(gt=0)]


class RetryPolicySpec(_TemplateModel):
    """A bounded retry modifier that names an explicit exhaustion route."""

    max_attempts: PositiveInteger
    messages: list[NonEmptyText] = Field(min_length=1)
    on_exhausted_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices(
            "on_exhausted_requirement_id",
            "on_exhausted_route",
            "on_exhausted_node_id",
        )
    )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.on_exhausted_requirement_id,)


class NoResponseTimeoutSpec(_TemplateModel):
    """A timeout modifier with a positive duration and explicit route."""

    timeout_seconds: PositiveInteger = Field(
        validation_alias=AliasChoices("timeout_seconds", "seconds")
    )
    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "timeout_route", "next_route")
    )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.next_requirement_id,)


class ChoiceOutcome(_TemplateModel):
    """One visible choice value and its explicit destination requirement."""

    label: NonEmptyText
    value: StableValue = Field(validation_alias=AliasChoices("value", "stable_value"))
    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "next_route")
    )

    @property
    def stable_value(self) -> str:
        """Compatibility name for callers that use the design-note wording."""

        return self.value

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.next_requirement_id,)


class ConditionRoute(_TemplateModel):
    """One condition outcome key and its explicit destination requirement."""

    outcome: StableValue
    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "next_route")
    )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.next_requirement_id,)


class SendTextMessagePayload(_TemplateModel):
    """Execution details for a text-message requirement."""

    copy: NonEmptyText = Field(validation_alias=AliasChoices("copy", "message_copy"))
    locale: NonEmptyText
    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "next_route")
    )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.next_requirement_id,)


class CaptureUserInputPayload(_TemplateModel):
    """Execution details for a captured user result."""

    prompt: NonEmptyText
    input_type: NonEmptyText
    save_as: NonEmptyText = Field(validation_alias=AliasChoices("save_as", "result_name"))
    required: StrictBool
    validation: dict[str, Any]
    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "next_route")
    )
    retry: RetryPolicySpec | None = None
    no_response: NoResponseTimeoutSpec | None = None

    @property
    def result_name(self) -> str:
        """Compatibility name for the result variable."""

        return self.save_as

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        route_ids = [self.next_requirement_id]
        if self.retry is not None:
            route_ids.extend(self.retry.route_requirement_ids)
        if self.no_response is not None:
            route_ids.extend(self.no_response.route_requirement_ids)
        return tuple(route_ids)


class FixedChoicePayload(_TemplateModel):
    """Execution details for a fixed, visible choice."""

    title: NonEmptyText
    outcomes: list[ChoiceOutcome] = Field(min_length=2)
    stable_values: list[StableValue] = Field(min_length=2)
    default_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("default_requirement_id", "default_route")
    )
    invalid_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("invalid_requirement_id", "invalid_route")
    )
    timeout_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("timeout_requirement_id", "timeout_route")
    )

    @model_validator(mode="after")
    def stable_values_are_unique_and_bound(self) -> FixedChoicePayload:
        outcome_values = [outcome.value for outcome in self.outcomes]
        if len(outcome_values) != len(set(outcome_values)):
            raise ValueError("choice outcomes must have unique stable values")
        if len(self.stable_values) != len(set(self.stable_values)):
            raise ValueError("choice stable_values must be unique")
        if set(self.stable_values) != set(outcome_values):
            raise ValueError("choice stable_values must match outcome values")
        return self

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        outcome_routes = tuple(
            route_id for outcome in self.outcomes for route_id in outcome.route_requirement_ids
        )
        return (
            *outcome_routes,
            self.default_requirement_id,
            self.invalid_requirement_id,
            self.timeout_requirement_id,
        )


class EvaluateConditionPayload(_TemplateModel):
    """Execution details for a deterministic condition evaluation."""

    expression: NonEmptyText
    outcomes: list[StableValue] = Field(min_length=2)
    routes: dict[StableValue, RequirementId] = Field(min_length=2)

    @model_validator(mode="after")
    def condition_outcomes_have_routes(self) -> EvaluateConditionPayload:
        if len(self.outcomes) != len(set(self.outcomes)):
            raise ValueError("condition outcomes must be unique")
        if set(self.routes) != set(self.outcomes):
            raise ValueError("condition routes must match condition outcomes")
        return self

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return tuple(self.routes.values())


class PersistContactFieldPayload(_TemplateModel):
    """Execution details for persisting a captured variable."""

    source_variable: NonEmptyText
    field_name: NonEmptyText = Field(
        validation_alias=AliasChoices("field_name", "destination_field")
    )
    success_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("success_requirement_id", "success_route")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure_route")
    )

    @property
    def destination(self) -> str:
        """Compatibility name for the contact-field destination."""

        return self.field_name

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.success_requirement_id, self.failure_requirement_id)


class JoinPayload(_TemplateModel):
    """Structural join of at least two incoming requirement routes."""

    incoming_routes: list[RequirementId] = Field(
        min_length=2,
        validation_alias=AliasChoices("incoming_routes", "incoming_requirement_ids"),
    )
    next_route: RequirementId = Field(
        validation_alias=AliasChoices("next_route", "next_requirement_id")
    )

    @model_validator(mode="after")
    def incoming_routes_are_unique(self) -> JoinPayload:
        if len(self.incoming_routes) != len(set(self.incoming_routes)):
            raise ValueError("join incoming routes must be unique")
        return self

    @property
    def next_requirement_id(self) -> str:
        """Compatibility name for the join continuation route."""

        return self.next_route

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (*self.incoming_routes, self.next_route)


class EndPayload(_TemplateModel):
    """Execution details for a terminal requirement."""

    reason: NonEmptyText

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return ()


class _CoreRequirement(RequirementEnvelope):
    """Common envelope specialized by one core capability discriminator."""

    capability: CapabilityId
    payload: _TemplateModel


class SendTextMessageRequirement(_CoreRequirement):
    capability: Literal["send_text_message"]
    payload: SendTextMessagePayload


class CaptureUserInputRequirement(_CoreRequirement):
    capability: Literal["capture_user_input"]
    payload: CaptureUserInputPayload


class FixedChoiceRequirement(_CoreRequirement):
    capability: Literal["fixed_choice"]
    payload: FixedChoicePayload


class EvaluateConditionRequirement(_CoreRequirement):
    capability: Literal["evaluate_condition"]
    payload: EvaluateConditionPayload


class PersistContactFieldRequirement(_CoreRequirement):
    capability: Literal["persist_contact_field"]
    payload: PersistContactFieldPayload


class JoinRequirement(_CoreRequirement):
    capability: Literal["join"]
    payload: JoinPayload


class EndRequirement(_CoreRequirement):
    capability: Literal["end"]
    payload: EndPayload


class RetryPolicyRequirement(_CoreRequirement):
    capability: Literal["retry_policy"]
    payload: RetryPolicySpec


class NoResponseTimeoutRequirement(_CoreRequirement):
    capability: Literal["no_response_timeout"]
    payload: NoResponseTimeoutSpec


CoreLedgerRequirementUnion: TypeAlias = Annotated[
    (
        SendTextMessageRequirement
        | CaptureUserInputRequirement
        | FixedChoiceRequirement
        | EvaluateConditionRequirement
        | PersistContactFieldRequirement
        | JoinRequirement
        | EndRequirement
        | RetryPolicyRequirement
        | NoResponseTimeoutRequirement
    ),
    Field(discriminator="capability"),
]

# Short aliases keep the capability names discoverable without introducing a
# second set of Pydantic models or a second union that could drift.
CoreLedgerRequirement = CoreLedgerRequirementUnion
CoreRequirement = CoreLedgerRequirementUnion
LedgerCoreRequirement = CoreLedgerRequirementUnion
CoreRequirementTemplate = CoreLedgerRequirementUnion


class CoreLedgerRequirementDocument(RootModel[CoreLedgerRequirementUnion]):
    """Root-model adapter for callers that need ``model_validate`` on a union."""


class CoreRequirementsLedger(RequirementsLedger):
    """T05 ledger envelope with typed core requirements and resolved routes."""

    requirements: list[CoreLedgerRequirementUnion] = Field(min_length=1)

    @model_validator(mode="after")
    def route_references_resolve(self) -> CoreRequirementsLedger:
        requirement_ids = {requirement.id for requirement in self.requirements}
        for requirement in self.requirements:
            missing = sorted(
                {
                    route_id
                    for route_id in _route_requirement_ids(requirement)
                    if route_id not in requirement_ids
                }
            )
            if missing:
                raise ValueError(
                    f"requirement {requirement.id} references unknown route requirements: {missing}"
                )
        return self


def _route_requirement_ids(requirement: _CoreRequirement) -> tuple[str, ...]:
    route_ids = getattr(requirement.payload, "route_requirement_ids", ())
    return tuple(route_ids)


CORE_REQUIREMENT_ADAPTER = TypeAdapter(CoreLedgerRequirementUnion)


def parse_core_requirement(value: Mapping[str, Any] | str | bytes | bytearray) -> _CoreRequirement:
    """Parse one typed requirement through the capability discriminator."""

    if isinstance(value, (str, bytes, bytearray)):
        return CORE_REQUIREMENT_ADAPTER.validate_json(value)
    return CORE_REQUIREMENT_ADAPTER.validate_python(value)


def parse_core_ledger_json(value: str | bytes | bytearray) -> CoreRequirementsLedger:
    """Parse a typed core ledger and verify all route requirement IDs."""

    return CoreRequirementsLedger.model_validate_json(value)


def serialize_core_ledger_json(value: CoreRequirementsLedger | Mapping[str, Any]) -> str:
    """Serialize a typed core ledger deterministically."""

    ledger = value if isinstance(value, CoreRequirementsLedger) else CoreRequirementsLedger.model_validate(value)
    return serialize_ledger_json(ledger)


def canonical_core_schema_json() -> str:
    """Return the deterministic JSON Schema for the typed core ledger."""

    return json.dumps(
        CoreRequirementsLedger.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_core_schema_hash() -> str:
    """Return the SHA-256 hash of the typed core ledger JSON Schema."""

    return hashlib.sha256(canonical_core_schema_json().encode("utf-8")).hexdigest()


# Friendly names used by the implementation note and by downstream form code.
SendMessageRequirement = SendTextMessageRequirement
CaptureInputRequirement = CaptureUserInputRequirement
EvaluateRequirement = EvaluateConditionRequirement
PersistValueRequirement = PersistContactFieldRequirement
PersistenceRequirement = PersistContactFieldRequirement
RetryPolicyModifier = RetryPolicyRequirement
NoResponseModifier = NoResponseTimeoutRequirement
NoResponseRequirement = NoResponseTimeoutRequirement
RetryPolicy = RetryPolicySpec
NoResponseTimeout = NoResponseTimeoutSpec
RetryPolicyPayload = RetryPolicySpec
NoResponseTimeoutPayload = NoResponseTimeoutSpec
SendMessagePayload = SendTextMessagePayload
CaptureInputPayload = CaptureUserInputPayload
PersistValuePayload = PersistContactFieldPayload


__all__ = [
    "CORE_LEDGER_TEMPLATE_VERSION",
    "CORE_REQUIREMENT_ADAPTER",
    "CaptureInputPayload",
    "CaptureInputRequirement",
    "CaptureUserInputPayload",
    "CaptureUserInputRequirement",
    "ChoiceOutcome",
    "ConditionRoute",
    "CoreLedgerRequirement",
    "CoreLedgerRequirementDocument",
    "CoreLedgerRequirementUnion",
    "CoreRequirement",
    "CoreRequirementTemplate",
    "CoreRequirementsLedger",
    "CoreTemplateError",
    "EndPayload",
    "EndRequirement",
    "EvaluateConditionPayload",
    "EvaluateConditionRequirement",
    "EvaluateRequirement",
    "FixedChoicePayload",
    "FixedChoiceRequirement",
    "JoinPayload",
    "JoinRequirement",
    "LedgerCoreRequirement",
    "NoResponseModifier",
    "NoResponseRequirement",
    "NoResponseTimeout",
    "NoResponseTimeoutPayload",
    "NoResponseTimeoutRequirement",
    "NoResponseTimeoutSpec",
    "NonEmptyText",
    "PersistContactFieldPayload",
    "PersistContactFieldRequirement",
    "PersistValuePayload",
    "PersistValueRequirement",
    "PersistenceRequirement",
    "RetryPolicy",
    "RetryPolicyModifier",
    "RetryPolicyPayload",
    "RetryPolicyRequirement",
    "RetryPolicySpec",
    "SendMessagePayload",
    "SendMessageRequirement",
    "SendTextMessagePayload",
    "SendTextMessageRequirement",
    "StableValue",
    "canonical_core_schema_hash",
    "canonical_core_schema_json",
    "parse_core_ledger_json",
    "parse_core_requirement",
    "serialize_core_ledger_json",
]
