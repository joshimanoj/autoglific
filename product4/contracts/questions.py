from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class QuestionClass(str, Enum):
    SEMANTIC = "semantic"
    CONFIGURATION = "configuration"


QUESTION_CONTEXT_VERSION = "product4-question-context-2.0-node-local"


class ContextualQuestionHint(BaseModel):
    """Provider wording for a real registry-owned configuration gap."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    answer_type: Literal["text", "boolean", "options", "json"] = "text"
    options: list[str] = Field(default_factory=list)


class PendingQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^Q-(SEM|CFG)-[A-F0-9]{12}$")
    question_class: QuestionClass
    node_proposal_id: str
    field_path: str | None = None
    prompt: str = Field(min_length=1)
    answer_type: Literal["text", "boolean", "options", "json"] = "text"
    options: list[str] = Field(default_factory=list)
    # This context is deliberately flat and allowlisted. It is the only node
    # context that may cross the authoring -> user/simulated-user boundary.
    translation_node_id: str = Field(default="unbound", min_length=1)
    capability: str = Field(default="unbound", min_length=1)
    position_path: tuple[str, ...] = ()
    node_statement: str = Field(default="Current node purpose unavailable", min_length=1)
    source_excerpt: str | None = None
    choice_labels: list[str] = Field(default_factory=list)
    contextual: bool = False


class QuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str
    value: Any
    decision_source: Literal[
        "confirmed_user_decision", "simulated_user_evaluation_decision"
    ] = "confirmed_user_decision"
    rationale: str | None = None
    answered_at: str | None = None
    model_identity: str | None = None
    prior_answer_context_hash: str | None = None
