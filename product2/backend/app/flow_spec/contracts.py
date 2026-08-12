from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class FlowSpecModel(BaseModel):
    """Strict Product 2-owned models for ``glific-flow-spec-1.0``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NodeId = Annotated[str, Field(pattern=r"^F[0-9]{3,}$")]
ChoiceId = Annotated[str, Field(pattern=r"^CH[0-9]{3,}$")]
DecisionId = Annotated[str, Field(pattern=r"^D[0-9]{3,}$")]
ScenarioId = Annotated[str, Field(pattern=r"^SC[0-9]{3,}$")]
MetadataId = Annotated[str, Field(pattern=r"^M[0-9]{3,}$")]


class FlowSourceRef(FlowSpecModel):
    source_unit_id: str = Field(pattern=r"^S[0-9]{3,}$")
    source_quote: str = Field(min_length=1)


# Public descriptive alias used by callers that name all new models with the
# ``FlowSpec`` prefix.
FlowSpecSourceRef = FlowSourceRef


class SemanticReferenceIds(FlowSpecModel):
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)


class FlowSpecSource(FlowSpecModel):
    product1_session_id: str = Field(min_length=1)
    product1_generation_id: str = Field(min_length=1)
    source_hash: Sha256


class FlowSpecTarget(FlowSpecModel):
    platform: Literal["glific"]
    contract_version: str = Field(min_length=1)
    language: str = Field(min_length=2, max_length=20)
    timezone: str = Field(min_length=1, max_length=80)


class FlowSpecFlow(FlowSpecModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2_000)
    entry_node_id: NodeId
    keywords: list[str] = Field(default_factory=list, max_length=20)


class FlowSpecMessage(FlowSpecModel):
    text: str = Field(min_length=1, max_length=5_000)
    variable_refs: list[str] = Field(default_factory=list)
    locale: str = Field(default="en", min_length=2, max_length=20)


class FlowSpecVariable(FlowSpecModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal[
        "string",
        "number",
        "boolean",
        "date",
        "time",
        "datetime",
        "email",
        "phone",
        "location",
        "media",
        "object",
        "list",
    ]
    scope: Literal["flow", "persistent"] = "flow"
    sensitive: bool = False
    display_companion: str | None = Field(default=None)
    default: Any = None


class FlowSpecResource(FlowSpecModel):
    logical_name: str = Field(min_length=1, max_length=160)
    kind: Literal[
        "contact_field",
        "collection",
        "staff_member",
        "ticket_target",
        "ticket_label",
        "child_flow",
        "hsm_template",
        "media",
        "integration_binding",
    ]
    platform_id: str | None = None
    binding_state: Literal["generated_in_package", "existing_bound", "required_at_import"] = Field(
        default="required_at_import",
        validation_alias=AliasChoices("binding_state", "binding_status"),
        serialization_alias="binding_state",
    )


class FlowSpecIntegration(FlowSpecModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    base_url_ref: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    auth_ref: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")


class FlowSpecRetry(FlowSpecModel):
    max_attempts: int = Field(ge=1, le=10)
    messages: list[str] = Field(default_factory=list, max_length=9)
    on_exhausted_node_id: NodeId


class FlowSpecNoResponse(FlowSpecModel):
    timeout_seconds: int = Field(gt=0)
    next_node_id: NodeId


class FlowSpecChoice(FlowSpecModel):
    id: ChoiceId
    title: str = Field(min_length=1, max_length=160)
    submitted_value: str = Field(min_length=1, max_length=200)
    next_node_id: NodeId


class FlowSpecInputValidation(FlowSpecModel):
    parser: str = Field(min_length=1, max_length=120)
    constraints: dict[str, Any] = Field(default_factory=dict)
    invalid_message: str = Field(min_length=1, max_length=500)


class FlowSpecResponseProperty(FlowSpecModel):
    type: Literal["string", "number", "boolean", "object", "array", "null"]


class FlowSpecResponseSchema(FlowSpecModel):
    type: Literal["object"]
    properties: dict[str, FlowSpecResponseProperty]
    required: list[str] = Field(default_factory=list)


class FlowSpecResponseMapping(FlowSpecModel):
    json_key: str = Field(min_length=1)
    variable: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class FlowSpecWebhookRoutes(FlowSpecModel):
    success_node_id: NodeId
    empty_node_id: NodeId | None = None
    not_found_node_id: NodeId | None = None
    conflict_node_id: NodeId | None = None
    invalid_response_node_id: NodeId | None = None
    http_error_node_id: NodeId | None = None
    timeout_node_id: NodeId | None = None


class FlowSpecEvaluateOperand(FlowSpecModel):
    source: Literal[
        "variable",
        "contact_field",
        "webhook_result",
        "system_expression",
        "collection_membership",
    ]
    variable: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    resource_ref: str | None = None
    expression: str | None = None

    @model_validator(mode="after")
    def source_has_operand(self) -> FlowSpecEvaluateOperand:
        if self.source == "variable" and not self.variable:
            raise ValueError("variable operands require variable")
        if self.source in {"contact_field", "collection_membership"} and not self.resource_ref:
            raise ValueError("contact/collection operands require resource_ref")
        if self.source in {"webhook_result", "system_expression"} and not self.expression:
            raise ValueError("expression operands require expression")
        return self


class FlowSpecEvaluateCase(FlowSpecModel):
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
    value: Any
    next_node_id: NodeId


class FlowSpecWebhook(FlowSpecModel):
    integration_ref: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    method: Literal["GET", "POST"]
    path: str = Field(min_length=1, max_length=1_000)
    query: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    response_schema: FlowSpecResponseSchema
    response_mappings: list[FlowSpecResponseMapping] = Field(default_factory=list)
    timeout_seconds: int = Field(gt=0, le=60)
    routes: FlowSpecWebhookRoutes
    idempotency_key: str | None = None
    mutating: bool = False


class FlowSpecNodeCommon(FlowSpecModel):
    id: NodeId
    type: str
    name: str = Field(min_length=1, max_length=200)
    source_refs: list[FlowSourceRef] = Field(default_factory=list)
    semantic_reference_ids: SemanticReferenceIds = Field(default_factory=SemanticReferenceIds)
    generated_from_decision_ids: list[DecisionId] = Field(default_factory=list)


class SendMessageNode(FlowSpecNodeCommon):
    type: Literal["send_message"]
    message: FlowSpecMessage
    next_node_id: NodeId


class SendMediaNode(FlowSpecNodeCommon):
    type: Literal["send_media"]
    resource_ref: str = Field(min_length=1)
    content_type: Literal["image", "video", "audio", "document"]
    caption: FlowSpecMessage | None = None
    next_node_id: NodeId
    failure_node_id: NodeId


class AskChoiceNode(FlowSpecNodeCommon):
    type: Literal["ask_choice"]
    message: FlowSpecMessage
    presentation: Literal["quick_reply", "list"]
    choices: list[FlowSpecChoice] = Field(min_length=2, max_length=100)
    save_as: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    retry: FlowSpecRetry
    no_response: FlowSpecNoResponse


class AskInputNode(FlowSpecNodeCommon):
    type: Literal["ask_input"]
    message: FlowSpecMessage
    input_type: Literal[
        "text",
        "number",
        "date",
        "time",
        "datetime",
        "email",
        "phone",
        "location",
        "media",
    ]
    save_as: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    validation: FlowSpecInputValidation | None
    retry: FlowSpecRetry
    no_response: FlowSpecNoResponse | None = None
    next_node_id: NodeId


class EvaluateNode(FlowSpecNodeCommon):
    type: Literal["evaluate"]
    operand: FlowSpecEvaluateOperand
    cases: list[FlowSpecEvaluateCase] = Field(min_length=1)
    default_node_id: NodeId


class CallWebhookNode(FlowSpecNodeCommon):
    type: Literal["call_webhook"]
    webhook: FlowSpecWebhook


class UpdateContactNode(FlowSpecNodeCommon):
    type: Literal["update_contact"]
    resource_ref: str = Field(min_length=1)
    source_variable: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    overwrite_policy: Literal["always", "if_empty", "never"]
    success_node_id: NodeId
    failure_node_id: NodeId


class RecordRequestNode(FlowSpecNodeCommon):
    type: Literal["record_request"]
    mechanism: Literal[
        "ticket",
        "contact_fields",
        "collection_and_contact_fields",
        "webhook",
    ]
    fields: dict[str, str] = Field(min_length=1)
    resource_ref: str = Field(min_length=1)
    success_node_id: NodeId


class HandoffNode(FlowSpecNodeCommon):
    type: Literal["handoff"]
    mechanism: Literal["ticket", "staff_notification", "queue_assignment", "instructions_only"]
    resource_refs: list[str] = Field(default_factory=list)
    message: FlowSpecMessage
    context_fields: dict[str, str] = Field(default_factory=dict)
    success_node_id: NodeId
    failure_node_id: NodeId
    exit_automation: bool = True


class DelayNode(FlowSpecNodeCommon):
    type: Literal["delay"]
    duration_seconds: int = Field(gt=0)
    timezone: str = Field(min_length=1)
    next_node_id: NodeId
    failure_node_id: NodeId


class EnterSubflowNode(FlowSpecNodeCommon):
    type: Literal["enter_subflow"]
    resource_ref: str = Field(min_length=1)
    input_mappings: dict[str, str] = Field(default_factory=dict)
    output_mappings: dict[str, str] = Field(default_factory=dict)
    success_node_id: NodeId
    failure_node_id: NodeId


class EndNode(FlowSpecNodeCommon):
    type: Literal["end"]
    reason: str = Field(min_length=1, max_length=200)


FlowSpecNode = Annotated[
    SendMessageNode
    | SendMediaNode
    | AskChoiceNode
    | AskInputNode
    | EvaluateNode
    | CallWebhookNode
    | UpdateContactNode
    | RecordRequestNode
    | HandoffNode
    | DelayNode
    | EnterSubflowNode
    | EndNode,
    Field(discriminator="type"),
]


DecisionCategory = Literal[
    "option_data_source",
    "validation_source",
    "recording_mechanism",
    "external_integration",
    "handoff_destination",
    "persistence_requirement",
    "business_constraint",
    "session_template_requirement",
    "unsupported_behavior_resolution",
]


class ImplementationDecisionChoice(FlowSpecModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)


class ImplementationDecision(FlowSpecModel):
    id: DecisionId
    category: DecisionCategory
    question: str = Field(min_length=1, max_length=500)
    choices: list[ImplementationDecisionChoice] = Field(default_factory=list, max_length=10)
    required: bool = True
    source_refs: list[FlowSourceRef] = Field(default_factory=list)
    depends_on: list[DecisionId] = Field(default_factory=list)
    answer: Any = None
    answer_label: str | None = None
    applied_paths: list[str] = Field(default_factory=list)
    expected_semantic_effect: str = Field(default="", max_length=500)
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def answer_is_declared(self) -> ImplementationDecision:
        declared = {choice.id for choice in self.choices}
        if (
            self.answer is not None
            and declared
            and (not isinstance(self.answer, str) or self.answer not in declared)
        ):
            raise ValueError("implementation decision answer must match a declared choice")
        return self


class ImplementationDecisionSet(FlowSpecModel):
    schema_version: Literal["implementation-decisions-1.0"]
    source_hash: Sha256
    decisions: list[ImplementationDecision] = Field(default_factory=list)
    canonical_hash: Sha256 | None = None
    status: Literal["discovering", "awaiting_answers", "resolved", "blocked"] = "discovering"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def unique_decision_ids(self) -> ImplementationDecisionSet:
        ids = [item.id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("implementation decision IDs must be unique")
        return self


class ImplementationDecisionAnswer(FlowSpecModel):
    decision_id: DecisionId
    answer: Any
    expected_revision: int = Field(default=1, ge=1)


class ImplementationDecisionAnswerBatch(FlowSpecModel):
    answers: list[ImplementationDecisionAnswer] = Field(min_length=1, max_length=10)


class SourceCoverageEntry(FlowSpecModel):
    source_unit_id: str = Field(pattern=r"^S[0-9]{3,}$")
    source_quote: str = Field(min_length=1)
    status: Literal["covered", "informational", "blocked"]
    flow_node_ids: list[NodeId] = Field(default_factory=list)
    semantic_requirement_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class ScenarioStimulus(FlowSpecModel):
    type: Literal[
        "select_visible_choice",
        "enter_text",
        "enter_invalid_text",
        "no_response",
        "webhook_fixture",
        "recording_fixture",
        "contact_state",
    ]
    node_id: NodeId | None = None
    visible_title: str | None = None
    value: Any = None
    fixture: dict[str, Any] | None = None


class AcceptanceScenario(FlowSpecModel):
    id: ScenarioId
    name: str | None = Field(default=None, max_length=240)
    source_refs: list[FlowSourceRef] = Field(default_factory=list)
    stimuli: list[ScenarioStimulus] = Field(default_factory=list)
    expected_terminal_reason: str | None = Field(default=None, max_length=180)
    expected_route_node_id: NodeId
    expected_source_outcomes: list[str] = Field(default_factory=list)


class FlowSpecMetadata(FlowSpecModel):
    """Operational-policy pass-through bound to a compiled node/edge.

    Produced by Product 3 from the source-flow-0.1 ``metadata`` array and
    preserved as typed data so a runtime/executor can honor retry counts,
    webhook calls, and escalation without the diagram carrying them.
    """

    id: MetadataId
    kind: str = Field(min_length=1, max_length=40)
    target_id: str = Field(min_length=1)
    target_kind: Literal["node", "edge"] = "node"
    params: dict[str, Any] = Field(default_factory=dict)
    source_ref: FlowSourceRef


class GlificFlowSpec(FlowSpecModel):
    schema_version: Literal["glific-flow-spec-1.0"]
    source: FlowSpecSource
    target: FlowSpecTarget
    flow: FlowSpecFlow
    variables: list[FlowSpecVariable] = Field(default_factory=list)
    resources: list[FlowSpecResource] = Field(default_factory=list)
    integrations: list[FlowSpecIntegration] = Field(default_factory=list)
    nodes: list[FlowSpecNode] = Field(min_length=1)
    implementation_decisions: list[ImplementationDecision] = Field(default_factory=list)
    metadata: list[FlowSpecMetadata] = Field(default_factory=list)
    source_coverage: list[SourceCoverageEntry] = Field(default_factory=list)
    acceptance_scenarios: list[AcceptanceScenario] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_top_level_ids(self) -> GlificFlowSpec:
        node_ids = [node.id for node in self.nodes]
        variable_names = [variable.name for variable in self.variables]
        resource_names = [resource.logical_name for resource in self.resources]
        integration_names = [integration.name for integration in self.integrations]
        decision_ids = [decision.id for decision in self.implementation_decisions]
        scenario_ids = [scenario.id for scenario in self.acceptance_scenarios]
        for label, values in {
            "node": node_ids,
            "variable": variable_names,
            "resource": resource_names,
            "integration": integration_names,
            "decision": decision_ids,
            "scenario": scenario_ids,
        }.items():
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} identifiers")
        return self


__all__ = [
    "AcceptanceScenario",
    "AskChoiceNode",
    "AskInputNode",
    "CallWebhookNode",
    "DelayNode",
    "EndNode",
    "EnterSubflowNode",
    "EvaluateNode",
    "FlowSpecChoice",
    "FlowSpecEvaluateOperand",
    "FlowSpecMessage",
    "FlowSpecNode",
"FlowSpecResource",
    "FlowSpecSource",
    "FlowSpecSourceRef",
    "FlowSpecVariable",
    "FlowSpecMetadata",
    "GlificFlowSpec",
    "HandoffNode",
    "ImplementationDecision",
    "ImplementationDecisionAnswer",
    "ImplementationDecisionAnswerBatch",
    "ImplementationDecisionChoice",
    "ImplementationDecisionSet",
    "RecordRequestNode",
    "SendMediaNode",
    "SendMessageNode",
    "UpdateContactNode",
]
