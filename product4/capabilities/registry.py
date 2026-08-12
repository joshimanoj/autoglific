from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

REGISTRY_VERSION = "capability-registry.v2-typed-provider-fields"
INPUT_TYPES = ("text", "number", "email", "phone")


class AcquisitionPolicy(str, Enum):
    SUPPLIED = "supplied"
    DERIVED = "derived"
    GENERATED = "generated"
    DEFAULTED = "defaulted"
    USER_REQUIRED = "user-required"


class FieldValueKind(str, Enum):
    STRING = "string"
    BOOLEAN = "boolean"
    INPUT_TYPE = "input_type"
    VALIDATION = "validation"
    CHOICE_OPTIONS = "choice_options"


class ChoiceOptionValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: Annotated[str, Field(min_length=1)]
    value: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class ValidationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    allowed_values: list[str] | None = None


_FIELD_VALUE_ADAPTERS = {
    FieldValueKind.STRING: TypeAdapter(Annotated[str, Field(min_length=1)]),
    FieldValueKind.BOOLEAN: TypeAdapter(bool),
    FieldValueKind.INPUT_TYPE: TypeAdapter(Literal["text", "number", "email", "phone"]),
    FieldValueKind.VALIDATION: TypeAdapter(ValidationValue),
    FieldValueKind.CHOICE_OPTIONS: TypeAdapter(
        Annotated[list[ChoiceOptionValue], Field(min_length=2, max_length=10)]
    ),
}


class CapabilityField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    policy: AcquisitionPolicy
    question: str | None = None
    default: Any = None
    answer_type: Literal["text", "boolean", "options", "json"] = "text"
    options: tuple[str, ...] = ()
    value_kind: FieldValueKind = FieldValueKind.STRING

    @model_validator(mode="after")
    def question_matches_policy(self) -> "CapabilityField":
        if self.policy is AcquisitionPolicy.USER_REQUIRED and not self.question:
            raise ValueError("user-required fields need a question")
        if self.policy not in {AcquisitionPolicy.USER_REQUIRED, AcquisitionPolicy.DERIVED} and self.question:
            raise ValueError("only user-required or derived-fallback fields may define questions")
        return self


class ExitDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["linear", "dynamic", "terminal"]
    source_field: str | None = None


class CapabilityDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    node_type: str
    aliases: tuple[str, ...]
    fields: tuple[CapabilityField, ...]
    exit: ExitDefinition
    engine1_type: str
    engine2_type: str
    engine3_primitive: str


def _field(path: str, policy: AcquisitionPolicy, **kwargs: Any) -> CapabilityField:
    return CapabilityField(path=path, policy=policy, **kwargs)


def _inline_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.pop("$defs", {})

    def resolve(value: Any) -> Any:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                return resolve(json.loads(json.dumps(definitions[reference.rsplit('/', 1)[-1]])))
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    return resolve(schema)


def field_value_json_schema(field: CapabilityField) -> dict[str, Any]:
    """Canonical provider JSON type generated from the registry's Pydantic adapter."""
    return _inline_schema_refs(_FIELD_VALUE_ADAPTERS[field.value_kind].json_schema())


def validate_registry_field_value(field: CapabilityField, value: Any) -> Any:
    """Validate and normalize a provider value with the same registry adapter."""
    parsed = _FIELD_VALUE_ADAPTERS[field.value_kind].validate_python(value, strict=True)
    if isinstance(parsed, BaseModel):
        return parsed.model_dump(mode="json", exclude_none=True)
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], BaseModel):
        return [item.model_dump(mode="json") for item in parsed]
    return parsed


CAPABILITIES = (
    CapabilityDefinition(
        id="send_text_message", node_type="send_message",
        aliases=("send", "message", "tell", "say", "welcome", "thank", "acknowledge", "confirm", "introduce"),
        fields=(
            _field("copy", AcquisitionPolicy.USER_REQUIRED, question="What exact message should be sent?"),
            _field("locale", AcquisitionPolicy.DEFAULTED, default="en"),
        ),
        exit=ExitDefinition(kind="linear"), engine1_type="send_message",
        engine2_type="send_message", engine3_primitive="send_text_message",
    ),
    CapabilityDefinition(
        id="capture_user_input", node_type="capture_input",
        aliases=("capture", "collect", "ask", "input", "reply", "response", "obtain"),
        fields=(
            _field("prompt", AcquisitionPolicy.USER_REQUIRED, question="What exact prompt should ask for the input?"),
            _field("input_type", AcquisitionPolicy.USER_REQUIRED, question="Which input type should be used: text, number, email or phone?", answer_type="options", options=INPUT_TYPES, value_kind=FieldValueKind.INPUT_TYPE),
            _field("save_as", AcquisitionPolicy.USER_REQUIRED, question="What flow-variable name should store the answer?"),
            _field("required", AcquisitionPolicy.DEFAULTED, default=True, value_kind=FieldValueKind.BOOLEAN),
            _field("validation", AcquisitionPolicy.USER_REQUIRED, question="What validation constraints should apply? Use an empty object for none.", answer_type="json", value_kind=FieldValueKind.VALIDATION),
        ),
        exit=ExitDefinition(kind="linear"), engine1_type="capture_input",
        engine2_type="ask_input", engine3_primitive="wait_for_text_response",
    ),
    CapabilityDefinition(
        id="fixed_choice", node_type="fixed_choice",
        aliases=("choice", "choose", "select", "options", "menu", "rate", "whether"),
        fields=(
            _field("title", AcquisitionPolicy.USER_REQUIRED, question="What exact choice prompt should be shown?"),
            _field("options", AcquisitionPolicy.USER_REQUIRED, question="Provide choice options as label=value pairs.", value_kind=FieldValueKind.CHOICE_OPTIONS),
        ),
        exit=ExitDefinition(kind="dynamic", source_field="options"), engine1_type="fixed_choice",
        engine2_type="ask_choice", engine3_primitive="exact_categorical_branch",
    ),
    CapabilityDefinition(
        id="persist_contact_field", node_type="persist_contact_field",
        aliases=("persist", "save", "store", "contact field", "record"),
        fields=(
            _field("source_variable", AcquisitionPolicy.DERIVED, question="Which flow variable should be saved?"),
            _field("field_name", AcquisitionPolicy.USER_REQUIRED, question="Which contact-field name should receive it?"),
        ),
        exit=ExitDefinition(kind="linear"), engine1_type="persist_contact_field",
        engine2_type="record_request", engine3_primitive="update_contact_field",
    ),
    CapabilityDefinition(
        id="end", node_type="end",
        aliases=("end", "finish", "stop", "terminate", "complete", "close"),
        fields=(_field("reason", AcquisitionPolicy.USER_REQUIRED, question="What terminal reason should be recorded?"),),
        exit=ExitDefinition(kind="terminal"), engine1_type="end",
        engine2_type="end", engine3_primitive="end_terminal",
    ),
)

REGISTRY = MappingProxyType({item.id: item for item in CAPABILITIES})


class UnsupportedCapabilityError(ValueError):
    code = "P4_UNSUPPORTED_CAPABILITY"

    def __init__(self, capability: str):
        self.capability = capability
        super().__init__(f"{self.code}: {capability!r} is not registered")


def require_capability(capability: str) -> CapabilityDefinition:
    try:
        return REGISTRY[capability]
    except KeyError as exc:
        raise UnsupportedCapabilityError(capability) from exc


def registry_payload() -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "capabilities": [item.model_dump(mode="json") for item in CAPABILITIES],
    }


def registry_hash() -> str:
    raw = json.dumps(registry_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def classify_tokens(statement: str) -> list[str]:
    tokens = {token.strip(".,:;!?()[]{}\"'").casefold() for token in statement.split()}
    normalized = statement.casefold()
    scored: list[tuple[int, str]] = []
    for definition in CAPABILITIES:
        score = sum(
            1
            for alias in definition.aliases
            if (
                alias in tokens
                if " " not in alias
                else re.search(rf"\b{re.escape(alias)}\b", normalized) is not None
            )
        )
        if score:
            scored.append((score, definition.id))
    best = max((score for score, _ in scored), default=0)
    return sorted(capability for score, capability in scored if score == best)
