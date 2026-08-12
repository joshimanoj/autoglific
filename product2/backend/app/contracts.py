from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

JsonValue = Any


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SourceRef(ContractModel):
    source_unit_id: str = Field(pattern=r"^S[0-9]{3,}$")
    source_quote: str = Field(min_length=1)


class SourceUnit(ContractModel):
    id: str = Field(pattern=r"^S[0-9]{3,}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def offsets_ordered(self) -> SourceUnit:
        if self.end_offset < self.start_offset:
            raise ValueError("end_offset must be >= start_offset")
        return self


class SemanticNode(ContractModel):
    id: str = Field(pattern=r"^N[0-9]{3,}$")
    type: Literal[
        "start",
        "message",
        "media",
        "input",
        "decision",
        "action",
        "delay",
        "handoff",
        "subflow",
        "join",
        "end",
    ]
    label: str = Field(min_length=1, max_length=300)
    details: str | None = Field(default=None, max_length=2_000)
    expected_branch_count: int | None = Field(default=None, ge=2)
    source_refs: list[SourceRef] = Field(min_length=1)


class SemanticEdge(ContractModel):
    id: str = Field(pattern=r"^E[0-9]{3,}$")
    from_: str = Field(alias="from", pattern=r"^N[0-9]{3,}$")
    to: str = Field(pattern=r"^N[0-9]{3,}$")
    label: str | None = None
    condition_source_text: str | None = None
    source_refs: list[SourceRef] = Field(min_length=1)


NORMALIZED_EXECUTION_ROLES = (
    "pass_through",
    "communication",
    "input_collection",
    "decision",
    "external_action",
    "contact_update",
    "collection_update",
    "ticket_or_handoff",
    "staff_notification",
    "subflow",
    "delay_or_schedule",
    "terminal",
    "compound",
    "unknown",
)

NORMALIZED_OPERATION_KINDS = (
    "emit_message",
    "emit_media",
    "collect_input",
    "evaluate_decision",
    "call_external",
    "update_contact",
    "add_to_collection",
    "remove_from_collection",
    "open_ticket",
    "notify_staff",
    "enter_subflow",
    "wait_for_time",
    "wait_for_result",
    "terminate",
    "pass_through",
    "unsupported",
)

NORMALIZED_EDGE_ROLES = (
    "sequential_continuation",
    "branch_outcome",
    "interaction_response",
    "error_or_fallback",
    "pass_through",
    "redundant_terminal_continuation",
    "contradictory_terminal_continuation",
    "unknown",
)


class NormalizedSemanticOperation(ContractModel):
    id: str = Field(pattern=r"^NOP_N[0-9]{3,}_[0-9]{2,}$")
    kind: Literal[*NORMALIZED_OPERATION_KINDS]  # type: ignore[misc]
    sequence: int = Field(ge=1)
    intent: str = Field(min_length=1, max_length=2_000)
    terminal: bool = False
    resource_kind: str | None = Field(default=None, max_length=100)
    interaction_family: str | None = Field(default=None, max_length=100)
    source_refs: list[SourceRef] = Field(default_factory=list)


class NormalizedControl(ContractModel):
    entry: Literal["allowed", "blocked"] = "allowed"
    continuation: Literal["continues", "terminates", "conditional", "unknown"]
    branching: Literal["none", "conditional", "multiple", "unknown"]


class NormalizedClassification(ContractModel):
    resolver: Literal["product2", "user", "resource_binding"] = "product2"
    confidence: Literal["high", "medium", "low"]
    basis: Literal["source_text_and_graph", "source_type", "user_answer", "verified_capability"]
    requires_user_confirmation: bool = False


class NormalizedSemanticContext(ContractModel):
    incoming_edge_ids: list[str] = Field(default_factory=list)
    outgoing_edge_ids: list[str] = Field(default_factory=list)


class NormalizedSemanticNode(ContractModel):
    semantic_node_id: str = Field(pattern=r"^N[0-9]{3,}$")
    source_type: Literal[
        "start",
        "message",
        "media",
        "input",
        "decision",
        "action",
        "delay",
        "handoff",
        "subflow",
        "join",
        "end",
    ]
    execution_role: Literal[*NORMALIZED_EXECUTION_ROLES]  # type: ignore[misc]
    operations: list[NormalizedSemanticOperation] = Field(min_length=1)
    control: NormalizedControl
    classification: NormalizedClassification
    source_refs: list[SourceRef] = Field(min_length=1)
    semantic_context: NormalizedSemanticContext


class NormalizedSemanticEdge(ContractModel):
    semantic_edge_id: str = Field(pattern=r"^E[0-9]{3,}$")
    from_semantic_node_id: str = Field(pattern=r"^N[0-9]{3,}$")
    to_semantic_node_id: str = Field(pattern=r"^N[0-9]{3,}$")
    role: Literal[*NORMALIZED_EDGE_ROLES]  # type: ignore[misc]
    reason: str = Field(min_length=1, max_length=1_000)
    requires_interaction_binding: bool = False
    safe_to_consume: bool = False
    source_refs: list[SourceRef] = Field(default_factory=list)


class NormalizedSemanticCoverage(ContractModel):
    semantic_node_ids: list[str]
    semantic_edge_ids: list[str]


class NormalizerMetadata(ContractModel):
    version: Literal["product2-semantic-normalizer-0.1"]
    attempt: int = Field(ge=1, le=3)
    model: str | None = None


class NormalizedSemanticPlan(ContractModel):
    schema_version: Literal["normalized-semantic-plan-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    nodes: list[NormalizedSemanticNode] = Field(min_length=1)
    edges: list[NormalizedSemanticEdge]
    coverage: NormalizedSemanticCoverage
    normalizer: NormalizerMetadata


class NormalizationIssue(ContractModel):
    code: str = Field(min_length=1, max_length=100)
    classification: Literal[
        "repairable_normalization",
        "material_clarification",
        "unsupported_capability",
        "environment_failure",
    ]
    message: str = Field(min_length=1)
    semantic_node_ids: list[str] = Field(default_factory=list)
    semantic_edge_ids: list[str] = Field(default_factory=list)
    source_quote: str | None = None
    graph_context: str | None = None
    repair_instruction: str = Field(min_length=1)


class NormalizedSemanticPlanValidationReport(ContractModel):
    schema_version: Literal["normalized-semantic-plan-validation-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt: int = Field(ge=1, le=3)
    passed: bool
    phase: Literal["semantic_normalization"] = "semantic_normalization"
    issues: list[NormalizationIssue]
    checks: dict[str, bool]


class SemanticIR(ContractModel):
    schema_version: Literal["source-flow-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=200)
    nodes: list[SemanticNode] = Field(min_length=1)
    edges: list[SemanticEdge]

    @model_validator(mode="after")
    def valid_graph(self) -> SemanticIR:
        node_ids = [node.id for node in self.nodes]
        edge_ids = [edge.id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("semantic node IDs must be unique")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("semantic edge IDs must be unique")
        known = set(node_ids)
        for edge in self.edges:
            if edge.from_ not in known or edge.to not in known:
                raise ValueError(f"edge {edge.id} references an unknown node")
        return self


class Product1CurrentRevision(ContractModel):
    revision_number: int = Field(ge=1)
    original_prose: str | None = None
    clarified_prose: str | None = None
    source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmed: bool


class Product1SessionResponse(ContractModel):
    id: str = Field(min_length=1)
    state: str
    created_at: str | None = None
    updated_at: str | None = None
    revision_number: int = Field(ge=0)
    current_revision: Product1CurrentRevision | None = None
    confirmed_revision_number: int | None = None
    clarification: dict[str, JsonValue] | None = None
    clarification_history: list[dict[str, JsonValue]] = Field(default_factory=list)
    active_job: dict[str, JsonValue] | None = None
    available_actions: list[str] = Field(default_factory=list)


class Product1ValidationIssue(ContractModel):
    code: str = Field(min_length=1)
    severity: Literal["error", "warning"]
    message: str = Field(min_length=1)
    source_unit_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    repair_instruction: str = Field(min_length=1)


class Product1ValidationReport(ContractModel):
    schema_version: Literal["validation-report-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt: int = Field(ge=1, le=3)
    passed: bool
    renderable: bool
    validator_versions: dict[str, str]
    issues: list[Product1ValidationIssue]


class Product1ResultResponse(ContractModel):
    status: str
    source_units: list[SourceUnit]
    flow: SemanticIR
    validation_report: Product1ValidationReport
    mermaid: str | None = None
    generation_id: str = Field(min_length=1)


class Product1Producer(ContractModel):
    product: Literal["product1"]
    session_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    revision_number: int = Field(ge=1)


class Product1Bundle(ContractModel):
    schema_version: Literal["product1-bundle-0.1"]
    producer: Product1Producer
    confirmed_requirements: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_units: list[SourceUnit]
    semantic_ir: SemanticIR
    product1_validation: Product1ValidationReport
    mermaid: str | None = None
    imported_at: datetime


class Product1BundleV02(ContractModel):
    """Versioned Product 1.1 handoff; 0.1 remains readable as history only."""

    schema_version: Literal["product1-bundle-0.2"]
    producer: Product1Producer
    confirmed_requirements: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_units: list[SourceUnit]
    atomic_behaviour_graph: dict[str, JsonValue]
    atomic_validation: dict[str, JsonValue]
    semantic_ir: SemanticIR
    product1_validation: Product1ValidationReport
    mermaid: str | None = None
    imported_at: datetime


Product1BundleAny = Product1Bundle | Product1BundleV02


class QuestionChoice(ContractModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=160)


class ObligationQuestion(ContractModel):
    text: str = Field(min_length=1, max_length=500)
    choices: list[QuestionChoice] = Field(default_factory=list, max_length=10)
    allow_free_text: bool = False


class ResolutionProvenance(ContractModel):
    semantic_node_ids: list[str] = Field(default_factory=list)
    semantic_edge_ids: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class ObligationResolution(ContractModel):
    value: JsonValue
    resolver: Literal["product2", "user", "environment_admin"]
    resolved_at: datetime
    confidence: Literal["rule", "high", "medium", "low"]
    provenance: ResolutionProvenance
    model_metadata: dict[str, str] | None = None


OBLIGATION_CATEGORIES = (
    "message_content",
    "message_format",
    "message_media",
    "message_template",
    "message_variables",
    "localization_config",
    "interaction_format",
    "interactive_message_config",
    "input_response_type",
    "input_validation",
    "input_result_binding",
    "input_no_response_policy",
    "retry_policy",
    "retry_messages",
    "large_choice_handling",
    "variable_declaration",
    "variable_scope",
    "contact_field_update",
    "data_source",
    "decision_type",
    "condition_expression",
    "branch_fallback",
    "collection_membership_condition",
    "business_action",
    "webhook_request",
    "webhook_response_mapping",
    "webhook_wait_policy",
    "integration_config",
    "error_handler",
    "wait_config",
    "subflow_config",
    "collection_action",
    "message_label_action",
    "staff_message_config",
    "start_other_contact_config",
    "handoff_intent",
    "ticket_config",
    "ticket_outcome_routes",
    "flow_metadata",
    "keyword_config",
    "scheduled_trigger_config",
    "campaign_entry_config",
    "session_window_policy",
    "platform_node_mapping",
    "platform_resource_binding",
    "platform_feature_requirement",
    "unsupported_capability",
    "uncategorized",
)


class Obligation(ContractModel):
    id: str = Field(pattern=r"^O[0-9]{4,}$")
    category: Literal[*OBLIGATION_CATEGORIES]  # type: ignore[misc]
    scope: Literal["flow", "node", "edge", "integration", "deployment"]
    semantic_node_ids: list[str] = Field(default_factory=list)
    semantic_edge_ids: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    current_state: str = Field(min_length=1)
    material_consequence: str = Field(min_length=1)
    resolution_mode: Literal["generated_default", "user_required", "resource_binding", "blocked"]
    required: bool
    question: ObligationQuestion | None = None
    generated_resolution: ObligationResolution | None = None
    depends_on: list[str] = Field(default_factory=list)
    status: Literal[
        "open",
        "generated",
        "answered",
        "bound",
        "deferred_optional",
        "blocked",
        "superseded",
    ]


class AnalysisCoverage(ContractModel):
    semantic_node_ids: list[str]
    semantic_edge_ids: list[str]


class ObligationAnalysis(ContractModel):
    schema_version: Literal["obligation-analysis-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    obligations: list[Obligation]
    coverage: AnalysisCoverage


# Interaction Contracts deliberately model the producer, stored value,
# consumer and semantic outcome as separate objects.  In particular, an
# outcome meaning is never reused as a submitted/router value.
INTERACTION_KINDS = (
    "user_choice",
    "free_text_capture",
    "typed_input_capture",
    "dynamic_choice",
    "input_validation",
    "confirmation",
    "external_result",
    "stored_value_decision",
    "system_condition",
    "collection_membership",
    "message_only_outcome",
    "handoff",
    "subflow_call",
    "timing_event",
    "consent_or_opt_out",
)


class InteractionResolutionProvenance(ContractModel):
    resolver: Literal["product2", "user", "resource_binding"] = "product2"
    basis: Literal[
        "source_explicit",
        "source_inferred",
        "user_answer",
        "resource_binding",
        "verified_capability",
        "generated_copy",
    ]
    obligation_ids: list[str] = Field(default_factory=list)


class InteractionProducer(ContractModel):
    type: Literal[
        "interactive_message",
        "user_text",
        "user_typed_input",
        "webhook_response",
        "contact_field",
        "flow_variable",
        "collection_lookup",
        "subflow_result",
        "system_event",
        "campaign_or_entry_payload",
    ]
    semantic_node_id: str | None = Field(default=None, pattern=r"^N[0-9]{3,}$")
    prompt: str | None = Field(default=None, min_length=1, max_length=2_000)
    presentation: Literal["quick_reply", "list", "text", "date", "time", "media", "none"] = "text"
    locale: str = Field(default="en", min_length=2, max_length=20)


class InteractionValue(ContractModel):
    type: Literal[
        "string",
        "number",
        "boolean",
        "date",
        "datetime",
        "phone",
        "email",
        "location",
        "media",
        "object",
        "list",
    ]
    cardinality: Literal["one", "many"] = "one"
    normalization: Literal[
        "exact_platform_value", "trim_casefold", "typed_parser", "regex_capture", "none"
    ]
    sensitive: bool = False
    nullable: bool = False


class InteractionStorage(ContractModel):
    variable_id: str = Field(pattern=r"^V[0-9]{3,}$")
    variable_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    scope: Literal["flow_result", "contact_field"] = "flow_result"
    write_node_semantic_id: str = Field(pattern=r"^N[0-9]{3,}$")
    contact_field_resource_id: str | None = Field(default=None, pattern=r"^R[0-9]{3,}$")


class InteractionOutcome(ContractModel):
    id: str = Field(pattern=r"^OUT[0-9]{3,}$")
    meaning: str = Field(min_length=1, max_length=500)
    semantic_edge_ids: list[str] = Field(default_factory=list)
    target_semantic_node_id: str | None = Field(default=None, pattern=r"^N[0-9]{3,}$")


class InteractionOption(ContractModel):
    id: str = Field(pattern=r"^OPT[0-9]{3,}$")
    title: str = Field(min_length=1, max_length=160)
    submitted_value: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    semantic_outcome_id: str = Field(pattern=r"^OUT[0-9]{3,}$")
    semantic_edge_ids: list[str] = Field(min_length=1)
    availability: Literal["enabled", "disabled"] = "enabled"


class InteractionConsumerCase(ContractModel):
    accepted_value: str | int | float | bool | list[Any]
    outcome_id: str = Field(pattern=r"^OUT[0-9]{3,}$")
    option_id: str | None = Field(default=None, pattern=r"^OPT[0-9]{3,}$")


class InteractionConsumer(ContractModel):
    type: Literal["switch", "wait", "validation", "external_mapping", "none"]
    operand_variable_id: str | None = Field(default=None, pattern=r"^V[0-9]{3,}$")
    matching: Literal[
        "equals",
        "contains_any",
        "contains_all",
        "contains_phrase",
        "number_equals",
        "number_between_inclusive",
        "matches_regex",
        "is_member",
        "typed",
    ] = "equals"
    cases: list[InteractionConsumerCase] = Field(default_factory=list)
    default_outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")


class InteractionUnmatchedPolicy(ContractModel):
    action: Literal["retry", "reprompt", "fallback_outcome", "handoff", "explicit_end"]
    max_attempts: int = Field(default=3, ge=1, le=10)
    messages: list[str] = Field(default_factory=list, max_length=9)
    on_exhausted_outcome_id: str = Field(pattern=r"^OUT[0-9]{3,}$")


class InteractionNoResponsePolicy(ContractModel):
    timeout_seconds: int = Field(gt=0)
    outcome_id: str = Field(pattern=r"^OUT[0-9]{3,}$")


class InteractionFailurePolicy(ContractModel):
    unmatched: InteractionUnmatchedPolicy | None = None
    no_response: InteractionNoResponsePolicy | None = None
    invalid_input_outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    timeout_outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    empty_outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    stale_selection_outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    failure_outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")


class InteractionDataSource(ContractModel):
    type: Literal["webhook", "flow_variable", "contact_field", "collection"]
    integration_id: str | None = Field(default=None, pattern=r"^I[0-9]{3,}$")
    request_contract_id: str | None = Field(default=None, pattern=r"^WR[0-9]{3,}$")
    items_json_path: str | None = None


class InteractionItemSchema(ContractModel):
    id_path: str = Field(min_length=1)
    title_path: str = Field(min_length=1)
    description_path: str | None = None
    disabled_path: str | None = None


class InteractionSelection(ContractModel):
    stored_value_path: str = Field(min_length=1)
    stored_display_path: str | None = None


class InteractionLimits(ContractModel):
    max_visible_items: int = Field(ge=1, le=100)
    overflow_strategy: Literal["paginate", "narrow", "free_text", "block"]


class InteractionValidationRule(ContractModel):
    type: Literal["syntax", "domain", "external"]
    parser: str = Field(min_length=1, max_length=80)
    constraints: dict[str, Any] = Field(default_factory=dict)
    invalid_message: str = Field(min_length=1, max_length=500)
    max_attempts: int = Field(default=3, ge=1, le=10)


class InteractionContractBase(ContractModel):
    id: str = Field(pattern=r"^IC[0-9]{3,}$")
    kind: str
    semantic_node_ids: list[str] = Field(min_length=1)
    semantic_edge_ids: list[str] = Field(default_factory=list)
    normalized_operation_ids: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    producer: InteractionProducer
    value: InteractionValue
    storage: InteractionStorage | None
    consumer: InteractionConsumer | None
    outcomes: list[InteractionOutcome] = Field(min_length=1)
    failure_policy: InteractionFailurePolicy | None
    resolution_provenance: InteractionResolutionProvenance


class UserChoiceInteractionContract(InteractionContractBase):
    kind: Literal["user_choice"]
    options: list[InteractionOption] = Field(min_length=1, max_length=100)


class FreeTextCaptureInteractionContract(InteractionContractBase):
    kind: Literal["free_text_capture"]
    downstream_consumer: str = Field(min_length=1, max_length=200)


class TypedInputCaptureInteractionContract(InteractionContractBase):
    kind: Literal["typed_input_capture"]
    parser: str = Field(min_length=1, max_length=80)
    validation_rule: InteractionValidationRule | None = None


class DynamicChoiceInteractionContract(InteractionContractBase):
    kind: Literal["dynamic_choice"]
    data_source: InteractionDataSource
    item_schema: InteractionItemSchema
    selection: InteractionSelection
    limits: InteractionLimits
    dynamic_outcomes: dict[
        Literal[
            "selected", "empty", "stale_selection", "fetch_failed", "timeout", "invalid_response"
        ],
        str,
    ]


class InputValidationInteractionContract(InteractionContractBase):
    kind: Literal["input_validation"]
    input_contract_id: str = Field(pattern=r"^IC[0-9]{3,}$")
    rule: InteractionValidationRule


class ConfirmationInteractionContract(InteractionContractBase):
    kind: Literal["confirmation"]
    options: list[InteractionOption] = Field(min_length=2, max_length=2)
    capture_variable_id: str = Field(pattern=r"^V[0-9]{3,}$")


class ExternalResultInteractionContract(InteractionContractBase):
    kind: Literal["external_result"]
    response_variable_id: str = Field(pattern=r"^V[0-9]{3,}$")
    response_mappings: dict[str, str] = Field(min_length=1)
    idempotency_key_source: str | None = None


class StoredValueDecisionInteractionContract(InteractionContractBase):
    kind: Literal["stored_value_decision"]


class SystemConditionInteractionContract(InteractionContractBase):
    kind: Literal["system_condition"]
    expression: str = Field(min_length=1, max_length=500)


class CollectionMembershipInteractionContract(InteractionContractBase):
    kind: Literal["collection_membership"]
    collection_resource_id: str = Field(pattern=r"^R[0-9]{3,}$")


class MessageOnlyOutcomeInteractionContract(InteractionContractBase):
    kind: Literal["message_only_outcome"]


class HandoffInteractionContract(InteractionContractBase):
    kind: Literal["handoff"]
    handoff_mode: Literal["offer", "ticket", "staff_notification", "instructions"]
    resource_id: str | None = Field(default=None, pattern=r"^R[0-9]{3,}$")


class SubflowCallInteractionContract(InteractionContractBase):
    kind: Literal["subflow_call"]
    child_flow_resource_id: str = Field(pattern=r"^R[0-9]{3,}$")
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)


class TimingEventInteractionContract(InteractionContractBase):
    kind: Literal["timing_event"]
    timing_type: Literal["delay", "scheduled_callback", "inactivity_timeout", "session_expiry"]
    timezone: str | None = None


class ConsentOptOutInteractionContract(InteractionContractBase):
    kind: Literal["consent_or_opt_out"]
    policy_name: str = Field(min_length=1, max_length=120)


InteractionContract = Annotated[
    UserChoiceInteractionContract
    | FreeTextCaptureInteractionContract
    | TypedInputCaptureInteractionContract
    | DynamicChoiceInteractionContract
    | InputValidationInteractionContract
    | ConfirmationInteractionContract
    | ExternalResultInteractionContract
    | StoredValueDecisionInteractionContract
    | SystemConditionInteractionContract
    | CollectionMembershipInteractionContract
    | MessageOnlyOutcomeInteractionContract
    | HandoffInteractionContract
    | SubflowCallInteractionContract
    | TimingEventInteractionContract
    | ConsentOptOutInteractionContract,
    Field(discriminator="kind"),
]


class InteractionCoverage(ContractModel):
    semantic_node_ids: list[str]
    semantic_edge_ids: list[str]
    normalized_operation_ids: list[str] = Field(default_factory=list)


class InteractionContractSetV01(ContractModel):
    """Read-only parser for historical Interaction Contract 0.1 artifacts."""

    schema_version: Literal["interaction-contracts-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    contracts: list[InteractionContract]
    coverage: InteractionCoverage

    @model_validator(mode="after")
    def unique_contract_ids(self) -> InteractionContractSet:
        ids = [contract.id for contract in self.contracts]
        if len(ids) != len(set(ids)):
            raise ValueError("interaction contract IDs must be unique")
        return self


class InteractionContractSet(ContractModel):
    schema_version: Literal["interaction-contracts-0.2"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    contracts: list[InteractionContract]
    coverage: InteractionCoverage

    @model_validator(mode="after")
    def unique_contract_ids_and_operations(self) -> InteractionContractSet:
        ids = [contract.id for contract in self.contracts]
        if len(ids) != len(set(ids)):
            raise ValueError("interaction contract IDs must be unique")
        operation_ids = [
            operation_id
            for contract in self.contracts
            for operation_id in contract.normalized_operation_ids
        ]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("normalized operation IDs must have one Interaction Contract owner")
        if any(not contract.normalized_operation_ids for contract in self.contracts):
            raise ValueError("every Interaction Contract must reference a normalized operation")
        return self


class ExecutableLoweringEdge(ContractModel):
    semantic_edge_id: str = Field(pattern=r"^E[0-9]{3,}$")
    from_semantic_node_id: str = Field(pattern=r"^N[0-9]{3,}$")
    to_semantic_node_id: str = Field(pattern=r"^N[0-9]{3,}$")
    owner: Literal[
        "semantic_sequence",
        "interaction_contract",
        "terminal_normalization",
        "pass_through_collapse",
        "unsupported",
    ]
    disposition: Literal[
        "lower_to_executable_edge",
        "lowered_by_interaction",
        "consumed",
        "blocked",
    ]
    interaction_contract_id: str | None = Field(default=None, pattern=r"^IC[0-9]{3,}$")
    outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    reason: str = Field(min_length=1, max_length=1_000)


class ExecutableInternalOperationEdge(ContractModel):
    id: str = Field(pattern=r"^IOE[0-9]{3,}$")
    from_operation_id: str = Field(pattern=r"^NOP_N[0-9]{3,}_[0-9]{2,}$")
    to_operation_id: str = Field(pattern=r"^NOP_N[0-9]{3,}_[0-9]{2,}$")
    reason: str = Field(min_length=1, max_length=500)


class ExecutableLoweringPlan(ContractModel):
    schema_version: Literal["executable-lowering-plan-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_edges: list[ExecutableLoweringEdge]
    internal_operation_edges: list[ExecutableInternalOperationEdge] = Field(default_factory=list)
    coverage: NormalizedSemanticCoverage


class LoweringPlanIssue(ContractModel):
    code: str = Field(min_length=1, max_length=100)
    classification: Literal["repairable_ir", "unsupported_capability"]
    message: str = Field(min_length=1)
    semantic_edge_ids: list[str] = Field(default_factory=list)
    normalized_operation_ids: list[str] = Field(default_factory=list)
    repair_instruction: str = Field(min_length=1)


class ExecutableLoweringPlanValidationReport(ContractModel):
    schema_version: Literal["executable-lowering-plan-validation-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool
    phase: Literal["executable_lowering_plan"] = "executable_lowering_plan"
    issues: list[LoweringPlanIssue]
    checks: dict[str, bool]


class ValidationIssue(ContractModel):
    code: str = Field(min_length=1, max_length=100)
    classification: Literal[
        "repairable_ir",
        "missing_user_decision",
        "missing_resource_binding",
        "unsupported_capability",
        "compiler_defect",
        "environment_failure",
    ]
    message: str = Field(min_length=1)
    executable_node_ids: list[str] = Field(default_factory=list)
    semantic_node_ids: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    repair_instruction: str = Field(min_length=1)


class ValidationReport(ContractModel):
    schema_version: Literal["product2-validation-0.1"]
    passed: bool
    phase: str
    issues: list[ValidationIssue]
    checks: dict[str, bool]


class InteractionValidationReport(ContractModel):
    schema_version: Literal["interaction-validation-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool
    phase: Literal["interaction_contract"] = "interaction_contract"
    issues: list[ValidationIssue]
    checks: dict[str, bool]


class SourceIdentity(ContractModel):
    product1_session_id: str = Field(min_length=1)
    product1_generation_id: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class TargetContract(ContractModel):
    platform: Literal["glific"]
    contract_version: str = Field(min_length=1)


class FlowEntry(ContractModel):
    type: Literal["keyword", "invoked_flow", "campaign", "scheduled", "api", "manual"]
    config_ref: str | None = None


class FlowMetadata(ContractModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_000)
    language: str = "base"
    keywords: list[str] = Field(default_factory=list, max_length=20)
    ignore_other_flow_keywords: bool = False
    expire_after_minutes: int = Field(default=10080, ge=1)
    entry: FlowEntry


class Variable(ContractModel):
    id: str = Field(pattern=r"^V[0-9]{3,}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value_type: Literal[
        "string",
        "number",
        "boolean",
        "date",
        "datetime",
        "phone",
        "email",
        "location",
        "media",
        "object",
        "list",
    ]
    scope: Literal["flow_result", "contact_field"]
    source_node_id: str | None = None
    contact_field_ref: str | None = None
    required: bool = True
    sensitive: bool = False


class Integration(ContractModel):
    id: str = Field(pattern=r"^I[0-9]{3,}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["http"]
    base_url_ref: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    auth_ref: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")


class Resource(ContractModel):
    id: str = Field(pattern=r"^R[0-9]{3,}$")
    kind: Literal[
        "contact_field",
        "collection",
        "interactive_template",
        "child_flow",
        "staff_member",
        "ticket_label",
        "hsm_template",
    ]
    logical_name: str = Field(min_length=1, max_length=160)
    platform_id: str | None = None
    binding_status: Literal["generated_in_package", "existing_bound", "required_at_import"]


class RetryPolicy(ContractModel):
    id: str = Field(pattern=r"^P[0-9]{3,}$")
    type: Literal["input_retry"]
    max_attempts: int = Field(ge=1, le=10)
    messages: list[str] = Field(max_length=9)
    on_exhausted_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")

    @model_validator(mode="after")
    def message_count_matches_attempts(self) -> RetryPolicy:
        if len(self.messages) != self.max_attempts - 1:
            raise ValueError("retry messages must equal max_attempts - 1")
        return self


class InteractiveChoice(ContractModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    option_id: str | None = Field(default=None, pattern=r"^OPT[0-9]{3,}$")
    outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    aliases: list[str] = Field(default_factory=list, max_length=10)


class InteractiveMessage(ContractModel):
    mode: Literal["quick_reply", "list"]
    title: str = Field(min_length=1, max_length=60)
    body: str = Field(min_length=1, max_length=1_024)
    footer: str | None = Field(default=None, max_length=60)
    choices: list[InteractiveChoice] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_limits(self) -> InteractiveMessage:
        if self.mode == "quick_reply" and len(self.choices) < 2:
            raise ValueError("quick_reply requires at least two choices")
        if len({choice.value for choice in self.choices}) != len(self.choices):
            raise ValueError("interactive choice values must be unique")
        return self


class SendMessageConfig(ContractModel):
    format: Literal["text", "media", "interactive", "hsm"]
    text: str | None = Field(default=None, max_length=5_000)
    variable_refs: list[str] = Field(default_factory=list)
    interactive: InteractiveMessage | None = None
    media_ref: str | None = None
    hsm_template_ref: str | None = None

    @model_validator(mode="after")
    def content_matches_format(self) -> SendMessageConfig:
        if self.format in {"text", "interactive"} and not self.text:
            raise ValueError("text is required for text and interactive messages")
        if self.format == "interactive" and self.interactive is None:
            raise ValueError("interactive configuration is required")
        if self.format == "media" and not self.media_ref:
            raise ValueError("media_ref is required for media messages")
        if self.format == "hsm" and not self.hsm_template_ref:
            raise ValueError("hsm_template_ref is required for hsm messages")
        return self


class NoResponseConfig(ContractModel):
    timeout_seconds: int = Field(gt=0)
    edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class WaitForResponseConfig(ContractModel):
    result_variable_id: str = Field(pattern=r"^V[0-9]{3,}$")
    interaction_contract_id: str | None = Field(default=None, pattern=r"^IC[0-9]{3,}$")
    response_type: Literal[
        "any_text",
        "any_words",
        "all_words",
        "phrase",
        "exact_phrase",
        "number",
        "number_between",
        "number_equal",
        "phone",
        "email",
        "image",
        "file",
        "location",
        "regex",
    ]
    criteria: dict[str, JsonValue] = Field(default_factory=dict)
    no_response: NoResponseConfig | None = None
    retry_policy_id: str | None = Field(default=None, pattern=r"^P[0-9]{3,}$")


class SwitchOperand(ContractModel):
    variable_id: str | None = Field(default=None, pattern=r"^V[0-9]{3,}$")
    contact_field_ref: str | None = None
    collection_ref: str | None = None
    expression: str | None = None


class SwitchCase(ContractModel):
    id: str = Field(pattern=r"^C[0-9]{3,}$")
    operator: Literal[
        "equals",
        "contains_any",
        "contains_all",
        "contains_phrase",
        "number_equals",
        "number_between_inclusive",
        "matches_regex",
        "is_member",
        "custom_expression",
    ]
    value: JsonValue
    edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")
    outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    option_id: str | None = Field(default=None, pattern=r"^OPT[0-9]{3,}$")


class SwitchConfig(ContractModel):
    mode: Literal["flow_result", "contact_field", "collection_membership", "custom_expression"]
    operand: SwitchOperand
    cases: list[SwitchCase] = Field(min_length=1)
    default_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")
    interaction_contract_id: str | None = Field(default=None, pattern=r"^IC[0-9]{3,}$")


class UpdateContactConfig(ContractModel):
    contact_field_ref: str = Field(pattern=r"^R[0-9]{3,}$")
    value_variable_id: str = Field(pattern=r"^V[0-9]{3,}$")
    continuation_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class ResponseProperty(ContractModel):
    type: Literal["string", "number", "boolean", "object", "array", "null"]


class ResponseSchema(ContractModel):
    type: Literal["object"]
    properties: dict[str, ResponseProperty]
    required: list[str] = Field(default_factory=list)


class WebhookErrorRoutes(ContractModel):
    timeout: str = Field(pattern=r"^XE[0-9]{3,}$")
    http_error: str = Field(pattern=r"^XE[0-9]{3,}$")
    invalid_response: str = Field(pattern=r"^XE[0-9]{3,}$")
    empty_response: str = Field(pattern=r"^XE[0-9]{3,}$")


class WebhookResponseMapping(ContractModel):
    json_key: str = Field(min_length=1)
    variable_id: str = Field(pattern=r"^V[0-9]{3,}$")


class CallWebhookConfig(ContractModel):
    integration_id: str = Field(pattern=r"^I[0-9]{3,}$")
    result_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    method: Literal["GET", "POST"]
    path: str = Field(min_length=1, max_length=1_000)
    query: dict[str, str] = Field(default_factory=dict)
    headers_refs: list[str] = Field(default_factory=list)
    body: JsonValue = None
    response_schema: ResponseSchema
    response_mappings: list[WebhookResponseMapping] = Field(min_length=1)
    timeout_seconds: int = Field(gt=0, le=60)
    error_routes: WebhookErrorRoutes


class WaitForResultConfig(ContractModel):
    duration_seconds: int = Field(ge=60)
    continuation_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class WaitForTimeConfig(ContractModel):
    duration_seconds: int = Field(gt=0)
    continuation_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class SendStaffMessageConfig(ContractModel):
    staff_resource_id: str = Field(pattern=r"^R[0-9]{3,}$")
    text: str = Field(min_length=1, max_length=2_000)
    continuation_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class LabelIncomingMessageConfig(ContractModel):
    label: str = Field(min_length=1, max_length=80)
    continuation_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class CollectionConfig(ContractModel):
    collection_resource_id: str = Field(pattern=r"^R[0-9]{3,}$")
    continuation_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class EnterFlowConfig(ContractModel):
    child_flow_resource_id: str = Field(pattern=r"^R[0-9]{3,}$")
    continuation_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class StartOtherContactFlowConfig(ContractModel):
    recipient_variable_id: str = Field(pattern=r"^V[0-9]{3,}$")
    child_flow_resource_id: str = Field(pattern=r"^R[0-9]{3,}$")
    continue_current_contact: bool = False
    continuation_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class OpenTicketConfig(ContractModel):
    body_template: str = Field(min_length=1, max_length=2_000)
    assignee_resource_id: str | None = Field(default=None, pattern=r"^R[0-9]{3,}$")
    label_resource_ids: list[str] = Field(default_factory=list)
    success_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")
    failure_edge_id: str = Field(pattern=r"^XE[0-9]{3,}$")


class EndConfig(ContractModel):
    reason: str = Field(min_length=1, max_length=200)


class NodeCommon(ContractModel):
    id: str = Field(pattern=r"^X[0-9]{3,}$")
    semantic_node_ids: list[str] = Field(default_factory=list)
    normalized_operation_ids: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    interaction_contract_id: str | None = Field(default=None, pattern=r"^IC[0-9]{3,}$")
    interaction_contract_ids: list[str] = Field(default_factory=list)


class SendMessageNode(NodeCommon):
    kind: Literal["send_message"]
    config: SendMessageConfig


class WaitForResponseNode(NodeCommon):
    kind: Literal["wait_for_response"]
    config: WaitForResponseConfig


class SwitchNode(NodeCommon):
    kind: Literal["switch"]
    config: SwitchConfig


class UpdateContactNode(NodeCommon):
    kind: Literal["update_contact"]
    config: UpdateContactConfig


class CallWebhookNode(NodeCommon):
    kind: Literal["call_webhook"]
    config: CallWebhookConfig


class WaitForResultNode(NodeCommon):
    kind: Literal["wait_for_result"]
    config: WaitForResultConfig


class WaitForTimeNode(NodeCommon):
    kind: Literal["wait_for_time"]
    config: WaitForTimeConfig


class SendStaffMessageNode(NodeCommon):
    kind: Literal["send_staff_message"]
    config: SendStaffMessageConfig


class LabelIncomingMessageNode(NodeCommon):
    kind: Literal["label_incoming_message"]
    config: LabelIncomingMessageConfig


class AddToCollectionNode(NodeCommon):
    kind: Literal["add_to_collection"]
    config: CollectionConfig


class RemoveFromCollectionNode(NodeCommon):
    kind: Literal["remove_from_collection"]
    config: CollectionConfig


class EnterFlowNode(NodeCommon):
    kind: Literal["enter_flow"]
    config: EnterFlowConfig


class StartOtherContactFlowNode(NodeCommon):
    kind: Literal["start_other_contact_flow"]
    config: StartOtherContactFlowConfig


class OpenTicketNode(NodeCommon):
    kind: Literal["open_ticket"]
    config: OpenTicketConfig


class EndNode(NodeCommon):
    kind: Literal["end"]
    config: EndConfig


ExecutableNode = Annotated[
    SendMessageNode
    | WaitForResponseNode
    | SwitchNode
    | UpdateContactNode
    | CallWebhookNode
    | WaitForResultNode
    | WaitForTimeNode
    | SendStaffMessageNode
    | LabelIncomingMessageNode
    | AddToCollectionNode
    | RemoveFromCollectionNode
    | EnterFlowNode
    | StartOtherContactFlowNode
    | OpenTicketNode
    | EndNode,
    Field(discriminator="kind"),
]


class ExecutableEdge(ContractModel):
    id: str = Field(pattern=r"^XE[0-9]{3,}$")
    from_: str = Field(alias="from", pattern=r"^X[0-9]{3,}$")
    to: str = Field(pattern=r"^X[0-9]{3,}$")
    label: str | None = None
    semantic_edge_ids: list[str] = Field(default_factory=list)
    interaction_contract_id: str | None = Field(default=None, pattern=r"^IC[0-9]{3,}$")
    option_id: str | None = Field(default=None, pattern=r"^OPT[0-9]{3,}$")
    outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    lowering_plan_edge_id: str | None = Field(default=None, pattern=r"^E[0-9]{3,}$")


class ProvenanceRecord(ContractModel):
    path: str = Field(min_length=1)
    kind: Literal[
        "source_ref",
        "semantic_node",
        "semantic_edge",
        "obligation",
        "generated_default",
        "contract_rule",
        "compiler",
    ]
    source_refs: list[SourceRef] = Field(default_factory=list)
    semantic_node_ids: list[str] = Field(default_factory=list)
    semantic_edge_ids: list[str] = Field(default_factory=list)
    normalized_operation_ids: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    interaction_contract_id: str | None = Field(default=None, pattern=r"^IC[0-9]{3,}$")
    option_id: str | None = Field(default=None, pattern=r"^OPT[0-9]{3,}$")
    outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    lowering_plan_edge_id: str | None = Field(default=None, pattern=r"^E[0-9]{3,}$")
    value_origin: (
        Literal[
            "source",
            "contract",
            "user_answer",
            "resource_binding",
            "generated_copy",
            "compiler",
        ]
        | None
    ) = None
    explanation: str = Field(min_length=1)


class ExecutableIRV01(ContractModel):
    schema_version: Literal["glific-executable-ir-0.1"]
    source: SourceIdentity
    target: TargetContract
    flow: FlowMetadata
    variables: list[Variable]
    integrations: list[Integration]
    resources: list[Resource]
    nodes: list[ExecutableNode]
    edges: list[ExecutableEdge]
    policies: list[RetryPolicy]
    provenance: list[ProvenanceRecord]

    @model_validator(mode="after")
    def unique_ids(self) -> ExecutableIRV01:
        for label, values in {
            "variables": [item.id for item in self.variables],
            "integrations": [item.id for item in self.integrations],
            "resources": [item.id for item in self.resources],
            "nodes": [item.id for item in self.nodes],
            "edges": [item.id for item in self.edges],
            "policies": [item.id for item in self.policies],
        }.items():
            if len(values) != len(set(values)):
                raise ValueError(f"{label} IDs must be unique")
        node_ids = {item.id for item in self.nodes}
        for edge in self.edges:
            if edge.from_ not in node_ids or edge.to not in node_ids:
                raise ValueError(f"edge {edge.id} references an unknown node")
        return self


class ExecutableIRV02(ContractModel):
    """Read-only parser for historical Executable IR 0.2 artifacts.

    A contract set is required.  There is intentionally no compatibility
    fallback here: legacy 0.1 evidence is read only through
    ``parse_legacy_executable_ir`` and must be regenerated from Product 1.
    """

    schema_version: Literal["glific-executable-ir-0.2"]
    source: SourceIdentity
    target: TargetContract
    flow: FlowMetadata
    interaction_contracts: InteractionContractSetV01
    variables: list[Variable]
    integrations: list[Integration]
    resources: list[Resource]
    nodes: list[ExecutableNode]
    edges: list[ExecutableEdge]
    policies: list[RetryPolicy]
    provenance: list[ProvenanceRecord]

    @model_validator(mode="after")
    def unique_ids(self) -> ExecutableIRV02:
        for label, values in {
            "variables": [item.id for item in self.variables],
            "integrations": [item.id for item in self.integrations],
            "resources": [item.id for item in self.resources],
            "nodes": [item.id for item in self.nodes],
            "edges": [item.id for item in self.edges],
            "policies": [item.id for item in self.policies],
        }.items():
            if len(values) != len(set(values)):
                raise ValueError(f"{label} IDs must be unique")
        node_ids = {item.id for item in self.nodes}
        for edge in self.edges:
            if edge.from_ not in node_ids or edge.to not in node_ids:
                raise ValueError(f"edge {edge.id} references an unknown node")
        if self.interaction_contracts.source_hash != self.source.source_hash:
            raise ValueError("interaction contract source hash must match Executable IR source")
        return self


class ExecutableIR(ContractModel):
    """Strict Executable IR 0.3 produced by the normalized build pipeline."""

    schema_version: Literal["glific-executable-ir-0.3"]
    source: SourceIdentity
    target: TargetContract
    flow: FlowMetadata
    normalized_plan_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    interaction_contracts: InteractionContractSet
    lowering_plan: ExecutableLoweringPlan
    variables: list[Variable]
    integrations: list[Integration]
    resources: list[Resource]
    nodes: list[ExecutableNode]
    edges: list[ExecutableEdge]
    policies: list[RetryPolicy]
    provenance: list[ProvenanceRecord]

    @model_validator(mode="after")
    def unique_ids_and_hashes(self) -> ExecutableIR:
        for label, values in {
            "variables": [item.id for item in self.variables],
            "integrations": [item.id for item in self.integrations],
            "resources": [item.id for item in self.resources],
            "nodes": [item.id for item in self.nodes],
            "edges": [item.id for item in self.edges],
            "policies": [item.id for item in self.policies],
        }.items():
            if len(values) != len(set(values)):
                raise ValueError(f"{label} IDs must be unique")
        node_ids = {item.id for item in self.nodes}
        for edge in self.edges:
            if edge.from_ not in node_ids or edge.to not in node_ids:
                raise ValueError(f"edge {edge.id} references an unknown node")
        if self.interaction_contracts.source_hash != self.source.source_hash:
            raise ValueError("interaction contract source hash must match Executable IR source")
        if self.lowering_plan.source_hash != self.source.source_hash:
            raise ValueError("lowering plan source hash must match Executable IR source")
        if self.lowering_plan.normalized_plan_hash != self.normalized_plan_hash:
            raise ValueError("lowering plan hash must match Executable IR normalized plan hash")
        return self


def parse_legacy_executable_ir(value: object) -> ExecutableIRV01:
    """Parse an old artifact as untrusted historical evidence only."""

    parsed = ExecutableIRV01.model_validate(value)
    if parsed.schema_version != "glific-executable-ir-0.1":
        raise ValueError("LEGACY_EXECUTABLE_IR_VERSION_INVALID")
    return parsed


def parse_legacy_executable_ir_v02(value: object) -> ExecutableIRV02:
    """Parse an Executable IR 0.2 artifact as historical evidence only."""

    parsed = ExecutableIRV02.model_validate(value)
    if parsed.schema_version != "glific-executable-ir-0.2":
        raise ValueError("LEGACY_EXECUTABLE_IR_VERSION_INVALID")
    return parsed


class TestStimulus(ContractModel):
    type: Literal[
        "select_visible_option",
        "free_text",
        "typed_input",
        "invalid_input",
        "no_response",
        "external_fixture",
        "system_event",
    ]
    option_id: str | None = Field(default=None, pattern=r"^OPT[0-9]{3,}$")
    value: Any = None
    fixture: dict[str, Any] | None = None


class TestExpected(ContractModel):
    stored_value: Any = None
    outcome_id: str | None = Field(default=None, pattern=r"^OUT[0-9]{3,}$")
    target_semantic_node_id: str | None = Field(default=None, pattern=r"^N[0-9]{3,}$")


class BehavioralTestVector(ContractModel):
    id: str = Field(pattern=r"^TV[0-9]{3,}$")
    interaction_contract_id: str = Field(pattern=r"^IC[0-9]{3,}$")
    normalized_operation_ids: list[str] = Field(default_factory=list)
    stimulus: TestStimulus
    expected: TestExpected
    origin: Literal["interaction_contract", "external_fixture", "source_explicit"]


class BehavioralTestVectorSet(ContractModel):
    schema_version: Literal["behavioral-test-vectors-0.1"]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    interaction_contract_revision: str | None = None
    vectors: list[BehavioralTestVector]


class PatchOperation(ContractModel):
    op: Literal["set", "append", "replace"]
    path: str = Field(pattern=r"^/nodes/X[0-9]{3,}/config(?:/[^/]+)+$")
    value: JsonValue
    obligation_id: str | None = Field(default=None, pattern=r"^O[0-9]{4,}$")


class ExecutableIRPatch(ContractModel):
    schema_version: Literal["executable-ir-patch-0.1"]
    base_revision: int = Field(ge=1)
    operations: list[PatchOperation] = Field(max_length=100)


class GlificValidationReport(ContractModel):
    schema_version: Literal["glific-validation-0.1"]
    passed: bool
    phase: str
    issues: list[ValidationIssue]
    checks: dict[str, bool]
    compilation_map: dict[str, list[str]] = Field(default_factory=dict)


class ImportRequest(ContractModel):
    product1_session_id: str = Field(min_length=1)
    acknowledge_product1_warnings: bool = False


class AnswerRequest(ContractModel):
    obligation_id: str = Field(pattern=r"^O[0-9]{4,}$")
    value: JsonValue
    expected_revision: int = Field(default=1, ge=1)


class AnswerBatchRequest(ContractModel):
    answers: list[AnswerRequest] = Field(min_length=1, max_length=5)


class ResourceBindingRequest(ContractModel):
    platform_id: str | None = None
    binding_status: Literal["existing_bound", "required_at_import"]
    expected_revision: int = Field(default=1, ge=1)


def model_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Small named helper used by the schema export command."""

    return model.model_json_schema()
