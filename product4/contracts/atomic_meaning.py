"""Minimal verified-meaning boundary for the atomic prose pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .trigger import FlowTriggerIntent

ATOMIC_MEANING_VERSION = "product4-atomic-meaning-1.0"


class MeaningStepStatus(str, Enum):
    COLLECTING = "collecting"
    VERIFIED = "verified"
    PROJECTED = "projected"


class MeaningPlanStep(BaseModel):
    """One source-grounded behavior ready for deterministic projection."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    ordinal: int = Field(ge=1)
    creation_ordinal: int = Field(default=1, ge=1)
    capability: str = Field(min_length=1, max_length=120)
    branch_path: tuple[str, ...] = ()
    branch_paths: tuple[tuple[str, ...], ...] = ()
    semantic_subject: str = Field(min_length=1, max_length=2_000)
    source_instruction: str = Field(min_length=1, max_length=100_000)
    source_excerpt: str = Field(min_length=1, max_length=100_000)
    supplied_values: dict[str, Any] = Field(default_factory=dict)
    acquisition_sources: dict[str, str] = Field(default_factory=dict)
    trigger_intent: FlowTriggerIntent | None = None
    status: MeaningStepStatus = MeaningStepStatus.COLLECTING

    @model_validator(mode="after")
    def source_and_paths_are_valid(self) -> MeaningPlanStep:
        if self.source_excerpt not in self.source_instruction:
            raise ValueError("P4_ATOMIC_SOURCE_EXCERPT_NOT_CONTIGUOUS")
        paths = self.branch_paths or (tuple(self.branch_path),)
        if tuple(self.branch_path) not in paths:
            raise ValueError("P4_ATOMIC_CANONICAL_BRANCH_PATH_MISSING")
        if len(paths) != len(set(paths)):
            raise ValueError("P4_ATOMIC_BRANCH_PATH_DUPLICATE")
        if any(any(not str(part).strip() for part in path) for path in paths):
            raise ValueError("P4_ATOMIC_BRANCH_PATH_INVALID")
        return self


class MeaningPlan(BaseModel):
    """The compact handoff consumed by the unchanged Authoring pipeline."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[ATOMIC_MEANING_VERSION] = ATOMIC_MEANING_VERSION
    id: str = Field(min_length=1, max_length=120)
    revision: int = Field(default=1, ge=1)
    source_revision: int = Field(default=1, ge=1)
    steps: list[MeaningPlanStep] = Field(default_factory=list)
    trigger_intent: FlowTriggerIntent | None = None
    status: MeaningStepStatus = MeaningStepStatus.COLLECTING

    @model_validator(mode="after")
    def steps_are_ordered_and_unique(self) -> MeaningPlan:
        ids = [step.id for step in self.steps]
        ordinals = [step.ordinal for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("P4_ATOMIC_STEP_ID_DUPLICATE")
        if len(ordinals) != len(set(ordinals)) or ordinals != sorted(ordinals):
            raise ValueError("P4_ATOMIC_STEP_ORDER_INVALID")
        identities = [
            (
                step.capability,
                tuple(step.branch_paths or (tuple(step.branch_path),)),
                step.source_excerpt,
            )
            for step in self.steps
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("P4_ATOMIC_STEP_IDENTITY_DUPLICATE")
        return self


__all__ = ["MeaningPlan", "MeaningPlanStep", "MeaningStepStatus"]
