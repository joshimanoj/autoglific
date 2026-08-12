"""Flow-level trigger keyword contracts shared by authoring and engines."""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TRIGGER_METADATA_KEY = "product4.trigger-keywords"
MAX_TRIGGER_KEYWORDS = 20
_KEYWORD_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class TriggerMetadataValidationStage(str, Enum):
    """Boundary-specific checks for preserved flow trigger metadata."""

    FROZEN_PACKAGE = "frozen_package"
    NORMALIZED_GRAPH = "normalized_graph"


class TriggerKeywordIntent(BaseModel):
    """One exact keyword proposed by the semantic provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1, max_length=200)
    source_excerpt: str = Field(min_length=1, max_length=10_000)


class FlowTriggerIntent(BaseModel):
    """Transient flow-level intent returned with an incremental segment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["none", "explicit", "ambiguous"] = "none"
    keywords: list[TriggerKeywordIntent] = Field(
        default_factory=list, max_length=MAX_TRIGGER_KEYWORDS
    )
    question: str | None = Field(default=None, max_length=1_000)


class TriggerKeywordRecord(BaseModel):
    """Committed keyword plus its approved source kind and exact quote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1, max_length=200)
    source_excerpt: str = Field(min_length=1, max_length=10_000)
    source: Literal["confirmed_prose", "confirmed_user_decision"]
    reference: str = Field(min_length=1, max_length=500)


class FlowTriggerMetadata(BaseModel):
    """Persisted workbench state for flow-level trigger metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    keywords: list[TriggerKeywordRecord] = Field(
        default_factory=list, max_length=MAX_TRIGGER_KEYWORDS
    )


def validate_keyword_value(value: Any) -> str:
    """Validate without normalizing the approved keyword spelling."""

    if not isinstance(value, str):
        raise TypeError("P4_TRIGGER_KEYWORD_NOT_TEXT")
    if not value or not value.strip():
        raise ValueError("P4_TRIGGER_KEYWORD_EMPTY")
    if value != value.strip():
        raise ValueError("P4_TRIGGER_KEYWORD_WHITESPACE")
    if _KEYWORD_CONTROL_RE.search(value):
        raise ValueError("P4_TRIGGER_KEYWORD_CONTROL_CHARACTER")
    return value


def trigger_provenance_reference(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16].upper()
    return f"product4:trigger-keyword:{digest}"


def _provenance_value(item: Any, key: str) -> Any:
    value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
    return getattr(value, "value", value)


def validate_trigger_metadata_payload(
    value: Any,
    provenance: Any,
    *,
    source_hash: str,
    stage: TriggerMetadataValidationStage,
    confirmed_prose: str | None = None,
) -> list[str]:
    """Validate preserved metadata for its current deterministic boundary."""

    if not isinstance(stage, TriggerMetadataValidationStage):
        raise TypeError("P4_TRIGGER_METADATA_VALIDATION_STAGE_INVALID")
    if stage is TriggerMetadataValidationStage.FROZEN_PACKAGE and confirmed_prose is None:
        raise ValueError("P4_TRIGGER_METADATA_CONFIRMED_PROSE_REQUIRED")

    if not isinstance(value, list) or not (1 <= len(value) <= MAX_TRIGGER_KEYWORDS):
        raise ValueError("P4_TRIGGER_METADATA_KEYWORDS_INVALID")
    keywords = [validate_keyword_value(item) for item in value]
    if len(set(keywords)) != len(keywords):
        raise ValueError("P4_TRIGGER_METADATA_DUPLICATE_KEYWORD")
    if not isinstance(provenance, list) or len(provenance) != len(keywords):
        raise ValueError("P4_TRIGGER_METADATA_PROVENANCE_COUNT_INVALID")

    expected_references = {
        trigger_provenance_reference(keyword) for keyword in keywords
    }
    actual_references = {
        str(_provenance_value(item, "reference")) for item in provenance
    }
    if actual_references != expected_references:
        raise ValueError("P4_TRIGGER_METADATA_PROVENANCE_REFERENCE_INVALID")

    for item in provenance:
        source = _provenance_value(item, "source")
        reference = str(_provenance_value(item, "reference"))
        keyword = next(
            keyword
            for keyword in keywords
            if trigger_provenance_reference(keyword) == reference
        )
        quote = _provenance_value(item, "quote")
        item_source_hash = _provenance_value(item, "source_hash")
        if not isinstance(quote, str) or not quote:
            raise ValueError("P4_TRIGGER_METADATA_PROVENANCE_QUOTE_INVALID")
        if keyword not in quote:
            raise ValueError("P4_TRIGGER_METADATA_PROVENANCE_KEYWORD_MISMATCH")
        if source == "confirmed_prose":
            if item_source_hash != source_hash:
                raise ValueError("P4_TRIGGER_METADATA_PROSE_HASH_INVALID")
            if (
                stage is TriggerMetadataValidationStage.FROZEN_PACKAGE
                and quote not in confirmed_prose
            ):
                raise ValueError("P4_TRIGGER_METADATA_PROSE_QUOTE_NOT_GROUNDED")
        elif source == "confirmed_user_decision":
            if item_source_hash is not None:
                raise ValueError("P4_TRIGGER_METADATA_USER_DECISION_HASH_INVALID")
        else:
            raise ValueError("P4_TRIGGER_METADATA_SOURCE_INVALID")
    return keywords
