"""Deterministic boundary from verified atomic meaning to AuthoringService."""

from __future__ import annotations

from typing import Any

from product4.capabilities.forms import validate_complete
from product4.capabilities.registry import REGISTRY
from product4.contracts.atomic_meaning import MeaningPlan, MeaningPlanStep
from product4.contracts.session import AuthoringSession, SessionState
from product4.contracts.trigger import (
    FlowTriggerMetadata,
    TriggerKeywordRecord,
    trigger_provenance_reference,
)

from .interpreter import RegistryInterpreter
from .session import AuthoringService


class AtomicProjectionError(ValueError):
    pass


def _step_paths(step: MeaningPlanStep) -> tuple[tuple[str, ...], ...]:
    return step.branch_paths or (tuple(step.branch_path),)


class _ProjectionClient:
    def __init__(self, step: MeaningPlanStep):
        self.step = step

    def interpret(self, **_: Any) -> dict[str, Any]:
        values = dict(self.step.supplied_values)
        labels = [
            str(item["label"])
            for item in values.get("options", [])
            if isinstance(item, dict) and item.get("label")
        ]
        return {
            "capability": self.step.capability,
            "supplied_values": values,
            "acquisition_sources": {
                field: self.step.acquisition_sources.get(field, "confirmed_prose")
                for field in values
            },
            "acquisition_source_quotes": {
                field: self.step.source_excerpt for field in values
            },
            "contains_additional_actions": False,
            "source_excerpt": self.step.source_excerpt,
            "translation_node_id": self.step.id,
            "position_path": list(self.step.branch_path),
            "choice_labels": labels,
            "semantic_concept": self.step.semantic_subject,
            "routing": {"kind": "current_branch", "scope": "single_branch"},
            "flow_trigger_intent": None,
        }

    def clarify_semantics(self, **_: Any) -> dict[str, Any]:
        return {"questions": []}

    def activate_segment_node(self, _: str) -> None:
        return None


def validate_projection_order(plan: MeaningPlan) -> None:
    steps = sorted(plan.steps, key=lambda item: item.ordinal)
    rank_map: dict[tuple[str, ...], tuple[int, ...]] = {(): ()}
    for parent in steps:
        if parent.capability != "fixed_choice":
            continue
        for parent_path in _step_paths(parent):
            parent_rank = rank_map.get(parent_path)
            if parent_rank is None:
                raise AtomicProjectionError("P4_ATOMIC_BRANCH_PATH_UNREACHABLE")
            for index, option in enumerate(parent.supplied_values.get("options") or []):
                if isinstance(option, dict) and option.get("value"):
                    rank_map[(*parent_path, str(option["value"]))] = (*parent_rank, index)
    previous_rank: tuple[int, ...] | None = None
    for step in steps:
        paths = _step_paths(step)
        grouped = len(paths) > 1
        for path in paths:
            rank = rank_map.get(path)
            if rank is None or any(path[:depth] not in rank_map for depth in range(1, len(path) + 1)):
                raise AtomicProjectionError("P4_ATOMIC_BRANCH_PATH_UNREACHABLE")
            if not grouped and previous_rank is not None and rank < previous_rank:
                raise AtomicProjectionError("P4_ATOMIC_PLAN_ORDER_INVALID")
            if not grouped:
                previous_rank = rank


def validate_meaning_plan(plan: MeaningPlan) -> None:
    if plan.status.value != "verified":
        raise AtomicProjectionError("P4_ATOMIC_PLAN_NOT_VERIFIED")
    validate_projection_order(plan)
    for step in plan.steps:
        if step.capability not in REGISTRY:
            raise AtomicProjectionError(f"P4_ATOMIC_UNSUPPORTED_CAPABILITY:{step.capability}")
        if step.status.value != "verified":
            raise AtomicProjectionError(f"P4_ATOMIC_STEP_NOT_VERIFIED:{step.id}")
        try:
            validate_complete(step.capability, step.supplied_values)
        except (TypeError, ValueError) as exc:
            raise AtomicProjectionError(f"P4_ATOMIC_CONFIGURATION_INVALID:{step.id}:{exc}") from exc


def project_meaning_plan(
    session: AuthoringSession,
    plan: MeaningPlan,
    authoring_service: AuthoringService,
) -> AuthoringSession:
    validate_meaning_plan(plan)
    working = session.model_copy(deep=True)
    trigger = plan.trigger_intent or next(
        (step.trigger_intent for step in plan.steps if step.trigger_intent is not None),
        None,
    )
    if trigger is not None and trigger.status == "explicit" and working.flow_trigger_metadata is None:
        working.flow_trigger_metadata = FlowTriggerMetadata(
            keywords=[
                TriggerKeywordRecord(
                    value=item.value,
                    source_excerpt=item.source_excerpt,
                    source="confirmed_prose",
                    reference=trigger_provenance_reference(item.value),
                )
                for item in trigger.keywords
            ]
        )
    for step in sorted(plan.steps, key=lambda item: item.ordinal):
        for path_index, path in enumerate(_step_paths(step)):
            projected_step = step.model_copy(update={"branch_path": path, "branch_paths": (path,)})
            position = next(
                (item for item in working.open_positions if tuple(item.branch_path) == tuple(path)),
                None,
            )
            if position is None:
                if not working.nodes and not path and len(working.open_positions) == 1:
                    position = working.open_positions[0]
                else:
                    raise AtomicProjectionError("P4_ATOMIC_BRANCH_PATH_UNREACHABLE")
            working.active_position_id = position.id
            projection_service = AuthoringService(
                RegistryInterpreter(_ProjectionClient(projected_step)),
                workbench_mode=authoring_service.workbench_mode,
            )
            translation_id = step.id if len(_step_paths(step)) == 1 else f"{step.id}:branch:{path_index}"
            working = projection_service.propose(
                working,
                projected_step.semantic_subject,
                translation_node_id=translation_id,
                position_path=path,
                node_statement=projected_step.semantic_subject,
                source_excerpt=projected_step.source_excerpt,
            )
            if working.state is SessionState.WAITING_FOR_ANSWER:
                raise AtomicProjectionError("P4_ATOMIC_PROJECTION_INCOMPLETE")
            if not working.nodes or working.nodes[-1].capability != step.capability:
                raise AtomicProjectionError("P4_ATOMIC_PROJECTION_MISMATCH")
    if working.state is not SessionState.READY_FOR_REVIEW or working.open_positions:
        raise AtomicProjectionError("P4_ATOMIC_PROJECTION_OPEN_PATH")
    return working


__all__ = [
    "AtomicProjectionError",
    "project_meaning_plan",
    "validate_meaning_plan",
    "validate_projection_order",
]
