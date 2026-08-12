"""Closed typed contracts for source-graph edges.

T08 owns concrete source-edge data only.  Source nodes and the package envelope
 are intentionally not imported here: T09 and T10 own those interfaces.  An
 edge therefore validates its own identifiers and provenance, while a later
 package validator resolves the identifiers against the node set.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from .ledger_contracts import Provenance

SOURCE_EDGE_CONTRACT_VERSION = "source-edge-contracts-1.0"
SOURCE_EDGE_SCHEMA_VERSION = SOURCE_EDGE_CONTRACT_VERSION
EDGE_CONTRACT_VERSION = SOURCE_EDGE_CONTRACT_VERSION

_IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,127}$"
_NON_EMPTY_TEXT_PATTERN = r".*\S.*"


def _validate_concrete_id(value: str) -> str:
    """Keep concrete source IDs separate from ledger/lifecycle IDs."""

    if value.upper().startswith(("REQ-", "DEC-", "LEDGER-", "CONF-")):
        raise ValueError("source edge IDs must not use ledger or lifecycle IDs")
    return value


SourceNodeId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
    ),
    AfterValidator(_validate_concrete_id),
]
NodeId: TypeAlias = SourceNodeId
SourceEdgeId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
    ),
    AfterValidator(_validate_concrete_id),
]
EdgeId: TypeAlias = SourceEdgeId
NonEmptyText: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=10_000, pattern=_NON_EMPTY_TEXT_PATTERN),
]
StableChoiceValue: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=300),
]
StableValue: TypeAlias = StableChoiceValue


class SourceEdgeContractError(ValueError):
    """Raised by explicit T08 validation helpers."""


class _StrictModel(BaseModel):
    """Strict base model used by every T08 object contract."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class EdgeRole(str, Enum):
    """The complete set of source-edge routing roles."""

    NEXT = "next"
    OUTCOME = "outcome"
    DEFAULT = "default"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    SUCCESS = "success"
    FAILURE = "failure"
    RETRY = "retry"
    EXHAUSTED = "exhausted"


SourceEdgeRole = EdgeRole
EdgeRoleType = EdgeRole


class ConditionOperator(str, Enum):
    """Closed operators available to deterministic source conditions."""

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    MATCHES = "matches"
    IS_PRESENT = "is_present"
    IS_NOT_PRESENT = "is_not_present"


ConditionOperatorType = ConditionOperator
EdgeConditionOperator = ConditionOperator


class ChoiceCondition(_StrictModel):
    """A visible choice title paired with its stable submitted value."""

    type: Literal["choice"]
    title: NonEmptyText = Field(validation_alias=AliasChoices("title", "label"))
    stable_value: StableChoiceValue = Field(
        validation_alias=AliasChoices("stable_value", "value")
    )

    @property
    def label(self) -> str:
        """Compatibility spelling for callers using the ledger vocabulary."""

        return self.title

    @property
    def value(self) -> str:
        """Compatibility spelling for callers using the ledger vocabulary."""

        return self.stable_value


class ComparisonCondition(_StrictModel):
    """A typed comparison against a source variable or expression."""

    type: Literal["comparison"]
    subject: NonEmptyText = Field(
        validation_alias=AliasChoices("subject", "field", "variable", "left")
    )
    operator: ConditionOperator
    expected: Any = Field(
        default=None,
        validation_alias=AliasChoices("expected", "value", "right"),
    )

    @property
    def field(self) -> str:
        """Compatibility spelling for source-variable conditions."""

        return self.subject

    @property
    def value(self) -> Any:
        """Compatibility spelling for callers using value/expected wording."""

        return self.expected


class ExpressionCondition(_StrictModel):
    """A confirmed expression with an optional closed operator annotation."""

    type: Literal["expression"]
    expression: NonEmptyText
    operator: ConditionOperator | None = None
    expected: Any = Field(
        default=None,
        validation_alias=AliasChoices("expected", "value"),
    )


class TimeoutCondition(_StrictModel):
    """A typed no-response condition with an explicit positive duration."""

    type: Literal["timeout"]
    seconds: int = Field(
        gt=0,
        validation_alias=AliasChoices("seconds", "timeout_seconds"),
    )

    @property
    def timeout_seconds(self) -> int:
        """Compatibility spelling used by the ledger timeout template."""

        return self.seconds


class RouteMarkerCondition(_StrictModel):
    """Optional typed marker for a role whose role is already explicit."""

    type: Literal[
        "next",
        "default",
        "invalid",
        "success",
        "failure",
        "retry",
        "exhausted",
    ]


TypedCondition: TypeAlias = Annotated[
    ChoiceCondition
    | ComparisonCondition
    | ExpressionCondition
    | TimeoutCondition
    | RouteMarkerCondition,
    Field(discriminator="type"),
]
EdgeCondition = TypedCondition
ConditionContract = TypedCondition
CONDITION_ADAPTER = TypeAdapter(TypedCondition)


class Condition(RootModel[TypedCondition]):
    """Root adapter for validating one closed typed condition."""

    @property
    def type(self) -> str:
        return self.root.type

    @property
    def condition(self) -> TypedCondition:
        return self.root


class _EdgeBase(_StrictModel):
    """Fields shared by every concrete edge-role contract."""

    id: SourceEdgeId = Field(validation_alias=AliasChoices("id", "edge_id"))
    source_id: SourceNodeId = Field(
        validation_alias=AliasChoices("source_id", "source_node_id", "from")
    )
    target_id: SourceNodeId = Field(
        validation_alias=AliasChoices("target_id", "target_node_id", "to")
    )
    provenance: list[Provenance] = Field(min_length=1)

    @property
    def edge_id(self) -> str:
        return self.id

    @property
    def source_node_id(self) -> str:
        return self.source_id

    @property
    def target_node_id(self) -> str:
        return self.target_id

    @model_validator(mode="before")
    @classmethod
    def accept_one_provenance_object(cls, value: Any) -> Any:
        """Normalize a single provenance object without weakening its schema."""

        if not isinstance(value, Mapping):
            return value
        candidate = dict(value)
        if isinstance(candidate.get("provenance"), Mapping):
            candidate["provenance"] = [candidate["provenance"]]
        return candidate


class NextEdge(_EdgeBase):
    role: Literal["next"]
    condition: RouteMarkerCondition | None = None

    @model_validator(mode="after")
    def marker_matches_role(self) -> NextEdge:
        _validate_marker(self.condition, self.role)
        return self


class OutcomeEdge(_EdgeBase):
    role: Literal["outcome"]
    condition: ChoiceCondition


class DefaultEdge(_EdgeBase):
    role: Literal["default"]
    condition: RouteMarkerCondition | None = None

    @model_validator(mode="after")
    def marker_matches_role(self) -> DefaultEdge:
        _validate_marker(self.condition, self.role)
        return self


class InvalidEdge(_EdgeBase):
    role: Literal["invalid"]
    condition: RouteMarkerCondition | None = None

    @model_validator(mode="after")
    def marker_matches_role(self) -> InvalidEdge:
        _validate_marker(self.condition, self.role)
        return self


class TimeoutEdge(_EdgeBase):
    role: Literal["timeout"]
    condition: TimeoutCondition


class SuccessEdge(_EdgeBase):
    role: Literal["success"]
    condition: ChoiceCondition | RouteMarkerCondition | None = None

    @model_validator(mode="after")
    def marker_matches_role(self) -> SuccessEdge:
        if isinstance(self.condition, RouteMarkerCondition):
            _validate_marker(self.condition, self.role)
        elif self.condition is not None and not isinstance(self.condition, ChoiceCondition):
            raise ValueError("success edge condition must be a choice or success marker")
        return self


class FailureEdge(_EdgeBase):
    role: Literal["failure"]
    condition: RouteMarkerCondition | None = None

    @model_validator(mode="after")
    def marker_matches_role(self) -> FailureEdge:
        _validate_marker(self.condition, self.role)
        return self


class RetryEdge(_EdgeBase):
    role: Literal["retry"]
    condition: RouteMarkerCondition | None = None

    @model_validator(mode="after")
    def marker_matches_role(self) -> RetryEdge:
        _validate_marker(self.condition, self.role)
        return self


class ExhaustedEdge(_EdgeBase):
    role: Literal["exhausted"]
    condition: RouteMarkerCondition | None = None

    @model_validator(mode="after")
    def marker_matches_role(self) -> ExhaustedEdge:
        _validate_marker(self.condition, self.role)
        return self


def _validate_marker(condition: RouteMarkerCondition | None, role: str) -> None:
    if condition is not None and condition.type != role:
        raise ValueError(f"{role} edge marker condition must have type {role!r}")


SourceEdgeUnion: TypeAlias = Annotated[
    NextEdge
    | OutcomeEdge
    | DefaultEdge
    | InvalidEdge
    | TimeoutEdge
    | SuccessEdge
    | FailureEdge
    | RetryEdge
    | ExhaustedEdge,
    Field(discriminator="role"),
]
TypedSourceEdge: TypeAlias = SourceEdgeUnion
SourceEdgeContractUnion: TypeAlias = SourceEdgeUnion
SOURCE_EDGE_ADAPTER = TypeAdapter(SourceEdgeUnion)
SOURCE_EDGE_MODELS: tuple[type[_EdgeBase], ...] = (
    NextEdge,
    OutcomeEdge,
    DefaultEdge,
    InvalidEdge,
    TimeoutEdge,
    SuccessEdge,
    FailureEdge,
    RetryEdge,
    ExhaustedEdge,
)
SOURCE_EDGE_TEMPLATE_BY_ROLE = {
    "next": NextEdge,
    "outcome": OutcomeEdge,
    "default": DefaultEdge,
    "invalid": InvalidEdge,
    "timeout": TimeoutEdge,
    "success": SuccessEdge,
    "failure": FailureEdge,
    "retry": RetryEdge,
    "exhausted": ExhaustedEdge,
}


class SourceEdge(_EdgeBase):
    """Direct single-edge contract with closed role/condition validation.

    ``SourceEdgeUnion`` is the schema-facing discriminated union.  This common
    model is retained as a convenient public validator for callers that only
    need one edge and do not need the concrete subclass type.
    """

    role: EdgeRole
    condition: TypedCondition | None = None

    @property
    def edge_role(self) -> EdgeRole:
        return self.role

    @model_validator(mode="after")
    def role_condition_is_typed(self) -> SourceEdge:
        if self.role is EdgeRole.OUTCOME:
            if not isinstance(self.condition, ChoiceCondition):
                raise ValueError("outcome edge requires a choice condition")
        elif self.role is EdgeRole.TIMEOUT:
            if not isinstance(self.condition, TimeoutCondition):
                raise ValueError("timeout edge requires a timeout condition")
        elif self.condition is not None:
            if isinstance(self.condition, RouteMarkerCondition):
                _validate_marker(self.condition, self.role.value)
            elif self.role is EdgeRole.SUCCESS and isinstance(self.condition, ChoiceCondition):
                pass
            else:
                raise ValueError(
                    f"{self.role.value} edge cannot use {self.condition.type!r} condition"
                )
        return self


SourceEdgeContract = SourceEdge
EdgeContract = SourceEdge


class SourceEdgeDocument(_StrictModel):
    """A deterministic edge collection with duplicate and choice checks."""

    schema_version: Literal[SOURCE_EDGE_CONTRACT_VERSION]
    edges: list[SourceEdgeUnion] = Field(
        min_length=1,
        validation_alias=AliasChoices("edges", "source_edges"),
    )

    @model_validator(mode="after")
    def identifiers_and_choices_are_unique(self) -> SourceEdgeDocument:
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate edge IDs are not allowed")

        outcome_edges_by_source: dict[str, list[OutcomeEdge]] = {}
        for edge in self.edges:
            if isinstance(edge, OutcomeEdge):
                outcome_edges_by_source.setdefault(edge.source_id, []).append(edge)
        for source_id, outcome_edges in outcome_edges_by_source.items():
            titles = [edge.condition.title for edge in outcome_edges]
            stable_values = [edge.condition.stable_value for edge in outcome_edges]
            if len(titles) != len(set(titles)):
                raise ValueError(f"duplicate visible choice titles for source {source_id}")
            if len(stable_values) != len(set(stable_values)):
                raise ValueError(f"duplicate stable choice values for source {source_id}")
        return self


SourceEdges = SourceEdgeDocument
EdgeDocument = SourceEdgeDocument
EdgeSet = SourceEdgeDocument


class SourceEdgeSet(RootModel[list[SourceEdgeUnion]]):
    """Root adapter for callers whose input is a bare edge list."""

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> SourceEdgeSet:
        edge_ids = [edge.id for edge in self.root]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate edge IDs are not allowed")
        return self


def parse_condition(
    value: TypedCondition | Mapping[str, Any] | str | bytes | bytearray,
) -> TypedCondition:
    """Parse one condition through the closed ``type`` discriminator."""

    if isinstance(value, (str, bytes, bytearray)):
        return CONDITION_ADAPTER.validate_json(value)
    return CONDITION_ADAPTER.validate_python(value)


def serialize_condition(value: TypedCondition | Mapping[str, Any]) -> str:
    """Serialize one typed condition with stable JSON key ordering."""

    condition = parse_condition(value)
    return json.dumps(
        condition.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_source_edge(
    value: SourceEdgeUnion | Mapping[str, Any] | str | bytes | bytearray,
) -> SourceEdgeUnion:
    """Parse one concrete edge through the closed ``role`` discriminator."""

    if isinstance(value, (str, bytes, bytearray)):
        return SOURCE_EDGE_ADAPTER.validate_json(value)
    return SOURCE_EDGE_ADAPTER.validate_python(value)


def serialize_source_edge(value: SourceEdgeUnion | Mapping[str, Any]) -> str:
    """Serialize one typed source edge deterministically."""

    edge = parse_source_edge(value)
    return json.dumps(
        edge.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_source_edge_document_json(
    value: str | bytes | bytearray,
) -> SourceEdgeDocument:
    """Parse a versioned edge document and enforce collection invariants."""

    return SourceEdgeDocument.model_validate_json(value)


def serialize_source_edge_document_json(
    value: SourceEdgeDocument | Mapping[str, Any],
) -> str:
    """Serialize a versioned edge document deterministically."""

    document = (
        value
        if isinstance(value, SourceEdgeDocument)
        else SourceEdgeDocument.model_validate(value)
    )
    return json.dumps(
        document.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_source_edges_json(
    value: str | bytes | bytearray,
) -> SourceEdgeSet:
    """Parse a bare JSON edge list through the same closed edge union."""

    return SourceEdgeSet.model_validate_json(value)


def canonical_source_edge_schema_json() -> str:
    """Return the deterministic JSON Schema for the typed edge union."""

    return json.dumps(
        SOURCE_EDGE_ADAPTER.json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_source_edge_schema_hash() -> str:
    """Return a stable hash for the typed edge-union schema."""

    return hashlib.sha256(canonical_source_edge_schema_json().encode("utf-8")).hexdigest()


def canonical_edge_schema_json() -> str:
    """Compatibility alias for the T08 edge schema snapshot."""

    return canonical_source_edge_schema_json()


def canonical_edge_schema_hash() -> str:
    """Compatibility alias for the T08 edge schema hash."""

    return canonical_source_edge_schema_hash()


def canonical_source_edges_json(
    value: SourceEdgeDocument | SourceEdgeSet | Sequence[SourceEdgeUnion] | Mapping[str, Any],
) -> str:
    """Return a stable canonical representation of an edge collection."""

    if isinstance(value, SourceEdgeDocument):
        payload = value.model_dump(mode="json")
    elif isinstance(value, SourceEdgeSet):
        payload = {"edges": [edge.model_dump(mode="json") for edge in value.root]}
    elif isinstance(value, Mapping):
        payload = SourceEdgeDocument.model_validate(value).model_dump(mode="json")
    else:
        document = SourceEdgeSet.model_validate(list(value))
        payload = {"edges": [edge.model_dump(mode="json") for edge in document.root]}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_source_edges_hash(
    value: SourceEdgeDocument | SourceEdgeSet | Sequence[SourceEdgeUnion] | Mapping[str, Any],
) -> str:
    """Return a stable hash for a validated edge collection."""

    return hashlib.sha256(canonical_source_edges_json(value).encode("utf-8")).hexdigest()


# Short compatibility names follow the naming used by the earlier ledger
# contract modules while keeping one implementation for every operation.
parse_edge = parse_source_edge
serialize_edge = serialize_source_edge
parse_edge_document_json = parse_source_edge_document_json
serialize_edge_document_json = serialize_source_edge_document_json
canonical_schema_json = canonical_source_edge_schema_json
canonical_schema_hash = canonical_source_edge_schema_hash
source_edge_schema_json = canonical_source_edge_schema_json
source_edge_schema_hash = canonical_source_edge_schema_hash
parse_source_edge_json = parse_source_edge
serialize_source_edge_json = serialize_source_edge

NextEdgeContract = NextEdge
OutcomeEdgeContract = OutcomeEdge
DefaultEdgeContract = DefaultEdge
InvalidEdgeContract = InvalidEdge
TimeoutEdgeContract = TimeoutEdge
SuccessEdgeContract = SuccessEdge
FailureEdgeContract = FailureEdge
RetryEdgeContract = RetryEdge
ExhaustedEdgeContract = ExhaustedEdge
ChoiceEdgeCondition = ChoiceCondition
TimeoutEdgeCondition = TimeoutCondition


__all__ = [
    "CONDITION_ADAPTER",
    "EDGE_CONTRACT_VERSION",
    "SOURCE_EDGE_ADAPTER",
    "SOURCE_EDGE_CONTRACT_VERSION",
    "SOURCE_EDGE_MODELS",
    "SOURCE_EDGE_SCHEMA_VERSION",
    "SOURCE_EDGE_TEMPLATE_BY_ROLE",
    "ChoiceEdgeCondition",
    "ComparisonCondition",
    "Condition",
    "ConditionContract",
    "ConditionOperator",
    "ConditionOperatorType",
    "DefaultEdge",
    "DefaultEdgeContract",
    "EdgeCondition",
    "EdgeConditionOperator",
    "EdgeContract",
    "EdgeDocument",
    "EdgeId",
    "EdgeRole",
    "EdgeRoleType",
    "EdgeSet",
    "ExhaustedEdge",
    "ExhaustedEdgeContract",
    "ExpressionCondition",
    "FailureEdge",
    "FailureEdgeContract",
    "InvalidEdge",
    "InvalidEdgeContract",
    "NextEdge",
    "NextEdgeContract",
    "NodeId",
    "OutcomeEdge",
    "OutcomeEdgeContract",
    "RetryEdge",
    "RetryEdgeContract",
    "RouteMarkerCondition",
    "SourceEdge",
    "SourceEdgeContract",
    "SourceEdgeContractError",
    "SourceEdgeContractUnion",
    "SourceEdgeDocument",
    "SourceEdgeId",
    "SourceEdgeRole",
    "SourceEdgeSet",
    "SourceEdgeUnion",
    "SourceEdges",
    "SourceNodeId",
    "StableChoiceValue",
    "StableValue",
    "SuccessEdge",
    "SuccessEdgeContract",
    "TimeoutCondition",
    "TimeoutEdge",
    "TimeoutEdgeCondition",
    "TimeoutEdgeContract",
    "TypedCondition",
    "TypedSourceEdge",
    "canonical_edge_schema_hash",
    "canonical_edge_schema_json",
    "canonical_schema_hash",
    "canonical_schema_json",
    "canonical_source_edge_schema_hash",
    "canonical_source_edge_schema_json",
    "canonical_source_edges_hash",
    "canonical_source_edges_json",
    "parse_condition",
    "parse_edge",
    "parse_edge_document_json",
    "parse_source_edge",
    "parse_source_edge_document_json",
    "parse_source_edge_json",
    "parse_source_edges_json",
    "serialize_condition",
    "serialize_edge",
    "serialize_edge_document_json",
    "serialize_source_edge",
    "serialize_source_edge_document_json",
    "serialize_source_edge_json",
    "source_edge_schema_hash",
    "source_edge_schema_json",
]
