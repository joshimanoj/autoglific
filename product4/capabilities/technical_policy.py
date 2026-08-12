"""Single source of truth for compiler-required generated behavior."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TECHNICAL_POLICY_VERSION = "product4-technical-policy-1.0"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(ge=1, le=10)
    messages: tuple[str, ...]
    invalid_message: str


class TechnicalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    retry: RetryPolicy
    no_response_timeout_seconds: int = Field(gt=0)
    retry_exhausted_reason: str
    no_response_reason: str
    persistence_failure_reason: str


POLICY = TechnicalPolicy(
    version=TECHNICAL_POLICY_VERSION,
    retry=RetryPolicy(
        max_attempts=3,
        messages=("That response was not valid. Please try again.",),
        invalid_message="Please provide a valid response.",
    ),
    no_response_timeout_seconds=86_400,
    retry_exhausted_reason="Technical policy: valid response attempts exhausted.",
    no_response_reason="Technical policy: no response before timeout.",
    persistence_failure_reason="Technical policy: contact-field persistence failed.",
)


def policy_payload() -> dict[str, Any]:
    return POLICY.model_dump(mode="json")


def policy_hash() -> str:
    canonical = json.dumps(policy_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def policy_reference(rule: str) -> str:
    return f"policy:{TECHNICAL_POLICY_VERSION}:{rule}"
