"""Deterministic validation and commit helpers for flow trigger metadata."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from product4.contracts.questions import PendingQuestion, QuestionClass
from product4.contracts.session import AuthoringSession, NodeProposal
from product4.contracts.trigger import (
    MAX_TRIGGER_KEYWORDS,
    FlowTriggerIntent,
    FlowTriggerMetadata,
    TriggerKeywordRecord,
    trigger_provenance_reference,
    validate_keyword_value,
)


def validate_provider_trigger_intent(
    intent: FlowTriggerIntent | None,
    statement: str,
) -> None:
    """Fail closed unless every proposed keyword is explicitly source-grounded."""

    if intent is None or intent.status == "none":
        if intent is not None and intent.keywords:
            raise ValueError("P4_TRIGGER_NONE_HAS_KEYWORDS")
        if intent is not None and intent.question:
            raise ValueError("P4_TRIGGER_NONE_HAS_QUESTION")
        return
    if intent.status == "ambiguous":
        if intent.keywords:
            raise ValueError("P4_TRIGGER_AMBIGUOUS_HAS_KEYWORDS")
        if not intent.question or not intent.question.strip():
            raise ValueError("P4_TRIGGER_AMBIGUOUS_QUESTION_MISSING")
        return
    if intent.status != "explicit" or not intent.keywords:
        raise ValueError("P4_TRIGGER_EXPLICIT_KEYWORDS_MISSING")
    if intent.question:
        raise ValueError("P4_TRIGGER_EXPLICIT_HAS_QUESTION")
    if not statement or not isinstance(statement, str):
        raise ValueError("P4_TRIGGER_SOURCE_MISSING")
    for keyword in intent.keywords:
        value = validate_keyword_value(keyword.value)
        if keyword.source_excerpt not in statement:
            raise ValueError("P4_TRIGGER_SOURCE_EXCERPT_MISMATCH")
        if value not in keyword.source_excerpt:
            raise ValueError("P4_TRIGGER_KEYWORD_NOT_IN_SOURCE_EXCERPT")


def trigger_questions(
    session: AuthoringSession,
    proposal: NodeProposal,
) -> list[PendingQuestion]:
    intent = proposal.flow_trigger_intent
    if intent is None or intent.status == "none":
        return []
    if session.flow_trigger_metadata is not None:
        raise ValueError("P4_TRIGGER_METADATA_ALREADY_COMMITTED")
    if intent.status == "explicit":
        return []
    prompt = intent.question or (
        "What exact keyword or keywords should start this flow? "
        "Enter the approved spelling, separated by commas."
    )
    digest = hashlib.sha256(
        f"{proposal.id}:semantic:flow.trigger_keywords:{prompt}".encode()
    ).hexdigest()[:12].upper()
    return [PendingQuestion(
        id=f"Q-SEM-{digest}",
        question_class=QuestionClass.SEMANTIC,
        node_proposal_id=proposal.id,
        field_path="flow.trigger_keywords",
        prompt=prompt,
        answer_type="text",
        translation_node_id=proposal.translation_node_id,
        capability="flow_metadata",
        position_path=proposal.position_path,
        node_statement=proposal.statement,
        source_excerpt=proposal.source_excerpt,
        choice_labels=list(proposal.choice_labels),
    )]


def parse_trigger_answer(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        raw_values = list(value)
    elif isinstance(value, str):
        raw_values = re.split(r"[,\n]", value)
    else:
        raise TypeError("P4_TRIGGER_ANSWER_NOT_TEXT")
    result: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            raise TypeError("P4_TRIGGER_ANSWER_NOT_TEXT")
        candidate = raw.strip()
        validate_keyword_value(candidate)
        if candidate not in result:
            result.append(candidate)
    if not result or len(result) > MAX_TRIGGER_KEYWORDS:
        raise ValueError("P4_TRIGGER_ANSWER_COUNT_INVALID")
    return tuple(result)


def commit_trigger_metadata(
    session: AuthoringSession,
    proposal: NodeProposal,
) -> FlowTriggerMetadata | None:
    """Return the next committed state; callers assign it transactionally."""

    intent = proposal.flow_trigger_intent
    answer_values = proposal.flow_trigger_answer
    if intent is None and not answer_values:
        return session.flow_trigger_metadata
    if session.flow_trigger_metadata is not None:
        raise ValueError("P4_TRIGGER_METADATA_ALREADY_COMMITTED")

    if answer_values:
        incoming = [
            TriggerKeywordRecord(
                value=value,
                source_excerpt=value,
                source="confirmed_user_decision",
                reference=trigger_provenance_reference(value),
            )
            for value in answer_values
        ]
    elif intent is not None and intent.status == "explicit":
        incoming = []
        seen_incoming: set[str] = set()
        for item in intent.keywords:
            if item.value in seen_incoming:
                continue
            seen_incoming.add(item.value)
            incoming.append(TriggerKeywordRecord(
                value=item.value,
                source_excerpt=item.source_excerpt,
                source="confirmed_prose",
                reference=trigger_provenance_reference(item.value),
            ))
    else:
        raise ValueError("P4_TRIGGER_AMBIGUOUS_UNANSWERED")
    if not incoming or len(incoming) > MAX_TRIGGER_KEYWORDS:
        raise ValueError("P4_TRIGGER_METADATA_COUNT_INVALID")
    return FlowTriggerMetadata(keywords=incoming)
