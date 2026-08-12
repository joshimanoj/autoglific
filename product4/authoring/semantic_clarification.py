from __future__ import annotations

import hashlib
from typing import Any

from product4.contracts.questions import PendingQuestion, QuestionClass
from product4.contracts.session import NodeProposal, OpenPosition

from .interpreter import ModelClient


def semantic_questions(client: ModelClient | None, proposal: NodeProposal, position: OpenPosition) -> list[PendingQuestion]:
    raw: dict[str, Any]
    if client:
        raw = client.clarify_semantics(proposal=proposal, position=position)
    elif proposal.contains_additional_actions:
        raw = {"questions": [{"prompt": "Which single action should this node perform?", "field_path": "capability"}]}
    else:
        raw = {"questions": []}
    result: list[PendingQuestion] = []
    for index, item in enumerate(raw.get("questions") or []):
        path = str(item.get("field_path") or "meaning")
        digest = hashlib.sha256(f"{proposal.id}:semantic:{index}:{path}".encode()).hexdigest()[:12].upper()
        labels = tuple(proposal.choice_labels)
        result.append(PendingQuestion(
            id=f"Q-SEM-{digest}", question_class=QuestionClass.SEMANTIC,
            node_proposal_id=proposal.id, field_path=path,
            prompt=str(item["prompt"]), answer_type=str(item.get("answer_type") or "text"),
            options=list(item.get("options") or []),
            translation_node_id=proposal.translation_node_id,
            capability=proposal.capability,
            position_path=proposal.position_path,
            node_statement=proposal.statement,
            source_excerpt=proposal.source_excerpt,
            choice_labels=list(labels),
        ))
    return result


def apply_semantic_answer(proposal: NodeProposal, field_path: str | None, value: Any) -> NodeProposal:
    if field_path == "capability":
        return proposal.model_copy(update={"capability": str(value), "contains_additional_actions": False})
    if field_path and field_path.startswith("config."):
        config_field = field_path.removeprefix("config.")
        supplied = dict(proposal.supplied_values)
        supplied[config_field] = value
        return proposal.model_copy(
            update={"supplied_values": supplied, "contains_additional_actions": False}
        )
    if field_path == "statement":
        return proposal.model_copy(
            update={"statement": str(value), "contains_additional_actions": False}
        )
    raise ValueError("P4_SEMANTIC_ANSWER_TARGET_UNSUPPORTED")
