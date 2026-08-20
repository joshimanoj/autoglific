from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .questions import ContextualQuestionHint, PendingQuestion
from .trigger import FlowTriggerIntent, FlowTriggerMetadata


class SessionState(str, Enum):
    EDITING = "editing"
    WAITING_FOR_ANSWER = "waiting_for_answer"
    READY_FOR_REVIEW = "ready_for_review"
    FROZEN = "frozen"
    BLOCKED = "blocked"


class OpenPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    parent_node_id: str | None = None
    exit_key: str = "entry"
    branch_path: tuple[str, ...] = ()
    claimed_by: str | None = None


class DraftNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    capability: str
    config: dict[str, Any]
    source_statement: str
    source_excerpt: str | None = None
    incoming_position_id: str


class DraftEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source_id: str
    target_id: str
    exit_key: str
    stable_value: str | None = None
    label: str | None = None


class SegmentRoutingOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str


class SegmentRouting(BaseModel):
    """Workbench-only semantic routing intent carried with a segment proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "current_branch",
        "existing_branch",
        "choice_group",
        "clarification",
    ] = "current_branch"
    scope: Literal["single_branch", "descendant_leaves"] = "single_branch"
    choice_group_id: str | None = None
    option_id: str | None = None
    question: str | None = None
    options: list[SegmentRoutingOption] = Field(default_factory=list)
    source_excerpt: str | None = None


class NodeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    capability: str
    supplied_values: dict[str, Any] = Field(default_factory=dict)
    statement: str
    source_excerpt: str | None = None
    contains_additional_actions: bool = False
    incoming_position_id: str
    acquisition_sources: dict[str, str] = Field(default_factory=dict)
    acquisition_source_quotes: dict[str, str] = Field(default_factory=dict)
    # Validated node-local context used to render deterministic questions. It
    # is never populated from scorer/gold/package data.
    translation_node_id: str = "unbound"
    position_path: tuple[str, ...] = ()
    choice_labels: tuple[str, ...] = ()
    routing: SegmentRouting = Field(default_factory=SegmentRouting)
    target_position_id: str | None = None
    contextual_questions: dict[str, ContextualQuestionHint] = Field(default_factory=dict)
    # Workbench-only semantic context. These values are transient orchestration
    # references and are not part of the frozen authoring package.
    semantic_concept: str | None = None
    capture_reference: str | None = None
    capture_reference_question: str | None = None
    capture_reference_options: dict[str, str] = Field(default_factory=dict)
    # Flow-level trigger intent is transient semantic input, not an authored
    # capability or node configuration. The service commits it separately.
    flow_trigger_intent: FlowTriggerIntent | None = None
    flow_trigger_answer: tuple[str, ...] = ()


class AnswerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    question_id: str
    question_class: str
    proposal_id: str
    node_id: str | None = None
    capability: str
    field_path: str
    prompt: str
    value: Any
    source: str
    rationale: str | None = None
    answered_at: str | None = None
    model_identity: str | None = None
    prior_answer_context_hash: str | None = None
    revision: int = Field(ge=1)


class RevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    parent_revision: int | None = None
    operation: str
    canonical_hash: str


class AuthoringSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "product4-session-1.0"
    id: str
    title: str
    original_brief: str | None = None
    semantic_translation_hash: str | None = None
    state: SessionState = SessionState.EDITING
    revision: int = 1
    nodes: list[DraftNode] = Field(default_factory=list)
    edges: list[DraftEdge] = Field(default_factory=list)
    open_positions: list[OpenPosition] = Field(
        default_factory=lambda: [OpenPosition(id="POS-0001")]
    )
    active_position_id: str | None = None
    active_proposal: NodeProposal | None = None
    # Remaining nodes from one provider-decomposed branch instruction. They
    # stay outside the package until each node has been clarified and
    # committed in order.
    queued_proposals: list[NodeProposal] = Field(default_factory=list)
    # Maps provider/opaque capture references to committed capture node ids.
    # The package builder does not consume this workbench-only map.
    capture_reference_map: dict[str, str] = Field(default_factory=dict)
    pending_questions: list[PendingQuestion] = Field(default_factory=list)
    revisions: list[RevisionRecord] = Field(default_factory=list)
    answer_records: list[AnswerRecord] = Field(default_factory=list)
    flow_trigger_metadata: FlowTriggerMetadata | None = None
    # Prose-first planning stays outside authored nodes until every required
    # configuration answer is available and deterministic projection succeeds.
    atomic_workbench: dict[str, Any] | None = None
    frozen_package: dict[str, Any] | None = None
    frozen_hash: str | None = None
    blocked_error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> AuthoringSession:
        if self.state is SessionState.WAITING_FOR_ANSWER and not self.pending_questions:
            raise ValueError("waiting_for_answer requires pending questions")
        if self.pending_questions and self.active_proposal is None:
            raise ValueError("pending questions require an active proposal")
        if self.state in {SessionState.READY_FOR_REVIEW, SessionState.FROZEN} and self.queued_proposals:
            raise ValueError("review or frozen state cannot have queued proposals")
        if self.state is SessionState.READY_FOR_REVIEW and self.open_positions:
            raise ValueError("ready_for_review cannot have open positions")
        if self.state is SessionState.FROZEN and not (self.frozen_package and self.frozen_hash):
            raise ValueError("frozen state requires a package and hash")
        if self.state is SessionState.BLOCKED and not self.blocked_error:
            raise ValueError("blocked state requires a typed error")
        return self
