from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

REGISTRY_VERSION = "capability-registry.v3-semantic-fact-bindings"
WORKBENCH_ACQUISITION_POLICY_VERSION = "product4-workbench-acquisition-policy-1.0"
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


class SemanticFactKind(str, Enum):
    """Capability-neutral facts that may be grounded in user prose."""

    USER_FACING_TEXT = "user_facing_text"
    VISIBLE_CHOICE = "visible_choice"
    SELECTION_CARDINALITY = "selection_cardinality"
    RESPONSE_FORMAT = "response_format"
    PROVIDED_VARIABLE_NAME = "provided_variable_name"
    VALIDATION_RULE = "validation_rule"
    REQUIREDNESS = "requiredness"
    TERMINAL_OUTCOME = "terminal_outcome"
    TRIGGER_VALUE = "trigger_value"
    COMMUNICATION_CHANNEL = "communication_channel"
    PERSISTENCE_DESTINATION = "persistence_destination"
    DURATION_OR_SCHEDULE = "duration_or_schedule"
    EXTERNAL_ENDPOINT = "external_endpoint"
    PROVIDED_IDENTIFIER = "provided_identifier"


class FactNormalization(str, Enum):
    """Deterministic conversion from a semantic fact to a registry value."""

    SINGLE_VALUE = "single_value"
    CHOICE_OPTIONS = "choice_options"
    VALIDATION_RULE = "validation_rule"


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
    accepted_fact_kinds: tuple[SemanticFactKind, ...] = ()
    fact_normalization: FactNormalization = FactNormalization.SINGLE_VALUE

    @model_validator(mode="after")
    def question_matches_policy(self) -> "CapabilityField":
        if self.policy is AcquisitionPolicy.USER_REQUIRED and not self.question:
            raise ValueError("user-required fields need a question")
        if self.policy not in {AcquisitionPolicy.USER_REQUIRED, AcquisitionPolicy.DERIVED} and self.question:
            raise ValueError("only user-required or derived-fallback fields may define questions")
        if self.policy in {AcquisitionPolicy.SUPPLIED, AcquisitionPolicy.USER_REQUIRED} and not self.accepted_fact_kinds:
            raise ValueError("source-supplied fields need at least one accepted semantic fact kind")
        if len(self.accepted_fact_kinds) != len(set(self.accepted_fact_kinds)):
            raise ValueError("accepted semantic fact kinds must be unique")
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

    @model_validator(mode="after")
    def semantic_fact_routes_are_unambiguous(self) -> "CapabilityDefinition":
        routes: dict[SemanticFactKind, str] = {}
        for field in self.fields:
            for fact_kind in field.accepted_fact_kinds:
                previous = routes.setdefault(fact_kind, field.path)
                if previous != field.path:
                    raise ValueError(
                        f"semantic fact {fact_kind.value!r} maps to both {previous!r} and {field.path!r}"
                    )
        return self


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
            _field(
                "copy", AcquisitionPolicy.USER_REQUIRED,
                question="What exact message should be sent?",
                accepted_fact_kinds=(SemanticFactKind.USER_FACING_TEXT,),
            ),
            _field("locale", AcquisitionPolicy.DEFAULTED, default="en"),
        ),
        exit=ExitDefinition(kind="linear"), engine1_type="send_message",
        engine2_type="send_message", engine3_primitive="send_text_message",
    ),
    CapabilityDefinition(
        id="capture_user_input", node_type="capture_input",
        aliases=("capture", "collect", "ask", "input", "reply", "response", "obtain"),
        fields=(
            _field(
                "prompt", AcquisitionPolicy.USER_REQUIRED,
                question="What exact prompt should ask for the input?",
                accepted_fact_kinds=(SemanticFactKind.USER_FACING_TEXT,),
            ),
            _field(
                "input_type", AcquisitionPolicy.USER_REQUIRED,
                question="Which input type should be used: text, number, email or phone?",
                answer_type="options", options=INPUT_TYPES,
                value_kind=FieldValueKind.INPUT_TYPE,
                accepted_fact_kinds=(SemanticFactKind.RESPONSE_FORMAT,),
            ),
            _field(
                "save_as", AcquisitionPolicy.USER_REQUIRED,
                question="What flow-variable name should store the answer?",
                accepted_fact_kinds=(SemanticFactKind.PROVIDED_VARIABLE_NAME,),
            ),
            _field(
                "required", AcquisitionPolicy.DEFAULTED, default=True,
                value_kind=FieldValueKind.BOOLEAN,
                accepted_fact_kinds=(SemanticFactKind.REQUIREDNESS,),
            ),
            _field(
                "validation", AcquisitionPolicy.USER_REQUIRED,
                question="What validation constraints should apply? Use an empty object for none.",
                answer_type="json", value_kind=FieldValueKind.VALIDATION,
                accepted_fact_kinds=(SemanticFactKind.VALIDATION_RULE,),
                fact_normalization=FactNormalization.VALIDATION_RULE,
            ),
        ),
        exit=ExitDefinition(kind="linear"), engine1_type="capture_input",
        engine2_type="ask_input", engine3_primitive="wait_for_text_response",
    ),
    CapabilityDefinition(
        id="fixed_choice", node_type="fixed_choice",
        aliases=("choice", "choose", "select", "options", "menu", "rate", "whether"),
        fields=(
            _field(
                "title", AcquisitionPolicy.USER_REQUIRED,
                question="What exact choice prompt should be shown?",
                accepted_fact_kinds=(SemanticFactKind.USER_FACING_TEXT,),
            ),
            _field(
                "options", AcquisitionPolicy.USER_REQUIRED,
                question="Which exact choice labels should people see? Provide labels only, separated by commas.",
                value_kind=FieldValueKind.CHOICE_OPTIONS,
                accepted_fact_kinds=(SemanticFactKind.VISIBLE_CHOICE,),
                fact_normalization=FactNormalization.CHOICE_OPTIONS,
            ),
        ),
        exit=ExitDefinition(kind="dynamic", source_field="options"), engine1_type="fixed_choice",
        engine2_type="ask_choice", engine3_primitive="exact_categorical_branch",
    ),
    CapabilityDefinition(
        id="persist_contact_field", node_type="persist_contact_field",
        aliases=("persist", "save", "store", "contact field", "record"),
        fields=(
            _field(
                "source_variable", AcquisitionPolicy.DERIVED,
                question="Which flow variable should be saved?",
                accepted_fact_kinds=(SemanticFactKind.PROVIDED_VARIABLE_NAME,),
            ),
            _field(
                "field_name", AcquisitionPolicy.USER_REQUIRED,
                question="Which contact-field name should receive it?",
                accepted_fact_kinds=(SemanticFactKind.PERSISTENCE_DESTINATION,),
            ),
        ),
        exit=ExitDefinition(kind="linear"), engine1_type="persist_contact_field",
        engine2_type="record_request", engine3_primitive="update_contact_field",
    ),
    CapabilityDefinition(
        id="end", node_type="end",
        aliases=("end", "finish", "stop", "terminate", "complete", "close"),
        fields=(
            _field(
                "reason", AcquisitionPolicy.USER_REQUIRED,
                question="What terminal reason should be recorded?",
                accepted_fact_kinds=(SemanticFactKind.TERMINAL_OUTCOME,),
            ),
        ),
        exit=ExitDefinition(kind="terminal"), engine1_type="end",
        engine2_type="end", engine3_primitive="end_terminal",
    ),
)

REGISTRY = MappingProxyType({item.id: item for item in CAPABILITIES})


# The authored registry remains intentionally conservative: its field policies
# describe the public package contract.  Meaning-stage acquisition is a
# separate, versioned workbench policy so deterministic defaults never leak
# into package contracts or scattered special cases.
WORKBENCH_ACQUISITION_POLICY: MappingProxyType[str, MappingProxyType[str, AcquisitionPolicy]] = MappingProxyType({
    "capture_user_input": MappingProxyType({
        "validation": AcquisitionPolicy.DEFAULTED,
        "save_as": AcquisitionPolicy.GENERATED,
    }),
    "persist_contact_field": MappingProxyType({
        "source_variable": AcquisitionPolicy.DERIVED,
    }),
})


def workbench_field_policy(capability: str, field_path: str) -> AcquisitionPolicy | None:
    """Return the explicit meaning-workbench acquisition override, if any."""

    return WORKBENCH_ACQUISITION_POLICY.get(capability, {}).get(field_path)


def workbench_policy_payload() -> dict[str, Any]:
    return {
        "version": WORKBENCH_ACQUISITION_POLICY_VERSION,
        "overrides": {
            capability: {field: policy.value for field, policy in fields.items()}
            for capability, fields in WORKBENCH_ACQUISITION_POLICY.items()
        },
    }


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


def semantic_fact_field_map(capability: str) -> dict[str, CapabilityField]:
    """Return the registry-owned fact-to-field routes for one capability."""

    result: dict[str, CapabilityField] = {}
    for field in require_capability(capability).fields:
        for fact_kind in field.accepted_fact_kinds:
            result[fact_kind.value] = field
    return result


def _single_fact_value(raw: Any) -> Any:
    if isinstance(raw, list):
        if len(raw) != 1:
            raise ValueError("P4_SEMANTIC_FACT_SINGLE_VALUE_REQUIRED")
        return raw[0]
    return raw


def _stable_choice_options(raw: Any) -> list[dict[str, str]]:
    labels = raw if isinstance(raw, list) else [raw]
    if not labels or any(not isinstance(label, str) or not label.strip() for label in labels):
        raise ValueError("P4_SEMANTIC_FACT_CHOICE_LABELS_INVALID")
    result: list[dict[str, str]] = []
    used: set[str] = set()
    for raw_label in labels:
        label = raw_label.strip()
        base = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_") or "option"
        value = base
        suffix = 2
        while value in used:
            value = f"{base}_{suffix}"
            suffix += 1
        used.add(value)
        result.append({"label": label, "value": value})
    return result


def normalize_semantic_fact_value(field: CapabilityField, raw: Any) -> Any:
    """Convert a grounded fact value using only registry-declared policy."""

    if field.fact_normalization is FactNormalization.CHOICE_OPTIONS:
        candidate = _stable_choice_options(raw)
    elif field.fact_normalization is FactNormalization.VALIDATION_RULE:
        candidate = _single_fact_value(raw)
        if candidate is None:
            candidate = {}
    else:
        candidate = _single_fact_value(raw)
    return validate_registry_field_value(field, candidate)


def bind_semantic_facts(capability: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically bind capability-neutral facts to registry fields.

    Facts that describe topology or unsupported future capabilities are left
    alone.  Two facts competing for one configuration field fail closed.
    """

    routes = semantic_fact_field_map(capability)
    grouped: dict[str, tuple[CapabilityField, list[Any]]] = {}
    for fact in facts:
        kind = str(fact.get("kind") or "")
        field = routes.get(kind)
        if field is None:
            continue
        grouped.setdefault(field.path, (field, []))[1].append(fact.get("values"))

    result: dict[str, Any] = {}
    for field_path, (field, raw_values) in grouped.items():
        if field.fact_normalization is FactNormalization.CHOICE_OPTIONS:
            combined: list[Any] = []
            for raw in raw_values:
                combined.extend(raw if isinstance(raw, list) else [raw])
            result[field_path] = normalize_semantic_fact_value(field, combined)
            continue
        normalized = [normalize_semantic_fact_value(field, raw) for raw in raw_values]
        if any(value != normalized[0] for value in normalized[1:]):
            raise ValueError(f"P4_SEMANTIC_FACT_FIELD_AMBIGUOUS: {capability}.{field.path}")
        result[field_path] = normalized[0]
    return result


def required_semantic_fact_gaps(capability: str, bound_values: dict[str, Any]) -> list[str]:
    """Report only genuinely absent user-required fields after binding."""

    missing: list[str] = []
    for field in require_capability(capability).fields:
        policy = workbench_field_policy(capability, field.path) or field.policy
        if policy is AcquisitionPolicy.USER_REQUIRED and field.path not in bound_values:
            missing.append(field.path)
    return missing


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
