"""Whole-brief semantic translation through a strict production boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import urllib.request
import uuid
from collections.abc import Mapping
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from product4.capabilities.registry import (
    REGISTRY,
    AcquisitionPolicy,
    field_value_json_schema,
    registry_payload,
    require_capability,
    validate_registry_field_value,
)
from product4.contracts.questions import QuestionAnswer, QuestionClass
from product4.contracts.session import (
    AuthoringSession,
    NodeProposal,
    OpenPosition,
    SegmentRouting,
    SessionState,
)
from product4.contracts.trigger import FlowTriggerIntent

from .interpreter import RegistryInterpreter
from .session import AuthoringService
from .trigger_metadata import validate_provider_trigger_intent


class SemanticTranslationError(ValueError):
    """Safe semantic failure carrying only stable diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
        validation_fingerprint: str | None = None,
        network_subtype: str | None = None,
    ) -> None:
        self.code = code or _leading_error_code(message)
        self.request_id = request_id if _safe_request_id(request_id) else None
        self.validation_fingerprint = safe_validation_fingerprint(validation_fingerprint)
        self.network_subtype = safe_network_subtype(network_subtype)
        super().__init__(message)


_REQUEST_ID_RE = re.compile(r"^REQ-[A-F0-9]{12}$")
_VALIDATION_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_VALIDATION_FINGERPRINT_RE = re.compile(
    r"^phase=[a-z0-9_.-]{1,64};issues="
    r"(?:[A-Za-z0-9_.<>-]{1,96}:[a-z0-9_.<>-]{1,64})"
    r"(?:\|[A-Za-z0-9_.<>-]{1,96}:[a-z0-9_.<>-]{1,64}){0,7}$"
)
_SAFE_VALIDATION_FIELDS = frozenset({
    "schema_version", "outcome", "nodes", "id", "position_path", "capability",
    "node_statement", "source_excerpt", "supplied_values", "semantic_questions",
    "capture_reference", "capture_reference_question", "issue_code", "issue_message",
    "routing", "kind", "scope", "choice_group_id", "option_id", "question", "options",
    "label", "value", "target", "prompt", "answer_type", "status", "keywords",
    "trigger_intent",
})


def _new_request_id() -> str:
    return f"REQ-{uuid.uuid4().hex[:12].upper()}"


def _safe_request_id(value: Any) -> bool:
    return isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None


def safe_validation_fingerprint(value: Any) -> str | None:
    """Accept only the bounded phase/location/type diagnostic format."""

    if not isinstance(value, str) or _VALIDATION_FINGERPRINT_RE.fullmatch(value) is None:
        return None
    _, issues_text = value.split(";issues=", 1)
    for entry in issues_text.split("|"):
        location, issue_type = entry.rsplit(":", 1)
        for token in location.split("."):
            if token == "<root>" or token == "<redacted>" or token.isdigit():
                continue
            if token not in _SAFE_VALIDATION_FIELDS:
                return None
        if issue_type != "<redacted>" and not _VALIDATION_TYPE_RE.fullmatch(issue_type):
            return None
    return value


def _safe_validation_location(location: Any) -> str:
    if not isinstance(location, (list, tuple)):
        return "<root>"
    parts: list[str] = []
    for item in location:
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 999:
            parts.append(str(item))
        elif isinstance(item, str) and item in _SAFE_VALIDATION_FIELDS:
            parts.append(item)
        else:
            parts.append("<redacted>")
    return ".".join(parts) or "<root>"


def validation_fingerprint(error: ValidationError, *, phase: str) -> str:
    """Summarize Pydantic errors without retaining messages, inputs, or context."""

    try:
        issues = error.errors(
            include_context=False,
            include_input=False,
            include_url=False,
        )
    except TypeError:
        issues = error.errors()
    entries: list[str] = []
    for issue in issues[:8]:
        raw_type = issue.get("type")
        issue_type = raw_type if isinstance(raw_type, str) and _VALIDATION_TYPE_RE.fullmatch(raw_type) else "<redacted>"
        entries.append(f"{_safe_validation_location(issue.get('loc'))}:{issue_type}")
    if not entries:
        entries.append("<root>:<redacted>")
    fingerprint = f"phase={phase};issues={'|'.join(entries)}"
    return safe_validation_fingerprint(fingerprint) or "phase=unknown;issues=<root>:<redacted>"


def simple_validation_fingerprint(*, phase: str, issue_type: str) -> str:
    safe_type = issue_type if _VALIDATION_TYPE_RE.fullmatch(issue_type) else "<redacted>"
    fingerprint = f"phase={phase};issues=<root>:{safe_type}"
    return safe_validation_fingerprint(fingerprint) or "phase=unknown;issues=<root>:<redacted>"


_SAFE_NETWORK_SUBTYPES = frozenset({"timeout", "dns", "connect", "network"})


def safe_network_subtype(value: Any) -> str | None:
    """Accept only the bounded network diagnostic vocabulary."""

    return value if isinstance(value, str) and value in _SAFE_NETWORK_SUBTYPES else None


def classify_semantic_network_failure(exc: BaseException) -> str:
    """Classify network exceptions without inspecting endpoint or exception text."""

    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        if isinstance(reason, socket.gaierror):
            return "dns"
        if isinstance(reason, (ConnectionError, OSError)):
            return "connect"
        return "network"
    if isinstance(exc, (ConnectionError, OSError)):
        return "connect"
    return "network"


def _leading_error_code(message: Any) -> str:
    match = re.match(r"(P4_[A-Z0-9_]+)", str(message))
    return match.group(1) if match else "P4_SEMANTIC_PROVIDER_FAILURE"


def classify_semantic_http_failure(
    status: int,
    provider_signals: str = "",
    *,
    project_configured: bool = False,
) -> str:
    """Classify HTTP failures without exposing provider response text."""

    signals = str(provider_signals).casefold()
    if status == 401 or any(token in signals for token in (
        "invalid_api_key", "incorrect_api_key", "authentication", "unauthorized",
    )):
        return "P4_SEMANTIC_AUTHENTICATION_FAILED"
    if status == 403:
        if project_configured or any(token in signals for token in (
            "project", "permission", "access_denied", "organization",
        )):
            return "P4_SEMANTIC_PROJECT_ACCESS_FAILED"
        return "P4_SEMANTIC_AUTHENTICATION_FAILED"
    if any(token in signals for token in (
        "insufficient_quota", "quota", "billing", "hard_limit",
    )):
        return "P4_SEMANTIC_QUOTA_EXCEEDED"
    if status == 429 or any(token in signals for token in (
        "rate_limit", "too_many_requests", "throttl",
    )):
        return "P4_SEMANTIC_RATE_LIMITED"
    if any(token in signals for token in (
        "model_not_found", "model_not_available", "invalid_model",
    )) or (status == 404 and "model" in signals):
        return "P4_SEMANTIC_MODEL_UNAVAILABLE"
    if status in {408, 504}:
        return "P4_SEMANTIC_NETWORK_FAILURE"
    if status >= 500:
        return "P4_SEMANTIC_PROVIDER_UNAVAILABLE"
    return "P4_SEMANTIC_PROVIDER_FAILURE"


TRANSLATION_CONTRACT_VERSION = "product4-brief-translation-1.0"
PROVIDER_RESULT_VERSION = "product4-brief-translation-result-3.0"
PROVIDER_SCHEMA_VERSION = "product4-brief-translation-provider-schema-4.0"
PROVIDER_SCHEMA_NAME = "product4_brief_translation_result_v4"
INCREMENTAL_PROVIDER_SCHEMA_NAME = "product4_workbench_incremental_segment_plan_v1"
EXACT_STRING_CONFIGURATION_FIELDS = frozenset({
    "copy", "prompt", "save_as", "title", "reason", "source_variable", "field_name", "locale",
})
INITIAL_QUOTED_EXACT_FIELDS = frozenset({"copy", "prompt", "title"})
_QUOTED_SPAN_PATTERNS = (
    re.compile(r'"([^"\r\n]*)"'),
    re.compile(r"(?<!\w)'([^'\r\n]*)'(?!\w)"),
    re.compile(r"“([^”\r\n]*)”"),
    re.compile(r"‘([^’\r\n]*)’"),
)
_EXPLICIT_INTEGER_RANGE_RE = re.compile(
    r"(?<!\d)(?P<start>\d{1,3})\s*(?:to|through|[-–—])\s*"
    r"(?P<end>\d{1,3})(?!\d)",
    re.IGNORECASE,
)
_INTEGER_TOKEN_RE = re.compile(r"(?<!\d)(\d{1,3})(?!\d)")
_INTEGER_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


class ProviderTranslationNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^T[0-9]{3,}$")
    position_path: list[str] = Field(
        description=(
            "Exact semantic branch_path. Trunk/root nodes use []; every node inside a "
            "fixed-choice branch appends that branch's stable option value; nested "
            "choices append values. Numeric traversal or index segments are forbidden."
        )
    )
    capability: str
    node_statement: str = Field(min_length=1)
    source_excerpt: str = Field(min_length=1)
    supplied_values: dict[str, Any]
    semantic_questions: list[TranslationSemanticQuestion]


class IncrementalProviderTranslationNode(ProviderTranslationNode):
    """Workbench-only node metadata for semantic capture-to-persist linking."""

    capture_reference: str | None = None
    capture_reference_question: str | None = None


class ProviderTranslationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["product4-brief-translation-result-3.0"] = PROVIDER_RESULT_VERSION
    outcome: Literal["translated", "unsupported", "ambiguous", "no_intent"]
    nodes: list[ProviderTranslationNode]
    issue_code: str | None
    issue_message: str | None

    @model_validator(mode="after")
    def outcome_matches_nodes(self) -> ProviderTranslationResult:
        if self.outcome == "translated" and not self.nodes:
            raise ValueError("P4_TRANSLATION_RESULT_EMPTY")
        if self.outcome != "translated" and self.nodes:
            raise ValueError("P4_TRANSLATION_RESULT_STATE_HAS_NODES")
        if self.outcome != "translated" and not (self.issue_code and self.issue_message):
            raise ValueError("P4_TRANSLATION_RESULT_ISSUE_REQUIRED")
        return self


class IncrementalProviderTranslationResult(ProviderTranslationResult):
    """Workbench-only extension; the whole-brief result contract is unchanged."""

    nodes: list[IncrementalProviderTranslationNode]
    routing: SegmentRouting | None = None
    # Optional in the Python parser for old recorded fixtures; required in the
    # current incremental provider JSON schema below.
    trigger_intent: FlowTriggerIntent | None = None


def _strict_schema(value: Any) -> None:
    if isinstance(value, dict):
        value.pop("default", None)
        if value.get("type") == "object" or "properties" in value:
            value["additionalProperties"] = False
            value["required"] = list(value.get("properties", {}))
        for item in value.values():
            _strict_schema(item)
    elif isinstance(value, list):
        for item in value:
            _strict_schema(item)


def translation_provider_json_schema() -> dict[str, Any]:
    """Generate capability-tagged node schemas from the registry's typed fields."""
    question_schema = TranslationSemanticQuestion.model_json_schema()
    _strict_schema(question_schema)
    variants: list[dict[str, Any]] = []
    for capability in REGISTRY.values():
        supplied_properties: dict[str, Any] = {}
        for field in capability.fields:
            typed_value_schema = field_value_json_schema(field)
            _strict_schema(typed_value_schema)
            supplied_properties[field.path] = {
                "type": "object",
                "properties": {
                    "value": {
                        "anyOf": [typed_value_schema, {"type": "null"}],
                    },
                    "source_excerpt": {
                        "anyOf": [
                            {"type": "string", "minLength": 1},
                            {"type": "null"},
                        ],
                    },
                },
                "required": ["value", "source_excerpt"],
                "additionalProperties": False,
            }
        variants.append({
            "type": "object",
            "properties": {
                "id": {"type": "string", "pattern": r"^T[0-9]{3,}$"},
                "position_path": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exact semantic branch_path. Trunk/root nodes use []; every node "
                        "inside a fixed-choice branch appends its stable option value; nested "
                        "choices append values. Numeric traversal/index segments are forbidden."
                    ),
                },
                "capability": {"type": "string", "const": capability.id},
                "node_statement": {"type": "string", "minLength": 1},
                "source_excerpt": {"type": "string", "minLength": 1},
                "supplied_values": {
                    "type": "object",
                    "properties": supplied_properties,
                    "required": list(supplied_properties),
                    "additionalProperties": False,
                },
                "semantic_questions": {"type": "array", "items": question_schema},
            },
            "required": [
                "id", "position_path", "capability", "node_statement",
                "source_excerpt", "supplied_values", "semantic_questions",
            ],
            "additionalProperties": False,
        })
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "const": PROVIDER_RESULT_VERSION},
            "outcome": {
                "type": "string",
                "enum": ["translated", "unsupported", "ambiguous", "no_intent"],
            },
            "nodes": {"type": "array", "items": {"anyOf": variants}},
            "issue_code": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "issue_message": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["schema_version", "outcome", "nodes", "issue_code", "issue_message"],
        "additionalProperties": False,
    }


def incremental_translation_provider_json_schema() -> dict[str, Any]:
    """Add routing and flow-level trigger intent to the workbench schema."""
    schema = translation_provider_json_schema()
    for variant in schema["properties"]["nodes"]["items"]["anyOf"]:
        variant["properties"]["capture_reference"] = {
            "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
        }
        variant["properties"]["capture_reference_question"] = {
            "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
        }
        variant["required"].extend(["capture_reference", "capture_reference_question"])
    schema["properties"]["routing"] = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["current_branch", "existing_branch", "choice_group", "clarification"],
            },
            "scope": {
                "type": "string",
                "enum": ["single_branch", "descendant_leaves"],
            },
            "choice_group_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "option_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                    },
                    "required": ["id", "label"],
                    "additionalProperties": False,
                },
            },
            "source_excerpt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": [
            "kind", "scope", "choice_group_id", "option_id", "question",
            "options", "source_excerpt",
        ],
        "additionalProperties": False,
    }
    schema["properties"]["trigger_intent"] = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["none", "explicit", "ambiguous"],
            },
            "keywords": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "minLength": 1, "maxLength": 200},
                        "source_excerpt": {"type": "string", "minLength": 1, "maxLength": 10_000},
                    },
                    "required": ["value", "source_excerpt"],
                    "additionalProperties": False,
                },
            },
            "question": {
                "anyOf": [{"type": "string", "maxLength": 1_000}, {"type": "null"}],
            },
        },
        "required": ["status", "keywords", "question"],
        "additionalProperties": False,
    }
    schema["required"] = [*schema["required"], "routing", "trigger_intent"]
    return schema


def translation_prompt_examples() -> dict[str, Any]:
    """Neutral examples maintained beside the generic provider contract."""
    return {
        "positive": [
            {
                "case": "linear_trunk",
                "brief": "Use a first node, then a second node, then finish.",
                "invariant": "Every sequential trunk node has position_path [].",
                "position_paths": [[], [], []],
            },
            {
                "case": "two_way_choice",
                "brief": "Choose one of two options, then perform actions in the selected route.",
                "invariant": (
                    "The choice uses []; every first-route node uses [branch_a], and every "
                    "second-route node uses [branch_b]."
                ),
                "position_paths": [[], ["branch_a"], ["branch_a"], ["branch_b"], ["branch_b"]],
            },
            {
                "case": "nested_choice",
                "brief": "Choose one option; within its route choose one nested option.",
                "invariant": "Nested stable values append to the parent branch path.",
                "position_paths": [
                    [], ["branch_a"], ["branch_a", "nested_a"],
                    ["branch_a", "nested_b"], ["branch_b"],
                ],
            },
            {
                "case": "source_values_and_unresolved_configuration",
                "brief": "Collect a stated value using the prompt Enter value.",
                "invariant": (
                    "Supply a source-explicit input_type=text with its exact source excerpt. "
                    "Represent unstated validation with its required value=null and "
                    "source_excerpt=null wrapper; the adapter removes unresolved values "
                    "so the registry asks."
                ),
                "supplied_values": {
                    "prompt": {
                        "value": "Enter value",
                        "source_excerpt": "stated prompt Enter value",
                    },
                    "input_type": {
                        "value": "text",
                        "source_excerpt": "stated value",
                    },
                    "validation": {"value": None, "source_excerpt": None},
                },
                "semantic_questions": [],
                "registry_action": "Ask configuration questions for unresolved fields.",
            },
            {
                "case": "genuinely_ambiguous_configuration",
                "brief": "Show one of two stated messages; ask which exact copy is intended.",
                "supplied_values": {
                    "copy": {"value": "Message one", "source_excerpt": "Message one"},
                    "locale": {"value": None, "source_excerpt": None},
                },
                "semantic_questions": [{
                    "target": "config.copy",
                    "prompt": "Which stated copy should be used?",
                    "answer_type": "options",
                    "options": ["Message one", "Message two"],
                }],
            },
            {
                "case": "typed_scalar",
                "capability": "send_text_message",
                "supplied_values": {
                    "copy": {"value": "Stated message", "source_excerpt": "Stated message"},
                    "locale": {"value": None, "source_excerpt": None},
                },
            },
            {
                "case": "typed_option_array",
                "capability": "fixed_choice",
                "supplied_values": {
                    "title": {"value": "Choose option", "source_excerpt": "Choose option"},
                    "options": {
                        "value": [
                            {"label": "First option", "value": "first_option"},
                            {"label": "Second option", "value": "second_option"},
                        ],
                        "source_excerpt": (
                            "First option=first_option or Second option=second_option"
                        ),
                    },
                },
            },
            {
                "case": "typed_validation_object",
                "capability": "capture_user_input",
                "supplied_values": {
                    "validation": {
                        "value": {
                            "minimum": 2,
                            "maximum": None,
                            "min_length": None,
                            "max_length": None,
                            "pattern": None,
                            "allowed_values": None,
                        },
                        "source_excerpt": "at least 2",
                    },
                },
            },
            {
                "case": "explicit_provenance_only",
                "brief": (
                    "A node explicitly names a value, a storage variable, and a destination field."
                ),
                "invariant": (
                    "The source fixes only the exact values it names. It does not fix any "
                    "other field or derived relationship."
                ),
            },
            {
                "case": "semantic_outcome_without_exact_copy",
                "brief": "Complete the requested operation, then finish.",
                "invariant": (
                    "The intent to complete is not an exact message. Leave copy unresolved "
                    "for configuration; do not invent wording."
                ),
            },
            {
                "case": "empty_validation_is_allowed",
                "brief": "Collect a note as text.",
                "invariant": (
                    "When no validation is stated, leave validation unresolved so the user "
                    "may answer with an empty object."
                ),
            },
            {
                "case": "cross_node_derived_value",
                "brief": "One node names a stored value and another node has a related field.",
                "invariant": (
                    "Keep the cross-node or derived field null unless its own node excerpt "
                    "explicitly contains the evidence; deterministic same-branch derivation "
                    "or a registry question handles the null field."
                ),
            },
        ],
        "negative": [
            "Numeric traversal paths such as [1, 0, 2] are invalid.",
            "Sequential nodes inside one branch must not change position_path.",
            "Unsupported intent must use outcome=unsupported, never a nearby supported capability.",
            "Never invent validation or other configuration absent from source prose.",
            "Never place fields belonging to another capability in supplied_values.",
            {
                "invalid": "options_string",
                "value": "First option=first_option, Second option=second_option",
                "reason": "fixed_choice.options must be an array of option objects",
            },
            {
                "invalid": "malformed_option",
                "value": [{"label": "First option"}],
                "reason": "every option requires label and stable value",
            },
            {
                "invalid": "irrelevant_field",
                "capability": "end",
                "supplied_values": {"options": None},
                "reason": "field is not owned by the capability",
            },
            {
                "invalid": "null_config_with_semantic_question",
                "supplied_values": {"copy": {"value": None, "source_excerpt": None}},
                "semantic_questions": [{"target": "config.copy"}],
                "reason": "config semantic questions require a non-null provisional value",
            },
            "A completion outcome does not source exact copy.",
            "A field on one node does not source a cross-node variable.",
            "Naming a destination field does not source a capture variable.",
            "A semantic outcome does not authorize invented message wording.",
        ],
    }


class SemanticTransport(Protocol):
    def complete(self, request: dict[str, Any]) -> str: ...


class JsonHttpClient(Protocol):
    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class UrlLibJsonHttpClient:
    """Small production HTTP implementation; never used by offline tests."""

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))


class ProductionSemanticTransport:
    """OpenAI-compatible semantic transport with an injectable HTTP boundary."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        project_id: str | None = None,
        http_client: JsonHttpClient | None = None,
        timeout_seconds: float = 60.0,
    ):
        if not endpoint.startswith(("https://", "http://")):
            raise SemanticTranslationError("P4_SEMANTIC_ENDPOINT_INVALID")
        if not model.strip():
            raise SemanticTranslationError("P4_SEMANTIC_MODEL_MISSING")
        if not api_key.strip():
            raise SemanticTranslationError("P4_SEMANTIC_CREDENTIAL_MISSING")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.project_id = project_id.strip() if project_id else None
        self.http_client = http_client or UrlLibJsonHttpClient()
        self.timeout_seconds = timeout_seconds
        self.last_request_id: str | None = None

    @staticmethod
    def _provider_signals(exc: HTTPError) -> str:
        """Read bounded fields only for classification; never retain the body."""

        try:
            raw = exc.read(4096)
        except (AttributeError, OSError):
            return ""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return ""
        return " ".join(
            str(error.get(key) or "")[:120]
            for key in ("type", "code", "param", "message")
        )

    @classmethod
    def from_environment(
        cls,
        *,
        http_client: JsonHttpClient | None = None,
    ) -> ProductionSemanticTransport:
        return cls(
            endpoint=os.environ.get("PRODUCT4_SEMANTIC_ENDPOINT", ""),
            model=os.environ.get("PRODUCT4_SEMANTIC_MODEL", ""),
            api_key=os.environ.get("PRODUCT4_SEMANTIC_API_KEY", ""),
            project_id=os.environ.get("PRODUCT4_SEMANTIC_PROJECT_ID") or os.environ.get("OPENAI_PROJECT_ID"),
            http_client=http_client,
        )

    def complete(self, request: dict[str, Any]) -> str:
        workbench_mode = request.get("workbench_mode") == "incremental_segment_planning"
        request_id = _new_request_id()
        self.last_request_id = request_id
        payload = {
            "model": self.model,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": (
                        INCREMENTAL_PROVIDER_SCHEMA_NAME
                        if workbench_mode
                        else PROVIDER_SCHEMA_NAME
                    ),
                    "strict": True,
                    "schema": (
                        incremental_translation_provider_json_schema()
                        if workbench_mode
                        else translation_provider_json_schema()
                    ),
                },
            },
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the Product 4 semantic translator. Return exactly the "
                        f"requested {PROVIDER_RESULT_VERSION} JSON contract."
                    ),
                },
                {"role": "user", "content": json.dumps(request, ensure_ascii=False, sort_keys=True)},
            ],
        }
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            if self.project_id:
                headers["OpenAI-Project"] = self.project_id
            response = self.http_client.post_json(
                url=self.endpoint,
                headers=headers,
                payload=payload,
                timeout_seconds=self.timeout_seconds,
            )
        except HTTPError as exc:
            code = classify_semantic_http_failure(
                int(exc.code),
                self._provider_signals(exc),
                project_configured=bool(self.project_id),
            )
            raise SemanticTranslationError(
                f"P4_SEMANTIC_PROVIDER_FAILURE:HTTPError:status={exc.code}",
                code=code,
                request_id=request_id,
            ) from exc
        except (TimeoutError, URLError, ConnectionError, OSError) as exc:
            raise SemanticTranslationError(
                f"P4_SEMANTIC_PROVIDER_FAILURE:{type(exc).__name__}",
                code="P4_SEMANTIC_NETWORK_FAILURE",
                request_id=request_id,
                network_subtype=classify_semantic_network_failure(exc),
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SemanticTranslationError(
                "P4_SEMANTIC_PROVIDER_RESPONSE_INVALID",
                code="P4_SEMANTIC_PROVIDER_RESPONSE_INVALID",
                request_id=request_id,
            ) from exc
        except Exception as exc:
            raise SemanticTranslationError(
                f"P4_SEMANTIC_PROVIDER_FAILURE:{type(exc).__name__}",
                code="P4_SEMANTIC_PROVIDER_FAILURE",
                request_id=request_id,
            ) from exc
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SemanticTranslationError(
                "P4_SEMANTIC_PROVIDER_RESPONSE_INVALID",
                code="P4_SEMANTIC_PROVIDER_RESPONSE_INVALID",
                request_id=request_id,
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise SemanticTranslationError(
                "P4_SEMANTIC_PROVIDER_RESPONSE_EMPTY",
                code="P4_SEMANTIC_PROVIDER_RESPONSE_EMPTY",
                request_id=request_id,
            )
        return content


class TranslationSemanticQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(pattern=r"^(?:capability|statement|config\.[a-z][a-z0-9_]*)$")
    prompt: str = Field(min_length=1)
    answer_type: Literal["text", "boolean", "options", "json"] = "text"
    options: list[str] = Field(default_factory=list)


class TranslationNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^T[0-9]{3,}$")
    position_path: tuple[str, ...] = ()
    capability: str
    node_statement: str = Field(min_length=1)
    source_excerpt: str = Field(min_length=1)
    supplied_values: dict[str, Any] = Field(default_factory=dict)
    supplied_value_sources: dict[str, str] = Field(default_factory=dict, exclude=True)
    semantic_questions: tuple[TranslationSemanticQuestion, ...] = ()
    capture_reference: str | None = Field(default=None, exclude=True)
    capture_reference_question: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def remove_strict_schema_null_placeholders(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        supplied = normalized.get("supplied_values")
        if isinstance(supplied, Mapping):
            cleaned = {key: item for key, item in supplied.items() if item is not None}
            validation = cleaned.get("validation")
            if isinstance(validation, Mapping):
                cleaned["validation"] = {
                    key: item for key, item in validation.items() if item is not None
                }
            normalized["supplied_values"] = cleaned
            if not normalized.get("supplied_value_sources") and normalized.get("source_excerpt"):
                normalized["supplied_value_sources"] = {
                    key: normalized["source_excerpt"] for key in cleaned
                }
        return normalized


class BriefSemanticTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["product4-brief-translation-1.0"] = TRANSLATION_CONTRACT_VERSION
    nodes: tuple[TranslationNode, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> BriefSemanticTranslation:
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("P4_TRANSLATION_DUPLICATE_NODE_ID")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def canonical_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def _repair_json_envelope(raw: str) -> str:
    value = raw.strip()
    if value.startswith("```json") and value.endswith("```"):
        return value[7:-3].strip()
    if value.startswith("```") and value.endswith("```"):
        return value[3:-3].strip()
    return value


def _is_inside_explicit_quote(value: Any, brief: str) -> bool:
    if not isinstance(value, str):
        return False
    return any(
        value == match.group(1)
        for pattern in _QUOTED_SPAN_PATTERNS
        for match in pattern.finditer(brief)
    )


def _normalize_incremental_unresolved_wrappers(
    envelope: Any,
    brief: str,
) -> tuple[Any, list[str]]:
    """Discard only stray quotes attached to unresolved incremental fields.

    The provider contract requires unresolved wrappers to use two nulls. The
    workbench accepts a provider's equivalent ``value=null`` shape by removing
    only its unusable source quote. It also removes unbacked registry defaults
    and unsupported initial exact copy/prompt/title values. All other non-null
    values still go through strict provenance validation.
    """
    if not isinstance(envelope, dict):
        return envelope, []
    normalized = json.loads(json.dumps(envelope, ensure_ascii=False))
    discarded: list[str] = []
    for index, node in enumerate(normalized.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        try:
            definition = require_capability(str(node.get("capability") or ""))
        except ValueError:
            definition = None
        fields = {field.path: field for field in definition.fields} if definition else {}
        supplied_values = node.get("supplied_values")
        if not isinstance(supplied_values, dict):
            continue
        for field_path, wrapper in supplied_values.items():
            if (
                isinstance(wrapper, dict)
                and set(wrapper) == {"value", "source_excerpt"}
                and wrapper.get("value") is None
                and wrapper.get("source_excerpt") is not None
            ):
                wrapper["source_excerpt"] = None
                node_id = node.get("id") or f"node-{index}"
                discarded.append(f"unresolved:{node_id}:{field_path}")
                continue
            if not isinstance(wrapper, dict):
                continue
            field = fields.get(field_path)
            if field is None:
                continue
            source_excerpt = wrapper.get("source_excerpt")
            value = wrapper.get("value")
            node_id = node.get("id") or f"node-{index}"
            if (
                field_path in INITIAL_QUOTED_EXACT_FIELDS
                and value is not None
                and not _is_inside_explicit_quote(value, brief)
            ):
                wrapper["value"] = None
                wrapper["source_excerpt"] = None
                discarded.append(f"quote_gated:{node_id}:{field_path}")
                continue
            if (
                field.policy is AcquisitionPolicy.DEFAULTED
                and value == field.default
                and not _has_valid_explicit_field_provenance(
                    brief=brief,
                    node_id=str(node_id),
                    field_path=field_path,
                    value=value,
                    field_source_excerpt=source_excerpt,
                    node_source_excerpt=str(node.get("source_excerpt") or ""),
                )
            ):
                wrapper["value"] = None
                wrapper["source_excerpt"] = None
                discarded.append(f"defaulted:{node_id}:{field_path}")
                continue
            if (
                field_path in EXACT_STRING_CONFIGURATION_FIELDS
                and value is not None
                and not _has_valid_explicit_field_provenance(
                    brief=brief,
                    node_id=str(node_id),
                    field_path=field_path,
                    value=value,
                    field_source_excerpt=source_excerpt,
                    node_source_excerpt=str(node.get("source_excerpt") or ""),
                )
            ):
                wrapper["value"] = None
                wrapper["source_excerpt"] = None
                discarded.append(f"exact_string:{node_id}:{field_path}")
                continue
    return normalized, discarded


def _validate_explicit_field_provenance(
    *,
    node_id: str,
    field_path: str,
    value: Any,
    field_source_excerpt: str,
    node_source_excerpt: str,
) -> None:
    """Reject inferred exact configuration while retaining typed validation."""
    if field_source_excerpt not in node_source_excerpt:
        raise SemanticTranslationError(
            f"P4_TRANSLATION_FIELD_SOURCE_CONTEXT_MISMATCH:{node_id}:{field_path}"
        )
    if field_path in EXACT_STRING_CONFIGURATION_FIELDS:
        if not isinstance(value, str) or value not in field_source_excerpt:
            raise SemanticTranslationError(
                f"P4_TRANSLATION_EXACT_STRING_SOURCE_MISMATCH:{node_id}:{field_path}"
            )
    elif field_path == "options" and (
        not isinstance(value, list)
        or any(
            not _choice_option_has_explicit_provenance(item, field_source_excerpt)
            for item in value
        )
    ):
        raise SemanticTranslationError(
            f"P4_TRANSLATION_CHOICE_SOURCE_MISMATCH:{node_id}:{field_path}"
        )


def _explicit_integer_range_number(label: str, source_excerpt: str) -> int | None:
    """Return a numeric label's value when an explicit source range supports it."""

    tokens = _INTEGER_TOKEN_RE.findall(label)
    if len(tokens) != 1:
        return None
    number = int(tokens[0])
    if label.strip() != str(number):
        return None
    for match in _EXPLICIT_INTEGER_RANGE_RE.finditer(source_excerpt):
        start = int(match.group("start"))
        end = int(match.group("end"))
        if start <= end and end - start <= 9 and start <= number <= end:
            return number
    return None


def _range_derived_stable_value(value: str, number: int) -> bool:
    """Accept only a stable identifier that preserves the explicit option number."""

    numeric_tokens = [int(token) for token in _INTEGER_TOKEN_RE.findall(value)]
    if numeric_tokens:
        return len(numeric_tokens) == 1 and numeric_tokens[0] == number
    word = _INTEGER_WORDS.get(number)
    return bool(
        word
        and re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", value.casefold())
    )


def _choice_option_has_explicit_provenance(item: Any, source_excerpt: str) -> bool:
    if not isinstance(item, Mapping):
        return False
    label = str(item.get("label") or "")
    stable_value = str(item.get("value") or "")
    range_number = _explicit_integer_range_number(label, source_excerpt)
    label_is_sourced = label in source_excerpt or range_number is not None
    if not label_is_sourced:
        return False
    if stable_value in source_excerpt:
        return True
    slug = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")
    if stable_value == slug:
        return True
    return range_number is not None and _range_derived_stable_value(stable_value, range_number)


def _has_valid_explicit_field_provenance(
    *,
    brief: str,
    node_id: str,
    field_path: str,
    value: Any,
    field_source_excerpt: Any,
    node_source_excerpt: str,
) -> bool:
    if not isinstance(field_source_excerpt, str) or field_source_excerpt not in brief:
        return False
    try:
        _validate_explicit_field_provenance(
            node_id=node_id,
            field_path=field_path,
            value=value,
            field_source_excerpt=field_source_excerpt,
            node_source_excerpt=node_source_excerpt,
        )
    except SemanticTranslationError:
        return False
    return True


class BriefSemanticTranslator:
    PROMPT_VERSION = "product4-semantic-translation-prompt-5.3-node-local-provenance-classification"

    def __init__(self, transport: SemanticTransport):
        self.transport = transport

    def translate(self, brief: str) -> BriefSemanticTranslation:
        if not brief.strip():
            raise SemanticTranslationError("P4_TRANSLATION_BRIEF_EMPTY")
        request = {
            "contract": TRANSLATION_CONTRACT_VERSION,
            "provider_schema_version": PROVIDER_SCHEMA_VERSION,
            "brief": brief,
            "registry": registry_payload(),
            "prompt_version": self.PROMPT_VERSION,
            "examples": translation_prompt_examples(),
            "instructions": (
                f"Return one JSON object matching {PROVIDER_RESULT_VERSION}. "
                "Translate the complete brief into ordered, single-capability nodes with "
                "sequential stable ids. position_path is exactly the "
                "semantic branch_path: trunk/root nodes use []; every node within a "
                "fixed_choice branch appends its stable option value; nested choices "
                "append another stable value. Never use numeric traversal/index paths. "
                "Visit fixed_choice options in listed order; "
                "stable option values must be lowercase snake_case. source_excerpt must "
                "be an exact contiguous verbatim substring of the brief. node_statement "
                "is derived meaning and must stay separate from source_excerpt. Include "
                "only configuration explicitly fixed by the brief in the capability-specific "
                "supplied_values object. Emit every field wrapper required by that capability. "
                "Each wrapper contains its registry-typed value and exact source_excerpt. Every "
                "non-null supplied field value must cite evidence contained within that same "
                "node's source_excerpt, and the value must be supported by that node-local "
                "evidence. A cross-node or derived field such as persistence source_variable "
                "must remain null unless its own node excerpt explicitly contains the evidence. "
                "When such a field is null, deterministic same-branch derivation or a registry "
                "question handles it; never infer it in translation. For every missing or "
                "unresolved field, emit value=null and source_excerpt=null; "
                "the adapter removes unresolved values so registry forms ask. Never "
                "put a field from another capability into supplied_values. If the brief "
                "unambiguously identifies a registry-valid input_type, include it; otherwise "
                "emit its required null wrapper. Never invent validation constraints: include validation only when "
                "stated verbatim, otherwise leave it unresolved for reviewed configuration. "
                "A semantic outcome does not fix exact message copy; keep copy unresolved "
                "unless the exact string is explicitly present. Naming an input kind fixes "
                "input_type only; it does not fix save_as unless the source names the variable. "
                "Naming a destination field fixes field_name only; it does not fix "
                "source_variable or save_as unless the source names them. A semantic outcome "
                "never authorizes invented copy or a terminal reason. For exact string fields, "
                "the value must occur "
                "verbatim in that field's source_excerpt; otherwise emit the null wrapper. "
                "Ordinary missing capability configuration uses its null wrapper and no "
                "config semantic question; the registry will ask a configuration question. "
                "If the brief explicitly leaves a capability, meaning, or configuration value "
                "genuinely ambiguous, do not decide silently: use one source-stated candidate "
                "as a non-null provisional value and add semantic_questions targeting "
                "capability, statement, or config.<field>. For an authored operation "
                "outside the registry, return outcome=unsupported with no nodes. Return "
                "outcome=ambiguous with no nodes when safe decomposition itself is ambiguous, "
                "and outcome=no_intent with no nodes when no actionable flow exists. Never "
                "fabricate a registered capability to satisfy the schema."
            ),
        }
        request_id = getattr(self.transport, "last_request_id", None)
        validation_phase = "brief_json"
        try:
            raw = self.transport.complete(request)
            request_id = getattr(self.transport, "last_request_id", None) or request_id
            envelope = json.loads(_repair_json_envelope(raw))
            if not isinstance(envelope, dict):
                raise SemanticTranslationError(
                    "P4_TRANSLATION_OUTPUT_MALFORMED",
                    code="P4_TRANSLATION_OUTPUT_MALFORMED",
                    request_id=request_id,
                    validation_fingerprint=simple_validation_fingerprint(
                        phase="brief_json", issue_type="object_required"
                    ),
                )
            if envelope.get("schema_version") == PROVIDER_RESULT_VERSION:
                validation_phase = "brief_provider_result"
                result = ProviderTranslationResult.model_validate(envelope)
                if result.outcome != "translated":
                    raise SemanticTranslationError(
                        f"P4_TRANSLATION_{result.outcome.upper()}:{result.issue_code}:{result.issue_message}"
                    )
                translation = self._adapt_provider_result(brief, result)
            else:
                # Backward-compatible parser for recorded/offline 1.0 fixtures only.
                validation_phase = "brief_legacy_result"
                translation = BriefSemanticTranslation.model_validate(envelope)
            validation_phase = "brief_semantics"
            self._validate_semantics(brief, translation)
        except SemanticTranslationError as exc:
            request_id = getattr(self.transport, "last_request_id", None) or request_id
            if request_id and not exc.request_id:
                raise SemanticTranslationError(
                    str(exc),
                    code=exc.code,
                    request_id=request_id,
                    validation_fingerprint=exc.validation_fingerprint,
                    network_subtype=exc.network_subtype,
                ) from exc
            raise
        except ValidationError as exc:
            raise SemanticTranslationError(
                "P4_TRANSLATION_OUTPUT_MALFORMED",
                code="P4_TRANSLATION_OUTPUT_MALFORMED",
                request_id=request_id,
                validation_fingerprint=validation_fingerprint(exc, phase=validation_phase),
            ) from exc
        except json.JSONDecodeError as exc:
            raise SemanticTranslationError(
                "P4_TRANSLATION_OUTPUT_MALFORMED",
                code="P4_TRANSLATION_OUTPUT_MALFORMED",
                request_id=request_id,
                validation_fingerprint=simple_validation_fingerprint(
                    phase="brief_json", issue_type="json_invalid"
                ),
            ) from exc
        except ValueError as exc:
            raise SemanticTranslationError(
                "P4_TRANSLATION_OUTPUT_MALFORMED",
                code="P4_TRANSLATION_OUTPUT_MALFORMED",
                request_id=request_id,
                validation_fingerprint=simple_validation_fingerprint(
                    phase=validation_phase, issue_type="value_error"
                ),
            ) from exc
        return translation

    @staticmethod
    def _adapt_provider_result(
        brief: str, result: ProviderTranslationResult
    ) -> BriefSemanticTranslation:
        nodes: list[dict[str, Any]] = []
        for provider_node in result.nodes:
            if any(segment.isdecimal() for segment in provider_node.position_path):
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_NUMERIC_POSITION_PATH:{provider_node.id}:{provider_node.position_path}"
                )
            if provider_node.source_excerpt not in brief:
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_SOURCE_EXCERPT_MISMATCH:{provider_node.id}"
                )
            values: dict[str, Any] = {}
            sources: dict[str, str] = {}
            definition = require_capability(provider_node.capability)
            fields = {field.path: field for field in definition.fields}
            unknown = set(provider_node.supplied_values) - set(fields)
            if unknown:
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_UNKNOWN_FIELDS:{provider_node.id}:{sorted(unknown)}"
                )
            for field_path, entry in provider_node.supplied_values.items():
                if not isinstance(entry, Mapping) or set(entry) != {"value", "source_excerpt"}:
                    raise SemanticTranslationError(
                        f"P4_TRANSLATION_FIELD_WRAPPER_INVALID:{provider_node.id}:{field_path}"
                    )
                value = entry["value"]
                source_excerpt = entry["source_excerpt"]
                if value is None:
                    if source_excerpt is not None:
                        raise SemanticTranslationError(
                            f"P4_TRANSLATION_UNRESOLVED_SOURCE_INVALID:{provider_node.id}:{field_path}"
                        )
                    continue
                if not isinstance(source_excerpt, str) or source_excerpt not in brief:
                    raise SemanticTranslationError(
                        f"P4_TRANSLATION_VALUE_SOURCE_MISMATCH:{provider_node.id}:{field_path}"
                    )
                try:
                    normalized_value = validate_registry_field_value(fields[field_path], value)
                except (ValidationError, ValueError, TypeError) as exc:
                    raise SemanticTranslationError(
                        f"P4_TRANSLATION_FIELD_TYPE_INVALID:{provider_node.id}:{field_path}"
                    ) from exc
                _validate_explicit_field_provenance(
                    node_id=provider_node.id,
                    field_path=field_path,
                    value=normalized_value,
                    field_source_excerpt=source_excerpt,
                    node_source_excerpt=provider_node.source_excerpt,
                )
                values[field_path] = normalized_value
                sources[field_path] = source_excerpt
            adapted_node = {
                "id": provider_node.id,
                "position_path": provider_node.position_path,
                "capability": provider_node.capability,
                "node_statement": provider_node.node_statement,
                "source_excerpt": provider_node.source_excerpt,
                "supplied_values": values,
                "supplied_value_sources": sources,
                "semantic_questions": [
                    question.model_dump(mode="json")
                    for question in provider_node.semantic_questions
                ],
            }
            capture_reference = getattr(provider_node, "capture_reference", None)
            capture_reference_question = getattr(provider_node, "capture_reference_question", None)
            if capture_reference is not None:
                adapted_node["capture_reference"] = capture_reference
            if capture_reference_question is not None:
                adapted_node["capture_reference_question"] = capture_reference_question
            nodes.append(adapted_node)
        return BriefSemanticTranslation(nodes=nodes)

    @staticmethod
    def _validate_semantics(brief: str, translation: BriefSemanticTranslation) -> None:
        open_paths: list[tuple[str, ...]] = [()]
        for node in translation.nodes:
            if any(segment.isdecimal() for segment in node.position_path):
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_NUMERIC_POSITION_PATH:{node.id}:{node.position_path}"
                )
            if not open_paths:
                raise SemanticTranslationError("P4_TRANSLATION_EXTRA_NODE")
            expected_path = open_paths.pop(0)
            if node.position_path != expected_path:
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_POSITION_MISMATCH:{node.id}:{expected_path}:{node.position_path}"
                )
            if node.source_excerpt not in brief:
                raise SemanticTranslationError(f"P4_TRANSLATION_SOURCE_EXCERPT_MISMATCH:{node.id}")
            try:
                definition = require_capability(node.capability)
            except ValueError as exc:
                raise SemanticTranslationError(f"P4_TRANSLATION_UNSUPPORTED:{node.capability}") from exc
            known_fields = {field.path for field in definition.fields}
            if unknown := set(node.supplied_values) - known_fields:
                raise SemanticTranslationError(f"P4_TRANSLATION_UNKNOWN_FIELDS:{node.id}:{sorted(unknown)}")
            if set(node.supplied_value_sources) != set(node.supplied_values):
                raise SemanticTranslationError(f"P4_TRANSLATION_VALUE_PROVENANCE_INCOMPLETE:{node.id}")
            for question in node.semantic_questions:
                if question.target.startswith("config."):
                    target_field = question.target.removeprefix("config.")
                    if target_field not in known_fields:
                        raise SemanticTranslationError(
                            f"P4_TRANSLATION_SEMANTIC_TARGET_INVALID:{node.id}"
                        )
                    if target_field not in node.supplied_values:
                        raise SemanticTranslationError(
                            f"P4_TRANSLATION_SEMANTIC_CONFIG_REQUIRES_PROVISIONAL:{node.id}:{target_field}"
                        )
            if definition.exit.kind == "linear":
                open_paths.insert(0, expected_path)
            elif definition.exit.kind == "dynamic":
                options = node.supplied_values.get(definition.exit.source_field or "options")
                if not isinstance(options, list) or len(options) < 2:
                    raise SemanticTranslationError(f"P4_TRANSLATION_CHOICE_OPTIONS_REQUIRED:{node.id}")
                values = [str(option.get("value") or "") for option in options if isinstance(option, dict)]
                if len(values) != len(options) or any(not value for value in values) or len(set(values)) != len(values):
                    raise SemanticTranslationError(f"P4_TRANSLATION_CHOICE_OPTIONS_INVALID:{node.id}")
                open_paths = [*(expected_path + (value,) for value in values), *open_paths]
        if open_paths:
            raise SemanticTranslationError(f"P4_TRANSLATION_INCOMPLETE_GRAPH:{open_paths}")

    @staticmethod
    def _validate_incremental_segment(
        brief: str,
        translation: BriefSemanticTranslation,
        expected_path: tuple[str, ...],
        routing: SegmentRouting | None = None,
    ) -> None:
        """Validate an ordered, still-incomplete run on one open branch."""
        if not translation.nodes:
            raise SemanticTranslationError("P4_TRANSLATION_INCREMENTAL_NODE_COUNT_INVALID")
        if routing and routing.source_excerpt and routing.source_excerpt not in brief:
            raise SemanticTranslationError("P4_TRANSLATION_ROUTING_SOURCE_EXCERPT_MISMATCH")
        segment_path = tuple(translation.nodes[0].position_path)
        for index, node in enumerate(translation.nodes):
            if any(segment.isdecimal() for segment in node.position_path):
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_NUMERIC_POSITION_PATH:{node.id}:{node.position_path}"
                )
            if tuple(node.position_path) != segment_path:
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_SEGMENT_POSITION_INCONSISTENT:{node.id}:{segment_path}:{node.position_path}"
                )
            if (routing is None or routing.kind == "current_branch") and tuple(node.position_path) != expected_path:
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_POSITION_MISMATCH:{node.id}:{expected_path}:{node.position_path}"
                )
            if node.source_excerpt not in brief:
                raise SemanticTranslationError(f"P4_TRANSLATION_SOURCE_EXCERPT_MISMATCH:{node.id}")
            try:
                definition = require_capability(node.capability)
            except ValueError as exc:
                raise SemanticTranslationError(f"P4_TRANSLATION_UNSUPPORTED:{node.capability}") from exc
            known_fields = {field.path for field in definition.fields}
            if node.capability != "persist_contact_field" and (
                node.capture_reference is not None
                or node.capture_reference_question is not None
            ):
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_CAPTURE_REFERENCE_NOT_APPLICABLE:{node.id}"
                )
            if unknown := set(node.supplied_values) - known_fields:
                raise SemanticTranslationError(f"P4_TRANSLATION_UNKNOWN_FIELDS:{node.id}:{sorted(unknown)}")
            if set(node.supplied_value_sources) != set(node.supplied_values):
                raise SemanticTranslationError(f"P4_TRANSLATION_VALUE_PROVENANCE_INCOMPLETE:{node.id}")
            for question in node.semantic_questions:
                if question.target.startswith("config."):
                    target_field = question.target.removeprefix("config.")
                    field = next(
                        (item for item in definition.fields if item.path == target_field),
                        None,
                    )
                    if field is None:
                        raise SemanticTranslationError(
                            f"P4_TRANSLATION_SEMANTIC_TARGET_INVALID:{node.id}:{target_field}"
                        )
                    if field.policy not in {AcquisitionPolicy.USER_REQUIRED, AcquisitionPolicy.DERIVED}:
                        raise SemanticTranslationError(
                            f"P4_TRANSLATION_CONTEXTUAL_FIELD_NOT_REQUIRED:{node.id}:{target_field}"
                        )
                    if target_field in node.supplied_values:
                        raise SemanticTranslationError(
                            f"P4_TRANSLATION_CONTEXTUAL_FIELD_NOT_MISSING:{node.id}:{target_field}"
                        )
                    if question.answer_type != field.answer_type:
                        raise SemanticTranslationError(
                            f"P4_TRANSLATION_CONTEXTUAL_QUESTION_TYPE_INVALID:{node.id}:{target_field}"
                        )
                    if list(question.options) != list(field.options):
                        raise SemanticTranslationError(
                            f"P4_TRANSLATION_CONTEXTUAL_QUESTION_OPTIONS_INVALID:{node.id}:{target_field}"
                        )
            if index < len(translation.nodes) - 1 and definition.exit.kind != "linear":
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_SEGMENT_NON_LINEAR_MIDPOINT:{node.id}"
                )
            if definition.exit.kind == "dynamic":
                options = node.supplied_values.get(definition.exit.source_field or "options")
                if not isinstance(options, list) or len(options) < 2:
                    raise SemanticTranslationError(f"P4_TRANSLATION_CHOICE_OPTIONS_REQUIRED:{node.id}")
                values = [str(option.get("value") or "") for option in options if isinstance(option, dict)]
                if len(values) != len(options) or any(not value for value in values) or len(set(values)) != len(values):
                    raise SemanticTranslationError(f"P4_TRANSLATION_CHOICE_OPTIONS_INVALID:{node.id}")
            if definition.exit.kind == "terminal" and index < len(translation.nodes) - 1:
                raise SemanticTranslationError(f"P4_TRANSLATION_TERMINAL_MIDPOINT:{node.id}")


WORKBENCH_SEMANTIC_PROMPT_VERSION = "product4-workbench-incremental-semantic-planning-1.2"
DEFAULT_OPENAI_CHAT_COMPLETIONS_ENDPOINT = "https://api.openai.com/v1/chat/completions"
DEFAULT_WORKBENCH_SEMANTIC_MODEL = "gpt-5.6-sol"


class IncrementalSemanticModelClient:
    """Classify one workbench instruction through the production semantic boundary.

    The workbench remains incremental and delegates node construction, questions,
    branch positions, and commits to ``RegistryInterpreter`` and ``AuthoringService``.
    This client only replaces the old keyword-only capability guess.
    """

    def __init__(self, transport: SemanticTransport):
        self.transport = transport
        self.active: TranslationNode | None = None
        self.pending_segment_nodes: list[TranslationNode] = []
        self.segment_nodes: dict[str, TranslationNode] = {}
        self.routing: SegmentRouting | None = None
        self.segment_trigger_intent: FlowTriggerIntent | None = None
        self.segment_root_node_id: str | None = None
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _environment_value(*names: str) -> str:
        for name in names:
            value = os.environ.get(name)
            if value and value.strip():
                return value
        return ""

    @classmethod
    def from_environment(
        cls,
        *,
        http_client: JsonHttpClient | None = None,
    ) -> IncrementalSemanticModelClient:
        """Build the workbench client without ever printing or persisting credentials.

        Product 4-specific variables remain the preferred configuration. The local
        demo may use the ambient OpenAI pair supplied by the desktop environment.
        """
        api_key = cls._environment_value(
            "PRODUCT4_SEMANTIC_API_KEY",
            "OPENAI_API_KEY",
        )
        if not api_key:
            raise SemanticTranslationError(
                "P4_SEMANTIC_CONFIGURATION_MISSING: set OPENAI_API_KEY for the local workbench"
            )
        endpoint = cls._environment_value(
            "PRODUCT4_SEMANTIC_ENDPOINT",
        )
        if not endpoint:
            base_url = cls._environment_value("OPENAI_BASE_URL")
            if not base_url:
                endpoint = DEFAULT_OPENAI_CHAT_COMPLETIONS_ENDPOINT
            elif base_url.rstrip("/").endswith("/chat/completions"):
                endpoint = base_url
            else:
                endpoint = f"{base_url.rstrip('/')}/chat/completions"
        model = cls._environment_value(
            "PRODUCT4_SEMANTIC_MODEL",
            "OPENAI_MODEL",
        ) or DEFAULT_WORKBENCH_SEMANTIC_MODEL
        # Do not forward an ambient project id: OpenAI API keys select their
        # project by default, and an unrelated OPENAI_PROJECT_ID causes a
        # misleading authentication failure. The workbench opts into an
        # explicit project header only through its Product 4 setting.
        project_id = cls._environment_value("PRODUCT4_SEMANTIC_PROJECT_ID") or None
        return cls(
            ProductionSemanticTransport(
                endpoint=endpoint,
                model=model,
                api_key=api_key,
                project_id=project_id,
                http_client=http_client,
            )
        )

    @staticmethod
    def _request(
        statement: str,
        position: OpenPosition,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "contract": TRANSLATION_CONTRACT_VERSION,
            "provider_schema_version": PROVIDER_SCHEMA_VERSION,
            "workbench_mode": "incremental_segment_planning",
            "brief": statement,
            "registry": registry_payload(),
            "prompt_version": WORKBENCH_SEMANTIC_PROMPT_VERSION,
            "position_path": list(position.branch_path),
            "authoring_context": context or {},
            "instructions": (
                f"Return one or more ordered {PROVIDER_RESULT_VERSION} nodes for this one "
                "incremental workbench instruction. If it is a short multi-action paragraph, "
                "decompose it into ordered single-capability nodes for one routing target. Do "
                "not translate a complete brief or add later steps, terminals, or branch child "
                "nodes that the instruction does not state. Set position_path consistently for "
                "every node in the segment. Use the authoring_context to decide routing intent: "
                "continue_current_branch, target one existing option/branch, or apply to the "
                "relevant choice group descendant leaves. Return routing.kind as current_branch, "
                "existing_branch, choice_group, or clarification. For an existing branch or "
                "choice group, return only the supplied opaque option_id/choice_group_id; never "
                "invent identifiers. Use clarification with concise human-language question "
                "and supplied option ids when the intended branch or scope is not unique. "
                "Classify the requested operation by its "
                "meaning, not by one isolated verb. A request to show a menu, offer choices, "
                "or let someone choose is fixed_choice even when it also says ask or give; "
                "capture_user_input is only for collecting a free-form answer. Use only one "
                "of the five registry capabilities. source_excerpt must be an exact contiguous "
                "substring of the instruction. Do not invent configuration. If the instruction "
                "explicitly names choice labels, return those exact labels and lower snake_case "
                "values in fixed_choice.options so the workbench can show them as user-approved "
                "suggestions. The workbench will still ask the user to approve all choice rows "
                "before commit. Leave other unresolved fields null. For each unresolved registry "
                "configuration gap, include at most one semantic_questions item targeting the "
                "exact valid config.<field> path. Its prompt should explain the missing detail "
                "naturally for this instruction and branch. Its answer_type and options must "
                "exactly match the registry field; it is wording only and cannot add, remove, "
                "or reinterpret a requirement. Do not include a configuration question for a "
                "field that already has a supplied value. Return outcome=unsupported or "
                "outcome=ambiguous instead of guessing. For persist_contact_field, when the "
                "instruction refers to an earlier captured answer, set capture_reference to "
                "the matching opaque capture_id from authoring_context.captures. If the capture "
                "was created earlier in this same ordered segment, use that earlier node's "
                "translation id as capture_reference. Never use save_as, source_variable, or a "
                "human label as the reference. The reference must precede the persist action "
                "and be on the same reachable branch. If the intended capture is genuinely "
                "unclear, leave capture_reference null and provide a concise "
                "capture_reference_question in ordinary user language; do not mention schema, "
                "field names, IDs, or internal variables. Return the required flow-level "
                "trigger_intent separately from the node capabilities. Use status=none when "
                "the instruction does not define a flow trigger. When it explicitly names "
                "one or more trigger keywords, return status=explicit, the exact approved "
                "spelling and case for each keyword, and a source_excerpt that is an exact "
                "substring containing the keyword. If the user "
                "requests a trigger but gives no exact keyword or leaves it ambiguous, return "
                "status=ambiguous with a concise human clarification question and no keywords. "
                "Never invent, paraphrase, uppercase, or normalize a keyword. Trigger intent "
                "is flow metadata and is not a sixth capability or node."
            ),
        }

    def interpret(
        self,
        *,
        statement: str,
        position: OpenPosition,
        registry: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._request(statement, position, context)
        call = {"kind": "interpret", "request": request}
        self.calls.append(call)
        request_id = getattr(self.transport, "last_request_id", None)
        validation_phase = "incremental_json"
        try:
            raw = self.transport.complete(request)
            request_id = getattr(self.transport, "last_request_id", None) or request_id
            envelope = json.loads(_repair_json_envelope(raw))
            call["provider_response"] = envelope
            envelope, discarded = _normalize_incremental_unresolved_wrappers(envelope, statement)
            if discarded:
                call["normalization"] = {
                    "discarded_provider_fields": discarded,
                }
            if envelope.get("outcome") == "translated" and envelope.get("nodes") == []:
                raw_trigger_intent = envelope.get("trigger_intent")
                try:
                    trigger_intent = (
                        FlowTriggerIntent.model_validate(raw_trigger_intent)
                        if raw_trigger_intent is not None
                        else None
                    )
                except ValidationError:
                    trigger_intent = None
                if trigger_intent is not None:
                    try:
                        validate_provider_trigger_intent(trigger_intent, statement)
                    except ValueError as exc:
                        raise SemanticTranslationError(
                            str(exc), request_id=request_id
                        ) from exc
                    if trigger_intent.status == "explicit" and trigger_intent.keywords:
                        raise SemanticTranslationError(
                            "P4_TRANSLATION_TRIGGER_ONLY",
                            code="P4_TRANSLATION_TRIGGER_ONLY",
                            request_id=request_id,
                        )
            validation_phase = "incremental_result"
            result = IncrementalProviderTranslationResult.model_validate(envelope)
            if result.outcome != "translated":
                raise SemanticTranslationError(
                    f"P4_TRANSLATION_{result.outcome.upper()}:{result.issue_code}:{result.issue_message}"
                )
            try:
                validate_provider_trigger_intent(result.trigger_intent, statement)
            except ValueError as exc:
                raise SemanticTranslationError(str(exc)) from exc
            translation = BriefSemanticTranslator._adapt_provider_result(statement, result)
            BriefSemanticTranslator._validate_incremental_segment(
                statement,
                translation,
                tuple(position.branch_path),
                result.routing,
            )
            self.segment_nodes = {node.id: node for node in translation.nodes}
            self.pending_segment_nodes = list(translation.nodes[1:])
            node = translation.nodes[0]
            self.active = node
            self.routing = result.routing
            self.segment_trigger_intent = (
                result.trigger_intent
                if result.trigger_intent is not None and result.trigger_intent.status != "none"
                else None
            )
            self.segment_root_node_id = node.id
            return self._node_result(node)
        except SemanticTranslationError as exc:
            request_id = getattr(self.transport, "last_request_id", None) or request_id
            if request_id and not exc.request_id:
                raise SemanticTranslationError(
                    str(exc),
                    code=exc.code,
                    request_id=request_id,
                    validation_fingerprint=exc.validation_fingerprint,
                    network_subtype=exc.network_subtype,
                ) from exc
            raise
        except ValidationError as exc:
            raise SemanticTranslationError(
                "P4_TRANSLATION_OUTPUT_MALFORMED",
                code="P4_TRANSLATION_OUTPUT_MALFORMED",
                request_id=request_id,
                validation_fingerprint=validation_fingerprint(exc, phase=validation_phase),
            ) from exc
        except json.JSONDecodeError as exc:
            raise SemanticTranslationError(
                "P4_TRANSLATION_OUTPUT_MALFORMED",
                code="P4_TRANSLATION_OUTPUT_MALFORMED",
                request_id=request_id,
                validation_fingerprint=simple_validation_fingerprint(
                    phase="incremental_json", issue_type="json_invalid"
                ),
            ) from exc
        except ValueError as exc:
            raise SemanticTranslationError(
                "P4_TRANSLATION_OUTPUT_MALFORMED",
                code="P4_TRANSLATION_OUTPUT_MALFORMED",
                request_id=request_id,
                validation_fingerprint=simple_validation_fingerprint(
                    phase=validation_phase, issue_type="value_error"
                ),
            ) from exc

    def _node_result(self, node: TranslationNode) -> dict[str, Any]:
        supplied_values = dict(node.supplied_values)
        contextual_questions: dict[str, dict[str, Any]] = {}
        semantic_questions = []
        for question in node.semantic_questions:
            if question.target.startswith("config."):
                contextual_questions[question.target.removeprefix("config.")] = {
                    "prompt": question.prompt,
                    "answer_type": question.answer_type,
                    "options": list(question.options),
                }
            else:
                semantic_questions.append(question)
        choice_labels = tuple(
            str(item["label"])
            for item in supplied_values.get("options", [])
            if isinstance(item, Mapping) and item.get("label")
        )
        # Choice labels can seed the explicit approval editor, but choice rows are
        # deliberately removed from the proposal so they cannot be silently committed.
        if node.capability == "fixed_choice":
            supplied_values.pop("options", None)
        result = {
            "capability": node.capability,
            "supplied_values": supplied_values,
            "acquisition_sources": {
                field_path: "confirmed_prose"
                for field_path in supplied_values
            },
            "acquisition_source_quotes": {
                field_path: node.supplied_value_sources[field_path]
                for field_path in supplied_values
                if field_path in node.supplied_value_sources
            },
            "contains_additional_actions": bool(semantic_questions),
            "source_excerpt": node.source_excerpt,
            "translation_node_id": node.id,
            "position_path": list(node.position_path),
            "choice_labels": list(choice_labels),
            "semantic_concept": node.node_statement,
            "capture_reference": node.capture_reference,
            "capture_reference_question": node.capture_reference_question,
            "contextual_questions": contextual_questions,
        }
        if (
            self.segment_trigger_intent is not None
            and node.id == self.segment_root_node_id
        ):
            result["flow_trigger_intent"] = self.segment_trigger_intent.model_dump(mode="json")
        if self.routing is not None:
            result["routing"] = self.routing.model_dump(mode="json")
        return result

    def drain_segment(self) -> list[dict[str, Any]]:
        nodes = list(self.pending_segment_nodes)
        self.pending_segment_nodes.clear()
        return [self._node_result(node) for node in nodes]

    def activate_segment_node(self, translation_node_id: str) -> None:
        node = self.segment_nodes.get(translation_node_id)
        if node is not None:
            self.active = node

    def clarify_semantics(self, *, proposal: NodeProposal, position: OpenPosition) -> dict[str, Any]:
        self.calls.append({"kind": "semantic", "proposal": proposal.model_dump(mode="json")})
        if self.active is None:
            raise SemanticTranslationError("P4_TRANSLATION_NO_ACTIVE_NODE")
        return {
            "questions": [
                {
                    "prompt": question.prompt,
                    "field_path": question.target,
                    "answer_type": question.answer_type,
                    "options": question.options,
                }
                for question in self.active.semantic_questions
                if not question.target.startswith("config.")
            ]
        }


class TranslationPlanClient:
    """Feeds a validated semantic translation into node-local authoring calls."""

    def __init__(self, translation: BriefSemanticTranslation):
        self.nodes = list(translation.nodes)
        self.active: TranslationNode | None = None
        self.calls: list[dict[str, Any]] = []

    def interpret(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"kind": "interpret", **kwargs})
        if not self.nodes:
            raise SemanticTranslationError("P4_TRANSLATION_PLAN_EXHAUSTED")
        self.active = self.nodes.pop(0)
        return {
            "capability": self.active.capability,
            "supplied_values": self.active.supplied_values,
            "acquisition_sources": {
                field_path: "confirmed_prose"
                for field_path in self.active.supplied_values
            },
            "acquisition_source_quotes": self.active.supplied_value_sources,
            "contains_additional_actions": bool(self.active.semantic_questions),
            "source_excerpt": self.active.source_excerpt,
        }

    def clarify_semantics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"kind": "semantic", **kwargs})
        if self.active is None:
            raise SemanticTranslationError("P4_TRANSLATION_NO_ACTIVE_NODE")
        return {
            "questions": [
                {
                    "prompt": question.prompt,
                    "field_path": question.target,
                    "answer_type": question.answer_type,
                    "options": question.options,
                }
                for question in self.active.semantic_questions
            ]
        }


class AnswerProvider(Protocol):
    def answer(self, **question_context: Any) -> Any: ...


class AnswerProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: Any
    decision_source: Literal[
        "confirmed_user_decision", "simulated_user_evaluation_decision"
    ] = "confirmed_user_decision"
    rationale: str | None = None
    answered_at: str | None = None
    model_identity: str | None = None
    prior_answer_context_hash: str | None = None


class BriefAuthoringResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: AuthoringSession
    translation: BriefSemanticTranslation
    turns: list[dict[str, Any]]


class BriefAuthoringService:
    def __init__(self, translator: BriefSemanticTranslator, answer_provider: AnswerProvider):
        self.translator = translator
        self.answer_provider = answer_provider

    def author(self, *, session_id: str, title: str, brief: str) -> BriefAuthoringResult:
        translation = self.translator.translate(brief)
        plan_client = TranslationPlanClient(translation)
        service = AuthoringService(RegistryInterpreter(plan_client))
        session = service.start(
            session_id,
            title,
            original_brief=brief,
            semantic_translation_hash=translation.canonical_hash(),
        )
        turns: list[dict[str, Any]] = []
        for translated_node in translation.nodes:
            current = session.open_positions[0]
            if current.branch_path != translated_node.position_path:
                raise SemanticTranslationError("P4_TRANSLATION_RUNTIME_POSITION_DRIFT")
            session = service.propose(
                session,
                translated_node.node_statement,
                translation_node_id=translated_node.id,
                position_path=translated_node.position_path,
                node_statement=translated_node.node_statement,
                source_excerpt=translated_node.source_excerpt,
                choice_labels=tuple(
                    str(item["label"])
                    for item in translated_node.supplied_values.get("options", [])
                    if isinstance(item, dict) and item.get("label")
                ),
            )
            questions = [item.model_dump(mode="json") for item in session.pending_questions]
            answers: list[dict[str, Any]] = []
            while session.state is SessionState.WAITING_FOR_ANSWER:
                question = session.pending_questions[0]
                provided = self.answer_provider.answer(
                    original_brief=brief,
                    translation_node_id=question.translation_node_id,
                    question_class=question.question_class.value,
                    field_path=str(question.field_path),
                    prompt=question.prompt,
                    question_id=question.id,
                    answer_type=question.answer_type,
                    options=list(question.options),
                    capability=question.capability,
                    position_path=list(question.position_path),
                    node_statement=question.node_statement,
                    source_excerpt=question.source_excerpt,
                    choice_labels=list(question.choice_labels),
                )
                answer_result = (
                    provided if isinstance(provided, AnswerProviderResult)
                    else AnswerProviderResult(value=provided)
                )
                answers.append({
                    "question_id": question.id,
                    "question_class": question.question_class.value,
                    "field_path": question.field_path,
                    "prompt": question.prompt,
                    "answer": answer_result.value,
                    "decision_source": answer_result.decision_source,
                })
                session = service.answer(
                    session,
                    QuestionAnswer(
                        question_id=question.id,
                        value=answer_result.value,
                        decision_source=answer_result.decision_source,
                        rationale=answer_result.rationale,
                        answered_at=answer_result.answered_at,
                        model_identity=answer_result.model_identity,
                        prior_answer_context_hash=answer_result.prior_answer_context_hash,
                    ),
                )
            committed = session.nodes[-1]
            turns.append({
                "translation_node_id": translated_node.id,
                "position_path": list(translated_node.position_path),
                "source_excerpt": translated_node.source_excerpt,
                "derived_node_statement": translated_node.node_statement,
                "interpreted_capability": committed.capability,
                "questions": questions,
                "answers": answers,
                "committed_node": committed.model_dump(mode="json"),
            })
        if session.state is not SessionState.READY_FOR_REVIEW:
            raise SemanticTranslationError("P4_TRANSLATION_DID_NOT_COMPLETE_GRAPH")
        return BriefAuthoringResult(session=session, translation=translation, turns=turns)


class StaticTextTransport:
    """Offline transport fixture that still crosses the production raw-text parser."""

    def __init__(self, response: str):
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def complete(self, request: dict[str, Any]) -> str:
        self.requests.append(request)
        return self.response


class StaticAnswerProvider:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers
        self.requests: list[dict[str, str]] = []

    def answer(
        self,
        *,
        translation_node_id: str,
        question_class: str,
        field_path: str,
        prompt: str,
        **context: Any,
    ) -> Any:
        self.requests.append({
            "translation_node_id": translation_node_id,
            "question_class": question_class,
            "field_path": field_path,
            "prompt": prompt,
            "capability": context.get("capability"),
            "position_path": list(context.get("position_path") or []),
            "node_statement": context.get("node_statement"),
            "source_excerpt": context.get("source_excerpt"),
            "choice_labels": list(context.get("choice_labels") or []),
        })
        key = f"semantic.{field_path}" if question_class == QuestionClass.SEMANTIC.value else field_path
        try:
            return self.answers[translation_node_id][key]
        except KeyError as exc:
            raise SemanticTranslationError(
                f"P4_CONFIGURATION_ANSWER_MISSING:{translation_node_id}:{key}"
            ) from exc
