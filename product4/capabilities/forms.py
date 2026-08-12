from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from product4.contracts.questions import (
    ContextualQuestionHint,
    PendingQuestion,
    QuestionClass,
)

from .registry import INPUT_TYPES, AcquisitionPolicy, require_capability

_INTERNAL_PROMPT_TOKENS = re.compile(
    r"(?:source[\s_-]*variable|save[\s_-]*as|stable[\s_-]*value|capture[\s_-]*reference|"
    r"flow[\s_-]*variable|input[\s_-]*type|field[\s_-]*(?:path|name)|"
    r"translation[\s_-]*node|capability[\s_-]*id|(?:OPT|CAP|PROP|T)-[A-Z0-9_-]+)",
    re.IGNORECASE,
)


def apply_non_user_policies(capability: str, values: dict) -> dict:
    result = copy.deepcopy(values)
    definition = require_capability(capability)
    for field in definition.fields:
        if field.path in result:
            continue
        if field.policy is AcquisitionPolicy.DEFAULTED:
            result[field.path] = copy.deepcopy(field.default)
    return result


def _clean_purpose(value: str) -> str:
    """Keep validated node purpose readable without carrying raw formatting."""
    purpose = " ".join(str(value).split()).strip().rstrip(".!?")
    return purpose or "the current node purpose"


def safe_contextual_prompt(prompt: str | None, fallback: str) -> str:
    """Keep provider wording human-facing; registry fallback remains authoritative."""
    normalized = " ".join(str(prompt or "").split()).strip()
    if not normalized or _INTERNAL_PROMPT_TOKENS.search(normalized):
        return fallback
    return normalized


def _human_subject(value: str) -> str:
    purpose = _clean_purpose(value)
    purpose = re.split(r"\b(?:then|and then)\b", purpose, maxsplit=1, flags=re.IGNORECASE)[0]
    purpose = re.sub(
        r"^\s*(?:save|store|persist|record|write)\s+(?:the|their|this|that|an?|user['’]s|person['’]s)\s+",
        "",
        purpose,
        flags=re.IGNORECASE,
    )
    return purpose.strip() or "answer"


def _workbench_question_prompt(
    capability: str,
    field_path: str,
    values: Mapping[str, Any],
    *,
    node_statement: str,
    position_path: Iterable[str],
    choice_labels: Iterable[str],
) -> str:
    subject = _human_subject(node_statement)
    if capability == "send_text_message" and field_path == "copy":
        return f"What message should people receive for {subject}?"
    if capability == "capture_user_input":
        if field_path == "prompt":
            return f"What question should people see for {subject}?"
        if field_path == "input_type":
            return f"What kind of answer should people give for {subject}?"
        if field_path == "validation":
            return f"Should the answer for {subject} have any extra validation rules?"
    if capability == "fixed_choice":
        if field_path == "title":
            return "What question should introduce these choices?"
        if field_path == "options":
            return "What choices should people see?"
    if capability == "persist_contact_field" and field_path == "field_name":
        return f"Where should the {subject} be saved in the contact record?"
    if capability == "end" and field_path == "reason":
        return "What should we call this completed branch?"
    return contextual_question_prompt(
        capability,
        field_path,
        values,
        node_statement=node_statement,
        position_path=position_path,
        choice_labels=choice_labels,
    )


def render_branch(position_path: Iterable[str]) -> str:
    path = tuple(str(segment) for segment in position_path)
    if not path:
        return "main flow"
    return " > ".join(f'branch "{segment}"' for segment in path)


def _choice_labels(values: Mapping[str, Any], labels: Iterable[str]) -> list[str]:
    supplied = values.get("options")
    if isinstance(supplied, list):
        own_labels = [
            str(item.get("label"))
            for item in supplied
            if isinstance(item, Mapping) and item.get("label")
        ]
        if own_labels:
            return own_labels
    return [str(label) for label in labels if str(label).strip()]


def contextual_question_prompt(
    capability: str,
    field_path: str,
    values: Mapping[str, Any],
    *,
    node_statement: str,
    position_path: Iterable[str] = (),
    choice_labels: Iterable[str] = (),
) -> str:
    """Render one deterministic, registry-owned configuration question."""
    purpose = _clean_purpose(node_statement)
    branch = render_branch(position_path)
    if capability == "capture_user_input":
        if field_path == "prompt":
            return f"For {purpose} in {branch}, what exact input prompt should be shown?"
        if field_path == "input_type":
            input_type_field = next(
                field for field in require_capability(capability).fields
                if field.path == field_path
            )
            input_types = ", ".join(str(option) for option in input_type_field.options)
            return (
                f"What input type should be used for {purpose} in {branch}? "
                f"Choose {input_types}."
            )
        if field_path == "save_as":
            return f"What flow variable should store {purpose} in {branch}?"
        if field_path == "validation":
            return f"What validation applies to {purpose} in {branch}? Empty object for none."
    if capability == "send_text_message" and field_path == "copy":
        return f"For {purpose} in {branch}, what exact message should be sent?"
    if capability == "fixed_choice":
        if field_path == "title":
            labels = _choice_labels(values, choice_labels)
            rendered = ", ".join(f'"{label}"' for label in labels) or "the listed options"
            return (
                f"For the choice in {branch} with options {rendered}, "
                "what exact title should be shown?"
            )
        if field_path == "options":
            return f"For the choice in {branch}, what option labels and stable values should be used?"
    if capability == "persist_contact_field":
        if field_path == "source_variable":
            return f"What flow variable should be saved for {purpose} in {branch}?"
        if field_path == "field_name":
            return f"Which contact field should receive {purpose} in {branch}?"
    if capability == "end" and field_path == "reason":
        return f"After {purpose} in {branch}, what terminal reason should be recorded?"
    return f"What value should be used for {field_path} for {purpose} in {branch}?"


def configuration_questions(
    proposal_id: str,
    capability: str,
    values: dict,
    *,
    translation_node_id: str | None = None,
    position_path: Iterable[str] = (),
    node_statement: str | None = None,
    source_excerpt: str | None = None,
    choice_labels: Iterable[str] = (),
    contextual_questions: Mapping[str, ContextualQuestionHint] | None = None,
    workbench_mode: bool = False,
) -> list[PendingQuestion]:
    definition = require_capability(capability)
    resolved_translation_node_id = translation_node_id or proposal_id
    resolved_statement = node_statement or capability
    resolved_path = tuple(str(segment) for segment in position_path)
    resolved_labels = _choice_labels(values, choice_labels)
    contextual_questions = contextual_questions or {}
    fields_by_path = {field.path: field for field in definition.fields}
    for field_path, hint in contextual_questions.items():
        field = fields_by_path.get(field_path)
        if field is None:
            raise ValueError(f"P4_CONTEXTUAL_QUESTION_FIELD_INVALID:{capability}:{field_path}")
        if hint.answer_type != field.answer_type:
            raise ValueError(f"P4_CONTEXTUAL_QUESTION_TYPE_INVALID:{capability}:{field_path}")
        if list(hint.options) != list(field.options):
            raise ValueError(f"P4_CONTEXTUAL_QUESTION_OPTIONS_INVALID:{capability}:{field_path}")
    questions: list[PendingQuestion] = []
    for field in definition.fields:
        if workbench_mode and (
            (capability == "capture_user_input" and field.path == "save_as")
            or (capability == "persist_contact_field" and field.path == "source_variable")
        ):
            continue
        if field.policy in {AcquisitionPolicy.USER_REQUIRED, AcquisitionPolicy.DERIVED} and field.path not in values:
            digest = hashlib.sha256(f"{proposal_id}:configuration:{field.path}".encode()).hexdigest()[:12].upper()
            hint = contextual_questions.get(field.path)
            prompt_context = (
                source_excerpt
                if workbench_mode and source_excerpt
                else resolved_statement
            )
            fallback = (
                _workbench_question_prompt(
                    capability,
                    field.path,
                    values,
                    node_statement=prompt_context,
                    position_path=resolved_path,
                    choice_labels=resolved_labels,
                )
                if workbench_mode
                else contextual_question_prompt(
                    capability,
                    field.path,
                    values,
                    node_statement=prompt_context,
                    position_path=resolved_path,
                    choice_labels=resolved_labels,
                )
            )
            questions.append(PendingQuestion(
                id=f"Q-CFG-{digest}", question_class=QuestionClass.CONFIGURATION,
                node_proposal_id=proposal_id, field_path=field.path,
                prompt=(
                    safe_contextual_prompt(hint.prompt, fallback)
                    if hint is not None
                    else fallback
                ),
                answer_type=field.answer_type,
                options=list(field.options),
                translation_node_id=resolved_translation_node_id,
                capability=capability,
                position_path=resolved_path,
                node_statement=resolved_statement,
                source_excerpt=source_excerpt,
                choice_labels=resolved_labels if capability == "fixed_choice" else [],
                contextual=hint is not None,
            ))
    return questions


def validate_complete(capability: str, values: dict) -> dict:
    definition = require_capability(capability)
    known = {field.path for field in definition.fields}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"P4_UNKNOWN_CONFIGURATION_FIELDS: {sorted(unknown)}")
    missing = [field.path for field in definition.fields if field.policy in {AcquisitionPolicy.USER_REQUIRED, AcquisitionPolicy.DERIVED} and field.path not in values]
    if missing:
        raise ValueError(f"P4_CONFIGURATION_INCOMPLETE: {missing}")
    if capability == "capture_user_input":
        if values.get("input_type") not in set(INPUT_TYPES):
            raise ValueError("P4_INPUT_TYPE_INVALID")
        if not isinstance(values.get("validation"), dict):
            raise ValueError("P4_INPUT_VALIDATION_INVALID")
    if capability == "fixed_choice":
        options = values.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 10:
            raise ValueError("P4_CHOICE_OPTIONS_INVALID: two to ten options are required")
        labels = [item.get("label") for item in options if isinstance(item, dict)]
        stable = [item.get("value") for item in options if isinstance(item, dict)]
        if len(labels) != len(options) or any(not item for item in labels + stable):
            raise ValueError("P4_CHOICE_OPTIONS_INVALID: each option needs label and value")
        if len(set(labels)) != len(labels) or len(set(stable)) != len(stable):
            raise ValueError("P4_CHOICE_OPTIONS_INVALID: labels and values must be unique")
    return values
