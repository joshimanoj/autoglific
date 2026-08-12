"""Typed source-node contracts for the execution-complete authoring path.

The requirements ledger names *why* a flow needs a capability and uses
requirement IDs for its routes.  This module names the concrete graph elements
that implement those requirements and uses node IDs for every source route.
The two ID spaces are deliberately different and are never accepted
interchangeably.

This is a new authoring-path contract.  The legacy Product 1 ``FlowNode``
schema remains unchanged and is not imported here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    RootModel,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .capabilities import CANONICAL_CAPABILITIES, CAPABILITY_REGISTRY
from .ledger_core_templates import (
    NonEmptyText,
    StableValue,
)
from .ledger_pending_templates import (
    CollectionOperation,
    FieldName,
    HttpMethod,
    MediaKind,
    ResourceReference,
    SecretReference,
)

SOURCE_NODE_SCHEMA_VERSION = "source-node-contracts-1.0"
SOURCE_NODE_TEMPLATE_VERSION = "source-node-templates-1.0"


class SourceNodeContractError(ValueError):
    """Raised when source-node templates do not match the closed contract."""


class SourceNodeRegistryError(SourceNodeContractError):
    """Raised when the node templates and capability registry drift apart."""


def _validate_concrete_node_id(value: str) -> str:
    """Reject IDs belonging to the requirements/lifecycle namespaces."""

    upper_value = value.upper()
    if upper_value.startswith(("REQ-", "DEC-", "LEDGER-", "CONF-")):
        raise ValueError("source node IDs and routes must not use ledger IDs")
    return value


NodeId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=100,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]*$",
    ),
    AfterValidator(_validate_concrete_node_id),
]
SourceUnitId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]*$",
    ),
]
PositiveInteger: TypeAlias = Annotated[StrictInt, Field(gt=0)]


class _StrictModel(BaseModel):
    """Strict base class shared by every new source-node object."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class SourceReference(_StrictModel):
    """A source-grounded reference carried by a typed node."""

    source_unit_id: SourceUnitId
    source_quote: NonEmptyText


class RetryPolicyNodeSpec(_StrictModel):
    """Concrete retry configuration whose exhaustion route is a node ID."""

    max_attempts: PositiveInteger
    messages: list[NonEmptyText] = Field(min_length=1)
    on_exhausted_node_id: NodeId

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.on_exhausted_node_id,)


class NoResponseTimeoutNodeSpec(_StrictModel):
    """Concrete timeout configuration whose destination is a node ID."""

    timeout_seconds: PositiveInteger
    next_node_id: NodeId

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.next_node_id,)


class ChoiceNodeOutcome(_StrictModel):
    """One visible choice and its concrete destination node."""

    label: NonEmptyText
    value: StableValue = Field(validation_alias=AliasChoices("value", "stable_value"))
    next_node_id: NodeId

    @property
    def stable_value(self) -> str:
        return self.value

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.next_node_id,)


class _SourceNode(_StrictModel):
    """Common source graph identity and provenance fields."""

    id: NodeId
    type: str
    label: NonEmptyText | None = None
    source_refs: list[SourceReference] = Field(
        min_length=1,
        validation_alias=AliasChoices("source_refs", "provenance"),
    )
    CAPABILITY_ID: ClassVar[str]

    @property
    def capability(self) -> str:
        """Return the registry capability represented by this node type."""

        return self.CAPABILITY_ID

    @property
    def capability_id(self) -> str:
        return self.CAPABILITY_ID


class StartNode(_SourceNode):
    """Structural entry marker; it has no executable label semantics."""

    type: Literal["start"]
    CAPABILITY_ID: ClassVar[str] = "start"


class SendMessageNode(_SourceNode):
    """A typed text-message source node."""

    type: Literal["send_message"]
    copy: NonEmptyText = Field(validation_alias=AliasChoices("copy", "message_copy"))
    locale: NonEmptyText
    next_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "send_text_message"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.next_node_id,)


class CaptureInputNode(_SourceNode):
    """A typed input interaction with concrete retry/timeout destinations."""

    type: Literal["capture_input"]
    prompt: NonEmptyText
    input_type: NonEmptyText
    save_as: NonEmptyText = Field(validation_alias=AliasChoices("save_as", "result_name"))
    required: StrictBool
    validation: dict[str, Any]
    next_node_id: NodeId
    retry: RetryPolicyNodeSpec | None = None
    no_response: NoResponseTimeoutNodeSpec | None = None
    CAPABILITY_ID: ClassVar[str] = "capture_user_input"

    @property
    def result_name(self) -> str:
        return self.save_as

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        route_ids = [self.next_node_id]
        if self.retry is not None:
            route_ids.extend(self.retry.route_node_ids)
        if self.no_response is not None:
            route_ids.extend(self.no_response.route_node_ids)
        return tuple(route_ids)


class FixedChoiceNode(_SourceNode):
    """A bounded visible choice with explicit default/invalid/timeout routes."""

    type: Literal["fixed_choice"]
    title: NonEmptyText
    outcomes: list[ChoiceNodeOutcome] = Field(min_length=2)
    stable_values: list[StableValue] = Field(min_length=2)
    default_node_id: NodeId
    invalid_node_id: NodeId
    timeout_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "fixed_choice"

    @model_validator(mode="after")
    def choice_values_are_unique_and_bound(self) -> FixedChoiceNode:
        outcome_values = [outcome.value for outcome in self.outcomes]
        if len(outcome_values) != len(set(outcome_values)):
            raise ValueError("choice outcomes must have unique stable values")
        if len(self.stable_values) != len(set(self.stable_values)):
            raise ValueError("choice stable_values must be unique")
        if set(self.stable_values) != set(outcome_values):
            raise ValueError("choice stable_values must match outcome values")
        return self

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        outcome_routes = tuple(
            route_id for outcome in self.outcomes for route_id in outcome.route_node_ids
        )
        return (
            *outcome_routes,
            self.default_node_id,
            self.invalid_node_id,
            self.timeout_node_id,
        )


class EvaluateConditionNode(_SourceNode):
    """A deterministic condition with one concrete node route per outcome."""

    type: Literal["evaluate_condition"]
    expression: NonEmptyText
    outcomes: list[StableValue] = Field(min_length=2)
    routes: dict[StableValue, NodeId] = Field(min_length=2)
    CAPABILITY_ID: ClassVar[str] = "evaluate_condition"

    @model_validator(mode="after")
    def condition_outcomes_have_routes(self) -> EvaluateConditionNode:
        if len(self.outcomes) != len(set(self.outcomes)):
            raise ValueError("condition outcomes must be unique")
        if set(self.routes) != set(self.outcomes):
            raise ValueError("condition routes must match condition outcomes")
        return self

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return tuple(self.routes.values())


class PersistContactFieldNode(_SourceNode):
    """A typed persistence action with explicit success/failure nodes."""

    type: Literal["persist_contact_field"]
    source_variable: NonEmptyText
    field_name: NonEmptyText = Field(
        validation_alias=AliasChoices("field_name", "destination_field")
    )
    success_node_id: NodeId
    failure_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "persist_contact_field"

    @property
    def destination(self) -> str:
        return self.field_name

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.success_node_id, self.failure_node_id)


class JoinNode(_SourceNode):
    """A structural join with concrete incoming and continuation node IDs."""

    type: Literal["join"]
    incoming_node_ids: list[NodeId] = Field(
        min_length=2,
        validation_alias=AliasChoices("incoming_node_ids", "incoming_nodes"),
    )
    next_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "join"

    @model_validator(mode="after")
    def incoming_node_ids_are_unique(self) -> JoinNode:
        if len(self.incoming_node_ids) != len(set(self.incoming_node_ids)):
            raise ValueError("join incoming node IDs must be unique")
        return self

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (*self.incoming_node_ids, self.next_node_id)


class EndNode(_SourceNode):
    """A typed terminal node; terminal meaning is not carried by a label."""

    type: Literal["end"]
    reason: NonEmptyText
    CAPABILITY_ID: ClassVar[str] = "end"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return ()


class RetryPolicyNode(_SourceNode):
    """A standalone typed retry-policy node."""

    type: Literal["retry_policy"]
    max_attempts: PositiveInteger
    messages: list[NonEmptyText] = Field(min_length=1)
    on_exhausted_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "retry_policy"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.on_exhausted_node_id,)


class NoResponseTimeoutNode(_SourceNode):
    """A standalone typed no-response timeout node."""

    type: Literal["no_response_timeout"]
    timeout_seconds: PositiveInteger
    next_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "no_response_timeout"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.next_node_id,)


class SendMediaNode(_SourceNode):
    """A typed media send with an explicit resource reference."""

    type: Literal["send_media"]
    kind: MediaKind
    resource_ref: ResourceReference = Field(
        validation_alias=AliasChoices("resource_ref", "resource")
    )
    caption: str = Field(min_length=0, max_length=10_000)
    next_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "send_media"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.next_node_id,)


class CallWebhookApiNode(_SourceNode):
    """A typed HTTP integration node with a secret reference, never a secret."""

    type: Literal["call_webhook_api"]
    integration_ref: ResourceReference = Field(
        validation_alias=AliasChoices("integration_ref", "ref")
    )
    method: HttpMethod
    url: HttpUrl
    secret_ref: SecretReference | None = Field(
        default=None,
        validation_alias=AliasChoices("secret_ref", "secret_reference"),
    )
    success_node_id: NodeId
    failure_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "call_webhook_api"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.success_node_id, self.failure_node_id)


class UpdateContactNode(_SourceNode):
    """A typed contact mutation with explicit success/failure routes."""

    type: Literal["update_contact"]
    binding: ResourceReference = Field(
        validation_alias=AliasChoices("binding", "binding_ref", "contact_binding")
    )
    fields: dict[FieldName, Any]
    success_node_id: NodeId
    failure_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "update_contact"

    @field_validator("fields")
    @classmethod
    def fields_must_not_be_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("contact fields must contain at least one field")
        return value

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.success_node_id, self.failure_node_id)


class CollectionMutationNode(_SourceNode):
    """A typed collection mutation with explicit success/failure routes."""

    type: Literal["collection_mutation"]
    collection_ref: ResourceReference = Field(
        validation_alias=AliasChoices("collection_ref", "ref")
    )
    operation: CollectionOperation
    value: Any
    success_node_id: NodeId
    failure_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "collection_mutation"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.success_node_id, self.failure_node_id)


class DelayScheduleNode(_SourceNode):
    """A typed duration/schedule node with continuation and failure routes."""

    type: Literal["delay_schedule"]
    duration: float | str | None = Field(
        validation_alias=AliasChoices("duration", "duration_seconds")
    )
    schedule: str | None = Field(validation_alias=AliasChoices("schedule", "schedule_at"))
    next_node_id: NodeId
    failure_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "delay_schedule"

    @field_validator("duration")
    @classmethod
    def duration_is_positive(cls, value: float | str | None) -> float | str | None:
        if value is None:
            return value
        if isinstance(value, bool):
            raise TypeError("delay duration must not be boolean")
        if isinstance(value, (int, float)) and value <= 0:
            raise ValueError("delay duration must be positive")
        if isinstance(value, str) and not value.strip():
            raise ValueError("delay duration cannot be blank")
        return value

    @field_validator("schedule")
    @classmethod
    def schedule_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("delay schedule cannot be blank")
        return value

    @model_validator(mode="after")
    def one_delay_mode_is_required(self) -> DelayScheduleNode:
        if self.duration is None and self.schedule is None:
            raise ValueError("delay requires duration or schedule")
        return self

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.next_node_id, self.failure_node_id)


class HandoffTicketNode(_SourceNode):
    """A typed human-handoff/ticket node with explicit outcome routes."""

    type: Literal["handoff_ticket"]
    queue: ResourceReference
    message: NonEmptyText
    success_node_id: NodeId
    failure_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "handoff_ticket"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.success_node_id, self.failure_node_id)


class EnterSubflowNode(_SourceNode):
    """A typed child-flow invocation with explicit return/failure routes."""

    type: Literal["enter_subflow"]
    subflow_ref: ResourceReference = Field(
        validation_alias=AliasChoices("subflow_ref", "ref")
    )
    inputs: dict[FieldName, Any]
    return_node_id: NodeId
    failure_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "enter_subflow"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.return_node_id, self.failure_node_id)


class TemplateHsmMessageNode(_SourceNode):
    """A typed template/HSM message with explicit continuation/failure routes."""

    type: Literal["template_hsm_message"]
    template_ref: ResourceReference = Field(
        validation_alias=AliasChoices("template_ref", "ref")
    )
    parameters: dict[FieldName, Any]
    locale: NonEmptyText
    next_node_id: NodeId
    failure_node_id: NodeId
    CAPABILITY_ID: ClassVar[str] = "template_hsm_message"

    @property
    def route_node_ids(self) -> tuple[str, ...]:
        return (self.next_node_id, self.failure_node_id)


SourceNodeUnion: TypeAlias = Annotated[
    (
        StartNode
        | SendMessageNode
        | CaptureInputNode
        | FixedChoiceNode
        | EvaluateConditionNode
        | PersistContactFieldNode
        | JoinNode
        | EndNode
        | RetryPolicyNode
        | NoResponseTimeoutNode
        | SendMediaNode
        | CallWebhookApiNode
        | UpdateContactNode
        | CollectionMutationNode
        | DelayScheduleNode
        | HandoffTicketNode
        | EnterSubflowNode
        | TemplateHsmMessageNode
    ),
    Field(discriminator="type"),
]

# Names used by callers that prefer the ledger vocabulary.  These are aliases
# to the same models, not additional union branches that could drift.
SourceNode = SourceNodeUnion
TypedSourceNode = SourceNodeUnion
SourceNodeTemplate = SourceNodeUnion
SourceNodeContract = SourceNodeUnion


class SourceNodeDocument(RootModel[SourceNodeUnion]):
    """Root adapter for validating one typed source node through ``type``."""


SOURCE_NODE_ADAPTER = TypeAdapter(SourceNodeUnion)
SOURCE_NODE_MODELS: tuple[type[_SourceNode], ...] = (
    StartNode,
    SendMessageNode,
    CaptureInputNode,
    FixedChoiceNode,
    EvaluateConditionNode,
    PersistContactFieldNode,
    JoinNode,
    EndNode,
    RetryPolicyNode,
    NoResponseTimeoutNode,
    SendMediaNode,
    CallWebhookApiNode,
    UpdateContactNode,
    CollectionMutationNode,
    DelayScheduleNode,
    HandoffTicketNode,
    EnterSubflowNode,
    TemplateHsmMessageNode,
)

SOURCE_NODE_TEMPLATE_BY_TYPE: Mapping[str, type[_SourceNode]] = {
    "start": StartNode,
    "send_message": SendMessageNode,
    "capture_input": CaptureInputNode,
    "fixed_choice": FixedChoiceNode,
    "evaluate_condition": EvaluateConditionNode,
    "persist_contact_field": PersistContactFieldNode,
    "join": JoinNode,
    "end": EndNode,
    "retry_policy": RetryPolicyNode,
    "no_response_timeout": NoResponseTimeoutNode,
    "send_media": SendMediaNode,
    "call_webhook_api": CallWebhookApiNode,
    "update_contact": UpdateContactNode,
    "collection_mutation": CollectionMutationNode,
    "delay_schedule": DelayScheduleNode,
    "handoff_ticket": HandoffTicketNode,
    "enter_subflow": EnterSubflowNode,
    "template_hsm_message": TemplateHsmMessageNode,
}
SOURCE_NODE_TEMPLATE_BY_CAPABILITY: Mapping[str, type[_SourceNode]] = {
    model.CAPABILITY_ID: model for model in SOURCE_NODE_MODELS
}
SOURCE_NODE_TYPE_TO_CAPABILITY: Mapping[str, str] = {
    node_type: model.CAPABILITY_ID
    for node_type, model in SOURCE_NODE_TEMPLATE_BY_TYPE.items()
}

# The paths are the registry's closed field vocabulary.  A source field may
# have a different concrete spelling (``next_node_id`` versus the ledger's
# ``route.next``), so this table is explicit rather than inferred from names.
SOURCE_NODE_FIELD_PATHS: Mapping[str, tuple[str, ...]] = {
    "start": (),
    "send_text_message": ("message.copy", "message.locale", "route.next"),
    "capture_user_input": (
        "input.prompt",
        "input.input_type",
        "input.save_as",
        "input.required",
        "input.validation",
        "input.route.next",
    ),
    "fixed_choice": (
        "choice.title",
        "choice.outcomes",
        "choice.stable_values",
        "choice.route.default",
        "choice.route.invalid",
        "choice.route.timeout",
    ),
    "evaluate_condition": (
        "condition.expression",
        "condition.outcomes",
        "condition.routes",
    ),
    "persist_contact_field": (
        "persistence.source_variable",
        "persistence.field_name",
        "persistence.success_route",
        "persistence.failure_route",
    ),
    "end": ("end.reason",),
    "join": ("join.incoming_routes", "join.next_route"),
    "retry_policy": (
        "retry.max_attempts",
        "retry.messages",
        "retry.on_exhausted_route",
    ),
    "no_response_timeout": ("timeout.seconds", "timeout.route"),
    "send_media": (
        "media.kind",
        "media.resource_ref",
        "media.caption",
        "route.next",
    ),
    "call_webhook_api": (
        "integration.ref",
        "integration.method",
        "integration.url",
        "route.success",
        "route.failure",
    ),
    "update_contact": (
        "contact.binding",
        "contact.fields",
        "route.success",
        "route.failure",
    ),
    "collection_mutation": (
        "collection.ref",
        "collection.operation",
        "collection.value",
        "route.success",
        "route.failure",
    ),
    "delay_schedule": (
        "delay.duration",
        "delay.schedule",
        "route.next",
        "route.failure",
    ),
    "handoff_ticket": (
        "handoff.queue",
        "handoff.message",
        "route.success",
        "route.failure",
    ),
    "enter_subflow": (
        "subflow.ref",
        "subflow.inputs",
        "route.return",
        "route.failure",
    ),
    "template_hsm_message": (
        "template.ref",
        "template.parameters",
        "template.locale",
        "route.next",
        "route.failure",
    ),
}


@dataclass(frozen=True)
class SourceNodeRegistryCoverage:
    """Exact registry/template field-set comparison for T09."""

    registry_fields: frozenset[tuple[str, str]]
    template_fields: frozenset[tuple[str, str]]
    missing_fields: frozenset[tuple[str, str]]
    extra_fields: frozenset[tuple[str, str]]
    missing_capabilities: frozenset[str]
    extra_capabilities: frozenset[str]

    @property
    def passed(self) -> bool:
        return not (
            self.missing_fields
            or self.extra_fields
            or self.missing_capabilities
            or self.extra_capabilities
        )


def _registry_field_set() -> frozenset[tuple[str, str]]:
    return frozenset(
        (capability_id, field_path)
        for capability_id, field_paths in SOURCE_NODE_FIELD_PATHS.items()
        for field_path in field_paths
    )


def _template_field_set() -> frozenset[tuple[str, str]]:
    return frozenset(
        (capability.id, field_path)
        for capability in CANONICAL_CAPABILITIES
        if capability.id in SOURCE_NODE_FIELD_PATHS
        for field_path in capability.required_configuration
    )


def source_node_registry_coverage() -> SourceNodeRegistryCoverage:
    """Return the deterministic node-template/registry coverage comparison."""

    registry_capabilities = {
        capability.id for capability in CANONICAL_CAPABILITIES if capability.id in SOURCE_NODE_FIELD_PATHS
    }
    template_capabilities = set(SOURCE_NODE_FIELD_PATHS)
    registry_fields = _template_field_set()
    template_fields = _registry_field_set()
    return SourceNodeRegistryCoverage(
        registry_fields=registry_fields,
        template_fields=template_fields,
        missing_fields=registry_fields - template_fields,
        extra_fields=template_fields - registry_fields,
        missing_capabilities=frozenset(registry_capabilities - template_capabilities),
        extra_capabilities=frozenset(template_capabilities - registry_capabilities),
    )


def validate_source_node_registry() -> SourceNodeRegistryCoverage:
    """Fail closed if source templates and registered ledger fields disagree."""

    expected_capabilities = {
        capability.id
        for capability in CANONICAL_CAPABILITIES
        if capability.id in SOURCE_NODE_FIELD_PATHS
    }
    mapped_capabilities = {
        model.CAPABILITY_ID for model in SOURCE_NODE_MODELS
    }
    if mapped_capabilities != expected_capabilities:
        raise SourceNodeRegistryError(
            "source node template capability mismatch: "
            f"missing={sorted(expected_capabilities - mapped_capabilities)!r}, "
            f"extra={sorted(mapped_capabilities - expected_capabilities)!r}"
        )
    if set(SOURCE_NODE_TEMPLATE_BY_TYPE) != set(SOURCE_NODE_TYPE_TO_CAPABILITY):
        raise SourceNodeRegistryError("source node type map is inconsistent")
    for node_type, model in SOURCE_NODE_TEMPLATE_BY_TYPE.items():
        if SOURCE_NODE_TYPE_TO_CAPABILITY[node_type] != model.CAPABILITY_ID:
            raise SourceNodeRegistryError(f"source node type map disagrees for {node_type}")
        if CAPABILITY_REGISTRY.get(model.CAPABILITY_ID) is None:
            raise SourceNodeRegistryError(
                f"source node {node_type} references unknown capability {model.CAPABILITY_ID}"
            )
    coverage = source_node_registry_coverage()
    if not coverage.passed:
        raise SourceNodeRegistryError(
            "source node template/registry field mismatch: "
            f"missing={sorted(coverage.missing_fields)!r}, "
            f"extra={sorted(coverage.extra_fields)!r}"
        )
    return coverage


def parse_source_node(
    value: SourceNodeUnion | Mapping[str, Any] | str | bytes | bytearray,
) -> _SourceNode:
    """Parse one source node through the closed ``type`` discriminator."""

    if isinstance(value, (str, bytes, bytearray)):
        return SOURCE_NODE_ADAPTER.validate_json(value)
    return SOURCE_NODE_ADAPTER.validate_python(value)


def serialize_source_node(value: SourceNodeUnion | Mapping[str, Any]) -> str:
    """Serialize one typed source node deterministically."""

    node = parse_source_node(value)
    return json.dumps(
        node.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def source_node_schema_json() -> str:
    """Return the deterministic JSON Schema for the closed node union."""

    return json.dumps(
        SOURCE_NODE_ADAPTER.json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def source_node_schema_hash() -> str:
    """Return the SHA-256 hash of the closed node-union JSON Schema."""

    return hashlib.sha256(source_node_schema_json().encode("utf-8")).hexdigest()


canonical_source_node_schema_json = source_node_schema_json
canonical_source_node_schema_hash = source_node_schema_hash
parse_typed_source_node = parse_source_node
serialize_typed_source_node = serialize_source_node
validate_source_node_template_registry = validate_source_node_registry


# Friendly aliases retain both the ledger and source vocabulary without adding
# a second set of classes or discriminator branches.
SendTextMessageNode = SendMessageNode
CaptureUserInputNode = CaptureInputNode
EvaluateNode = EvaluateConditionNode
PersistValueNode = PersistContactFieldNode
PersistenceNode = PersistContactFieldNode
RetryNode = RetryPolicyNode
NoResponseNode = NoResponseTimeoutNode
MediaNode = SendMediaNode
WebhookApiNode = CallWebhookApiNode


validate_source_node_registry()


__all__ = [
    "SOURCE_NODE_ADAPTER",
    "SOURCE_NODE_FIELD_PATHS",
    "SOURCE_NODE_MODELS",
    "SOURCE_NODE_SCHEMA_VERSION",
    "SOURCE_NODE_TEMPLATE_BY_CAPABILITY",
    "SOURCE_NODE_TEMPLATE_BY_TYPE",
    "SOURCE_NODE_TEMPLATE_VERSION",
    "SOURCE_NODE_TYPE_TO_CAPABILITY",
    "CallWebhookApiNode",
    "CaptureInputNode",
    "CaptureUserInputNode",
    "ChoiceNodeOutcome",
    "CollectionMutationNode",
    "CollectionOperation",
    "DelayScheduleNode",
    "EndNode",
    "EnterSubflowNode",
    "EvaluateConditionNode",
    "EvaluateNode",
    "FixedChoiceNode",
    "HandoffTicketNode",
    "HttpMethod",
    "JoinNode",
    "MediaKind",
    "MediaNode",
    "NoResponseNode",
    "NoResponseTimeoutNode",
    "NoResponseTimeoutNodeSpec",
    "NodeId",
    "PersistContactFieldNode",
    "PersistValueNode",
    "PersistenceNode",
    "RetryNode",
    "RetryPolicyNode",
    "RetryPolicyNodeSpec",
    "SendMediaNode",
    "SendMessageNode",
    "SendTextMessageNode",
    "SourceNode",
    "SourceNodeContract",
    "SourceNodeContractError",
    "SourceNodeDocument",
    "SourceNodeRegistryCoverage",
    "SourceNodeRegistryError",
    "SourceNodeTemplate",
    "SourceNodeUnion",
    "SourceReference",
    "StartNode",
    "TemplateHsmMessageNode",
    "TypedSourceNode",
    "UpdateContactNode",
    "WebhookApiNode",
    "canonical_source_node_schema_hash",
    "canonical_source_node_schema_json",
    "parse_source_node",
    "parse_typed_source_node",
    "serialize_source_node",
    "serialize_typed_source_node",
    "source_node_registry_coverage",
    "source_node_schema_hash",
    "source_node_schema_json",
    "validate_source_node_registry",
    "validate_source_node_template_registry",
]
