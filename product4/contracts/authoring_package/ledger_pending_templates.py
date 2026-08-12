"""Typed requirements for capabilities that are authoring-representable only.

T06 owns the compiler-pending portion of the requirements-ledger contract.  A
requirement keeps the common T04 envelope and puts its capability-specific
contract in a typed ``payload``.  The union below is deliberately closed: a
new capability must be added to the registry, this module, and its tests before
it can be represented here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    RootModel,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .capabilities import (
    CAPABILITY_REGISTRY,
    CapabilityStatus,
    canonical_registry_hash,
)
from .ledger_contracts import (
    RequirementEnvelope,
    RequirementId,
    RequirementsLedger,
    serialize_ledger_json,
)

PENDING_TEMPLATE_SCHEMA_VERSION = "ledger-pending-templates-1.0"
PENDING_LEDGER_TEMPLATE_VERSION = "requirements-ledger-pending-1.0"

COMPILER_PENDING_CAPABILITIES: tuple[str, ...] = (
    "send_media",
    "call_webhook_api",
    "update_contact",
    "collection_mutation",
    "delay_schedule",
    "handoff_ticket",
    "enter_subflow",
    "template_hsm_message",
)

_REFERENCE_PATTERN = r"^[a-z][a-z0-9_-]{0,63}:[A-Za-z0-9][A-Za-z0-9._~:/-]{0,255}$"
_SECRET_REFERENCE_PATTERN = r"^secret:[A-Za-z0-9][A-Za-z0-9._~:/-]{0,255}$"
_FIELD_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,127}$"

ResourceReference: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=320,
        pattern=_REFERENCE_PATTERN,
    ),
]
SecretReference: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=263,
        pattern=_SECRET_REFERENCE_PATTERN,
    ),
]
FieldName: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=_FIELD_NAME_PATTERN,
    ),
]


class _StrictModel(BaseModel):
    """Strict base for capability payloads and typed gate results."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class MediaKind(str, Enum):
    AUDIO = "audio"
    DOCUMENT = "document"
    IMAGE = "image"
    VIDEO = "video"
    STICKER = "sticker"
    FILE = "file"


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"

    @classmethod
    def _missing_(cls, value: object) -> HttpMethod | None:
        if isinstance(value, str):
            normalized = value.strip().upper()
            return next((member for member in cls if member.value == normalized), None)
        return None


class CollectionOperation(str, Enum):
    ADD = "add"
    APPEND = "append"
    CLEAR = "clear"
    REMOVE = "remove"
    REPLACE = "replace"
    SET = "set"
    UPSERT = "upsert"


class MediaSpec(_StrictModel):
    """The explicit media resource and representation to send."""

    kind: MediaKind
    resource_ref: ResourceReference = Field(
        validation_alias=AliasChoices("resource_ref", "resource")
    )
    caption: str = Field(min_length=0, max_length=10_000)


class NextRoute(_StrictModel):
    """One explicit success/continuation route in requirement-ID space."""

    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "next")
    )


class SuccessFailureRoutes(_StrictModel):
    """Routes required for an operation that can succeed or fail."""

    success_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("success_requirement_id", "success")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure")
    )


class NextFailureRoutes(_StrictModel):
    """Continuation plus explicit failure route for scheduled work."""

    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "next")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure")
    )


class ReturnFailureRoutes(_StrictModel):
    """Return and failure routes for a nested flow invocation."""

    return_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("return_requirement_id", "return")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure")
    )


class WebhookIntegrationSpec(_StrictModel):
    """HTTP integration configuration without embedded credentials."""

    ref: ResourceReference = Field(
        validation_alias=AliasChoices("ref", "integration_ref")
    )
    method: HttpMethod
    url: HttpUrl
    secret_ref: SecretReference | None = Field(
        default=None,
        validation_alias=AliasChoices("secret_ref", "secret_reference"),
    )


class ContactUpdateSpec(_StrictModel):
    """An explicit contact binding and field/value update map."""

    binding: ResourceReference = Field(
        validation_alias=AliasChoices("binding", "binding_ref", "contact_binding")
    )
    fields: dict[FieldName, Any]

    @field_validator("fields")
    @classmethod
    def fields_must_not_be_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("contact.fields must contain at least one field")
        return value


class CollectionMutationSpec(_StrictModel):
    """An explicit collection resource, operation, and mutation value."""

    ref: ResourceReference = Field(
        validation_alias=AliasChoices("ref", "collection_ref")
    )
    operation: CollectionOperation
    value: Any


class DelaySpec(_StrictModel):
    """A duration or schedule, with both keys explicit in the ledger."""

    duration: float | str | None = Field(
        validation_alias=AliasChoices("duration", "duration_seconds")
    )
    schedule: str | None = Field(
        validation_alias=AliasChoices("schedule", "schedule_at")
    )

    @field_validator("duration")
    @classmethod
    def duration_must_be_positive(cls, value: float | str | None) -> float | str | None:
        if value is None:
            return value
        if isinstance(value, bool):
            raise TypeError("delay.duration must not be boolean")
        if isinstance(value, (int, float)) and value <= 0:
            raise ValueError("delay.duration must be positive")
        if isinstance(value, str) and not value.strip():
            raise ValueError("delay.duration cannot be blank")
        return value

    @field_validator("schedule")
    @classmethod
    def schedule_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("delay.schedule cannot be blank")
        return value

    @model_validator(mode="after")
    def one_delay_mode_is_required(self) -> DelaySpec:
        if self.duration is None and self.schedule is None:
            raise ValueError("delay requires duration or schedule")
        return self


class HandoffSpec(_StrictModel):
    """Human-handoff queue and user-visible handoff message."""

    queue: ResourceReference
    message: str = Field(min_length=1, max_length=10_000)


class SubflowSpec(_StrictModel):
    """A referenced child flow and its explicitly named inputs."""

    ref: ResourceReference = Field(
        validation_alias=AliasChoices("ref", "subflow_ref")
    )
    inputs: dict[FieldName, Any]


class TemplateHsmSpec(_StrictModel):
    """A referenced template with explicit parameters and locale."""

    ref: ResourceReference = Field(
        validation_alias=AliasChoices("ref", "template_ref")
    )
    parameters: dict[FieldName, Any]
    locale: str = Field(min_length=2, max_length=35)


def _flatten_payload_sections(value: Any, section_name: str) -> Any:
    """Accept the design-note nested spelling while storing flat typed fields."""

    if not isinstance(value, Mapping):
        return value
    candidate = dict(value)
    nested_section = candidate.pop(section_name, None)
    nested_route = candidate.pop("route", None)
    for nested in (nested_section, nested_route):
        if not isinstance(nested, Mapping):
            continue
        for field_name, field_value in nested.items():
            if field_name in candidate:
                raise ValueError(f"duplicate payload field: {field_name}")
            candidate[field_name] = field_value
    return candidate


class SendMediaPayload(_StrictModel):
    """Execution details for a media send and its continuation route."""

    kind: MediaKind
    resource_ref: ResourceReference = Field(
        validation_alias=AliasChoices("resource_ref", "resource")
    )
    caption: str = Field(min_length=0, max_length=10_000)
    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "next_route", "next")
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_design_note_shape(cls, value: Any) -> Any:
        return _flatten_payload_sections(value, "media")

    @property
    def media(self) -> MediaSpec:
        return MediaSpec(kind=self.kind, resource_ref=self.resource_ref, caption=self.caption)

    @property
    def route(self) -> NextRoute:
        return NextRoute(next_requirement_id=self.next_requirement_id)

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.next_requirement_id,)


class CallWebhookApiPayload(_StrictModel):
    """HTTP integration details with explicit success and failure routes."""

    integration_ref: ResourceReference = Field(
        validation_alias=AliasChoices("integration_ref", "ref")
    )
    method: HttpMethod
    url: HttpUrl
    secret_ref: SecretReference | None = Field(
        default=None,
        validation_alias=AliasChoices("secret_ref", "secret_reference"),
    )
    success_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("success_requirement_id", "success_route", "success")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure_route", "failure")
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_design_note_shape(cls, value: Any) -> Any:
        return _flatten_payload_sections(value, "integration")

    @property
    def integration(self) -> WebhookIntegrationSpec:
        return WebhookIntegrationSpec(
            ref=self.integration_ref,
            method=self.method,
            url=self.url,
            secret_ref=self.secret_ref,
        )

    @property
    def route(self) -> SuccessFailureRoutes:
        return SuccessFailureRoutes(
            success_requirement_id=self.success_requirement_id,
            failure_requirement_id=self.failure_requirement_id,
        )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.success_requirement_id, self.failure_requirement_id)


class UpdateContactPayload(_StrictModel):
    """Contact binding, updates, and explicit success/failure routes."""

    binding: ResourceReference = Field(
        validation_alias=AliasChoices("binding", "binding_ref", "contact_binding")
    )
    fields: dict[FieldName, Any]
    success_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("success_requirement_id", "success_route", "success")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure_route", "failure")
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_design_note_shape(cls, value: Any) -> Any:
        return _flatten_payload_sections(value, "contact")

    @field_validator("fields")
    @classmethod
    def fields_must_not_be_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("contact.fields must contain at least one field")
        return value

    @property
    def contact(self) -> ContactUpdateSpec:
        return ContactUpdateSpec(binding=self.binding, fields=self.fields)

    @property
    def route(self) -> SuccessFailureRoutes:
        return SuccessFailureRoutes(
            success_requirement_id=self.success_requirement_id,
            failure_requirement_id=self.failure_requirement_id,
        )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.success_requirement_id, self.failure_requirement_id)


class CollectionMutationPayload(_StrictModel):
    """Collection resource, operation, value, and explicit routes."""

    collection_ref: ResourceReference = Field(
        validation_alias=AliasChoices("collection_ref", "ref")
    )
    operation: CollectionOperation
    value: Any
    success_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("success_requirement_id", "success_route", "success")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure_route", "failure")
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_design_note_shape(cls, value: Any) -> Any:
        return _flatten_payload_sections(value, "collection")

    @property
    def collection(self) -> CollectionMutationSpec:
        return CollectionMutationSpec(
            ref=self.collection_ref,
            operation=self.operation,
            value=self.value,
        )

    @property
    def route(self) -> SuccessFailureRoutes:
        return SuccessFailureRoutes(
            success_requirement_id=self.success_requirement_id,
            failure_requirement_id=self.failure_requirement_id,
        )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.success_requirement_id, self.failure_requirement_id)


class DelaySchedulePayload(_StrictModel):
    """Delay duration or schedule with continuation and failure routes."""

    duration: float | str | None = Field(
        validation_alias=AliasChoices("duration", "duration_seconds")
    )
    schedule: str | None = Field(
        validation_alias=AliasChoices("schedule", "schedule_at")
    )
    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "next_route", "next")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure_route", "failure")
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_design_note_shape(cls, value: Any) -> Any:
        return _flatten_payload_sections(value, "delay")

    @model_validator(mode="after")
    def one_delay_mode_is_required(self) -> DelaySchedulePayload:
        if self.duration is None and self.schedule is None:
            raise ValueError("delay requires duration or schedule")
        return self

    @property
    def delay(self) -> DelaySpec:
        return DelaySpec(duration=self.duration, schedule=self.schedule)

    @property
    def route(self) -> NextFailureRoutes:
        return NextFailureRoutes(
            next_requirement_id=self.next_requirement_id,
            failure_requirement_id=self.failure_requirement_id,
        )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.next_requirement_id, self.failure_requirement_id)


class HandoffTicketPayload(_StrictModel):
    """Handoff queue/message with explicit success and failure routes."""

    queue: ResourceReference
    message: str = Field(min_length=1, max_length=10_000)
    success_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("success_requirement_id", "success_route", "success")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure_route", "failure")
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_design_note_shape(cls, value: Any) -> Any:
        return _flatten_payload_sections(value, "handoff")

    @property
    def handoff(self) -> HandoffSpec:
        return HandoffSpec(queue=self.queue, message=self.message)

    @property
    def route(self) -> SuccessFailureRoutes:
        return SuccessFailureRoutes(
            success_requirement_id=self.success_requirement_id,
            failure_requirement_id=self.failure_requirement_id,
        )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.success_requirement_id, self.failure_requirement_id)


class EnterSubflowPayload(_StrictModel):
    """Child-flow reference/inputs with explicit return and failure routes."""

    subflow_ref: ResourceReference = Field(
        validation_alias=AliasChoices("subflow_ref", "ref")
    )
    inputs: dict[FieldName, Any]
    return_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("return_requirement_id", "return_route", "return")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure_route", "failure")
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_design_note_shape(cls, value: Any) -> Any:
        return _flatten_payload_sections(value, "subflow")

    @property
    def subflow(self) -> SubflowSpec:
        return SubflowSpec(ref=self.subflow_ref, inputs=self.inputs)

    @property
    def route(self) -> ReturnFailureRoutes:
        return ReturnFailureRoutes(
            return_requirement_id=self.return_requirement_id,
            failure_requirement_id=self.failure_requirement_id,
        )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.return_requirement_id, self.failure_requirement_id)


class TemplateHsmMessagePayload(_StrictModel):
    """Template reference/parameters/locale with explicit routes."""

    template_ref: ResourceReference = Field(
        validation_alias=AliasChoices("template_ref", "ref")
    )
    parameters: dict[FieldName, Any]
    locale: str = Field(min_length=2, max_length=35)
    next_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("next_requirement_id", "next_route", "next")
    )
    failure_requirement_id: RequirementId = Field(
        validation_alias=AliasChoices("failure_requirement_id", "failure_route", "failure")
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_design_note_shape(cls, value: Any) -> Any:
        return _flatten_payload_sections(value, "template")

    @property
    def template(self) -> TemplateHsmSpec:
        return TemplateHsmSpec(
            ref=self.template_ref,
            parameters=self.parameters,
            locale=self.locale,
        )

    @property
    def route(self) -> NextFailureRoutes:
        return NextFailureRoutes(
            next_requirement_id=self.next_requirement_id,
            failure_requirement_id=self.failure_requirement_id,
        )

    @property
    def route_requirement_ids(self) -> tuple[str, ...]:
        return (self.next_requirement_id, self.failure_requirement_id)


# Nested names are retained as read-only compatibility aliases for callers
# that use the design-note terminology; the canonical payloads are flat like
# the T05 core templates.
MediaPayload = SendMediaPayload
WebhookApiPayload = CallWebhookApiPayload


class _CompilerPendingRequirement(RequirementEnvelope):
    """Common typed-envelope behavior for all T06 requirements."""

    @model_validator(mode="after")
    def registry_marks_capability_pending(self) -> _CompilerPendingRequirement:
        definition = CAPABILITY_REGISTRY.get(self.capability)
        if definition is None or self.capability not in COMPILER_PENDING_CAPABILITIES:
            raise ValueError(
                f"{self.capability!r} is not a T06 compiler-pending capability"
            )
        if not definition.compiler_pending:
            raise ValueError(f"{self.capability!r} is not compiler pending in the registry")
        if definition.end_to_end_enabled:
            raise ValueError(
                f"compiler-pending capability {self.capability!r} cannot be end-to-end enabled"
            )
        return self


class SendMediaRequirement(_CompilerPendingRequirement):
    capability: Literal["send_media"]
    payload: MediaPayload


class CallWebhookApiRequirement(_CompilerPendingRequirement):
    capability: Literal["call_webhook_api"]
    payload: WebhookApiPayload


class UpdateContactRequirement(_CompilerPendingRequirement):
    capability: Literal["update_contact"]
    payload: UpdateContactPayload


class CollectionMutationRequirement(_CompilerPendingRequirement):
    capability: Literal["collection_mutation"]
    payload: CollectionMutationPayload


class DelayScheduleRequirement(_CompilerPendingRequirement):
    capability: Literal["delay_schedule"]
    payload: DelaySchedulePayload


class HandoffTicketRequirement(_CompilerPendingRequirement):
    capability: Literal["handoff_ticket"]
    payload: HandoffTicketPayload


class EnterSubflowRequirement(_CompilerPendingRequirement):
    capability: Literal["enter_subflow"]
    payload: EnterSubflowPayload


class TemplateHsmMessageRequirement(_CompilerPendingRequirement):
    capability: Literal["template_hsm_message"]
    payload: TemplateHsmMessagePayload


CompilerPendingLedgerRequirement: TypeAlias = Annotated[
    SendMediaRequirement
    | CallWebhookApiRequirement
    | UpdateContactRequirement
    | CollectionMutationRequirement
    | DelayScheduleRequirement
    | HandoffTicketRequirement
    | EnterSubflowRequirement
    | TemplateHsmMessageRequirement,
    Field(discriminator="capability"),
]

# Explicit aliases keep the union discoverable under the terms used by the
# implementation plan and by downstream contract callers.
CompilerPendingRequirement: TypeAlias = CompilerPendingLedgerRequirement
LedgerPendingRequirement: TypeAlias = CompilerPendingLedgerRequirement
PendingLedgerRequirement: TypeAlias = CompilerPendingLedgerRequirement
CompilerPendingLedgerRequirementUnion: TypeAlias = CompilerPendingLedgerRequirement
PendingLedgerRequirementUnion: TypeAlias = CompilerPendingLedgerRequirement
PendingRequirementUnion: TypeAlias = CompilerPendingLedgerRequirement

PENDING_REQUIREMENT_ADAPTER = TypeAdapter(CompilerPendingLedgerRequirement)

PENDING_REQUIREMENT_MODELS: tuple[type[_CompilerPendingRequirement], ...] = (
    SendMediaRequirement,
    CallWebhookApiRequirement,
    UpdateContactRequirement,
    CollectionMutationRequirement,
    DelayScheduleRequirement,
    HandoffTicketRequirement,
    EnterSubflowRequirement,
    TemplateHsmMessageRequirement,
)

def _pending_model_capability(model: type[_CompilerPendingRequirement]) -> str:
    literal_schema = model.model_json_schema()["properties"]["capability"]
    return literal_schema["const"]


PENDING_TEMPLATE_BY_CAPABILITY = {
    _pending_model_capability(model): model for model in PENDING_REQUIREMENT_MODELS
}


class PendingLedgerRequirementDocument(RootModel[CompilerPendingLedgerRequirement]):
    """Root-model adapter for callers needing model validation on the union."""


class PendingRequirementsLedger(RequirementsLedger):
    """Common ledger envelope specialized to T06 requirements."""

    requirements: list[CompilerPendingLedgerRequirement] = Field(min_length=1)


def validate_pending_template_registry() -> None:
    """Verify that T06 templates match the registry's pending capability set."""

    template_capabilities = set(PENDING_TEMPLATE_BY_CAPABILITY)
    expected_capabilities = set(COMPILER_PENDING_CAPABILITIES)
    if template_capabilities != expected_capabilities:
        raise ValueError(
            "T06 template/registry capability mismatch: "
            f"missing={sorted(expected_capabilities - template_capabilities)!r}, "
            f"extra={sorted(template_capabilities - expected_capabilities)!r}"
        )
    for capability_id in COMPILER_PENDING_CAPABILITIES:
        capability = CAPABILITY_REGISTRY[capability_id]
        if capability.authoring_status is not CapabilityStatus.ENABLED:
            raise ValueError(f"{capability_id} is not authoring-representable")
        if not capability.compiler_pending or capability.end_to_end_enabled:
            raise ValueError(f"{capability_id} has an invalid compiler-pending status")


class UnsupportedCapabilityResult(_StrictModel):
    """Typed fail-closed result for a pending capability."""

    supported: Literal[False] = False
    code: Literal["CAPABILITY_COMPILER_PENDING"] = "CAPABILITY_COMPILER_PENDING"
    capability: str
    status: Literal["compiler_pending"] = "compiler_pending"
    reason: str = Field(min_length=1, max_length=10_000)
    capability_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def issue_code(self) -> str:
        """Compatibility spelling for validators that call this an issue."""

        return self.code


def parse_pending_requirement(
    value: CompilerPendingLedgerRequirement | Mapping[str, Any] | str | bytes | bytearray,
) -> CompilerPendingLedgerRequirement:
    """Parse one T06 requirement through the closed capability union."""

    if isinstance(value, (str, bytes, bytearray)):
        return PENDING_REQUIREMENT_ADAPTER.validate_json(value)
    return PENDING_REQUIREMENT_ADAPTER.validate_python(value)


def serialize_pending_requirement(
    value: CompilerPendingLedgerRequirement,
) -> str:
    """Serialize one typed pending requirement deterministically."""

    requirement = parse_pending_requirement(value)
    return json.dumps(
        requirement.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_pending_ledger_json(value: str | bytes | bytearray) -> PendingRequirementsLedger:
    """Parse a ledger whose requirements are all T06 typed requirements."""

    return PendingRequirementsLedger.model_validate_json(value)


def serialize_pending_ledger_json(
    value: PendingRequirementsLedger | Mapping[str, Any],
) -> str:
    """Serialize a typed T06 ledger deterministically."""

    ledger = (
        value
        if isinstance(value, PendingRequirementsLedger)
        else PendingRequirementsLedger.model_validate(value)
    )
    return serialize_ledger_json(ledger)


def pending_requirement_schema_json() -> str:
    """Return the deterministic JSON Schema for the T06 union."""

    return json.dumps(
        PENDING_REQUIREMENT_ADAPTER.json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pending_requirement_schema_hash() -> str:
    """Return a stable hash for the T06 union schema."""

    return hashlib.sha256(pending_requirement_schema_json().encode("utf-8")).hexdigest()


def canonical_pending_schema_json() -> str:
    """Return the deterministic JSON Schema for a typed T06 ledger."""

    return json.dumps(
        PendingRequirementsLedger.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_pending_schema_hash() -> str:
    """Return a stable hash for the typed T06 ledger schema."""

    return hashlib.sha256(canonical_pending_schema_json().encode("utf-8")).hexdigest()


def compiler_pending_result(
    value: CompilerPendingLedgerRequirement | str,
) -> UnsupportedCapabilityResult:
    """Return a typed unsupported result without attempting compilation."""

    capability_id = value.capability if isinstance(value, RequirementEnvelope) else value
    if capability_id not in COMPILER_PENDING_CAPABILITIES:
        raise ValueError(f"{capability_id!r} is not a T06 compiler-pending capability")
    capability = CAPABILITY_REGISTRY[capability_id]
    reason = capability.target_limitations[0]
    return UnsupportedCapabilityResult(
        capability=capability_id,
        reason=reason,
        capability_profile_hash=canonical_registry_hash(),
    )


def validate_end_to_end_capability(
    value: CompilerPendingLedgerRequirement | str,
) -> UnsupportedCapabilityResult:
    """Alias used by the T06 integration gate and later capability validators."""

    return compiler_pending_result(value)


validate_pending_template_registry()


__all__ = [
    "COMPILER_PENDING_CAPABILITIES",
    "PENDING_LEDGER_TEMPLATE_VERSION",
    "PENDING_REQUIREMENT_ADAPTER",
    "PENDING_REQUIREMENT_MODELS",
    "PENDING_TEMPLATE_BY_CAPABILITY",
    "PENDING_TEMPLATE_SCHEMA_VERSION",
    "CallWebhookApiPayload",
    "CallWebhookApiRequirement",
    "CollectionMutationPayload",
    "CollectionMutationRequirement",
    "CollectionMutationSpec",
    "CollectionOperation",
    "CompilerPendingLedgerRequirement",
    "CompilerPendingLedgerRequirementUnion",
    "CompilerPendingRequirement",
    "ContactUpdateSpec",
    "DelaySchedulePayload",
    "DelayScheduleRequirement",
    "DelaySpec",
    "EnterSubflowPayload",
    "EnterSubflowRequirement",
    "FieldName",
    "HandoffSpec",
    "HandoffTicketPayload",
    "HandoffTicketRequirement",
    "HttpMethod",
    "LedgerPendingRequirement",
    "MediaKind",
    "MediaPayload",
    "MediaSpec",
    "NextFailureRoutes",
    "NextRoute",
    "PendingLedgerRequirement",
    "PendingLedgerRequirementDocument",
    "PendingLedgerRequirementUnion",
    "PendingRequirementUnion",
    "PendingRequirementsLedger",
    "ResourceReference",
    "ReturnFailureRoutes",
    "SecretReference",
    "SendMediaPayload",
    "SendMediaRequirement",
    "SubflowSpec",
    "SuccessFailureRoutes",
    "TemplateHsmMessagePayload",
    "TemplateHsmMessageRequirement",
    "TemplateHsmSpec",
    "UnsupportedCapabilityResult",
    "UpdateContactPayload",
    "UpdateContactRequirement",
    "WebhookApiPayload",
    "WebhookIntegrationSpec",
    "canonical_pending_schema_hash",
    "canonical_pending_schema_json",
    "compiler_pending_result",
    "parse_pending_ledger_json",
    "parse_pending_requirement",
    "pending_requirement_schema_hash",
    "pending_requirement_schema_json",
    "serialize_pending_ledger_json",
    "serialize_pending_requirement",
    "validate_end_to_end_capability",
    "validate_pending_template_registry",
]
