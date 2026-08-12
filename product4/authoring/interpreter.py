from __future__ import annotations

import hashlib
from typing import Any, Protocol

from product4.capabilities.registry import (
    REGISTRY,
    UnsupportedCapabilityError,
    classify_tokens,
)
from product4.contracts.questions import ContextualQuestionHint
from product4.contracts.session import NodeProposal, OpenPosition, SegmentRouting
from product4.contracts.trigger import FlowTriggerIntent


class AmbiguousInstructionError(ValueError):
    code = "P4_AMBIGUOUS_INSTRUCTION"


class ModelClient(Protocol):
    def interpret(
        self,
        *,
        statement: str,
        position: OpenPosition,
        registry: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def clarify_semantics(self, *, proposal: NodeProposal, position: OpenPosition) -> dict[str, Any]: ...


class RegistryInterpreter:
    """One-call interpreter. Production clients return structured output only."""

    def __init__(self, client: ModelClient | None = None):
        self.client = client

    def interpret(
        self,
        statement: str,
        position: OpenPosition,
        *,
        context: dict[str, Any] | None = None,
    ) -> NodeProposal:
        if self.client:
            raw = self.client.interpret(
                statement=statement,
                position=position,
                registry={key: value.model_dump(mode="json") for key, value in REGISTRY.items()},
                context=context,
            )
        else:
            matches = classify_tokens(statement)
            if not matches:
                raise UnsupportedCapabilityError(statement)
            if len(matches) != 1:
                raise AmbiguousInstructionError(f"{AmbiguousInstructionError.code}: {matches}")
            raw = {"capability": matches[0], "supplied_values": {}, "contains_additional_actions": False}
        return self._proposal_from_raw(raw, statement, position)

    @staticmethod
    def _proposal_from_raw(
        raw: dict[str, Any],
        statement: str,
        position: OpenPosition,
        *,
        proposal_key: str | None = None,
    ) -> NodeProposal:
        capability = str(raw.get("capability") or "")
        if capability not in REGISTRY:
            raise UnsupportedCapabilityError(capability)
        digest_input = f"{position.id}:{statement}"
        if proposal_key:
            digest_input = f"{digest_input}:{proposal_key}"
        digest = hashlib.sha256(digest_input.encode()).hexdigest()[:12].upper()
        definition = REGISTRY[capability]
        known_fields = {field.path for field in definition.fields}
        contextual_questions: dict[str, ContextualQuestionHint] = {}
        raw_contextual_questions = raw.get("contextual_questions") or {}
        if not isinstance(raw_contextual_questions, dict):
            raise TypeError("P4_CONTEXTUAL_QUESTION_INVALID")
        for field_path, hint in raw_contextual_questions.items():
            if field_path not in known_fields:
                raise ValueError(f"P4_CONTEXTUAL_QUESTION_FIELD_INVALID:{capability}:{field_path}")
            contextual_questions[str(field_path)] = ContextualQuestionHint.model_validate(hint)
        raw_capture_options = raw.get("capture_reference_options") or {}
        if not isinstance(raw_capture_options, dict):
            raise TypeError("P4_CAPTURE_REFERENCE_OPTIONS_INVALID")
        return NodeProposal(
            id=f"PROP-{digest}", capability=capability,
            supplied_values=dict(raw.get("supplied_values") or {}), statement=statement,
            acquisition_sources=dict(raw.get("acquisition_sources") or {}),
            acquisition_source_quotes=dict(raw.get("acquisition_source_quotes") or {}),
            contains_additional_actions=bool(raw.get("contains_additional_actions")),
            incoming_position_id=position.id,
            source_excerpt=(str(raw["source_excerpt"]) if raw.get("source_excerpt") else None),
            translation_node_id=str(raw.get("translation_node_id") or "unbound"),
            position_path=tuple(str(item) for item in (raw.get("position_path") or ())),
            choice_labels=tuple(str(item) for item in (raw.get("choice_labels") or ())),
            routing=SegmentRouting.model_validate(raw.get("routing") or {}),
            target_position_id=(
                str(raw["target_position_id"])
                if raw.get("target_position_id") is not None
                else None
            ),
            contextual_questions=contextual_questions,
            semantic_concept=(
                str(raw["semantic_concept"])
                if raw.get("semantic_concept")
                else None
            ),
            capture_reference=(
                str(raw["capture_reference"])
                if raw.get("capture_reference")
                else None
            ),
            capture_reference_question=(
                str(raw["capture_reference_question"])
                if raw.get("capture_reference_question")
                else None
            ),
            capture_reference_options={
                str(key): str(value)
                for key, value in raw_capture_options.items()
            },
            flow_trigger_intent=(
                FlowTriggerIntent.model_validate(raw["flow_trigger_intent"])
                if raw.get("flow_trigger_intent") is not None
                else None
            ),
        )

    def drain_segment_proposals(
        self, statement: str, position: OpenPosition
    ) -> list[NodeProposal]:
        """Convert provider-decomposed remainder nodes into queued proposals."""
        drain = getattr(self.client, "drain_segment", None)
        if not callable(drain):
            return []
        proposals: list[NodeProposal] = []
        for index, raw in enumerate(drain()):
            proposals.append(
                self._proposal_from_raw(
                    raw,
                    statement,
                    position,
                    proposal_key=str(raw.get("translation_node_id") or index),
                )
            )
        return proposals

    def activate_segment_node(self, translation_node_id: str) -> None:
        activate = getattr(self.client, "activate_segment_node", None)
        if callable(activate):
            activate(translation_node_id)


class ScriptedModelClient:
    """Deterministic offline client used by fixtures and acceptance tests."""

    def __init__(self, interpretations: list[dict[str, Any]], semantic_results: list[dict[str, Any]] | None = None):
        self.interpretations = list(interpretations)
        self.semantic_results = list(semantic_results or [])
        self.calls: list[dict[str, Any]] = []

    def interpret(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"kind": "interpret", **kwargs})
        if not self.interpretations:
            raise RuntimeError("scripted interpretation fixture exhausted")
        return self.interpretations.pop(0)

    def clarify_semantics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"kind": "semantic", **kwargs})
        return self.semantic_results.pop(0) if self.semantic_results else {"questions": []}
