from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import Product1Bundle
from app.flow_spec.capabilities import (
    load_flow_spec_capabilities,
    node_capability,
    parser_enabled_locally,
)
from app.flow_spec.contracts import (
    AskChoiceNode,
    AskInputNode,
    CallWebhookNode,
    EvaluateNode,
    GlificFlowSpec,
    ImplementationDecision,
    RecordRequestNode,
)

ISSUE_CODES = (
    "FS_SOURCE_HASH_MISMATCH",
    "FS_REFERENCE_INVALID",
    "FS_SOURCE_COVERAGE_MISSING",
    "FS_GENERATED_BEHAVIOR_PROVENANCE_MISSING",
    "FS_QUESTION_MESSAGE_MISSING",
    "FS_CHOICE_TITLE_MISSING",
    "FS_CHOICE_VALUE_MISSING",
    "FS_INPUT_STORAGE_MISSING",
    "FS_INPUT_RETRY_MISSING",
    "FS_DECISION_OPERAND_MISSING",
    "FS_ROUTER_VALUE_UNPRODUCIBLE",
    "FS_VARIABLE_READ_BEFORE_WRITE",
    "FS_VARIABLE_TYPE_MISMATCH",
    "FS_USER_DECISION_NOT_INTERACTIVE",
    "FS_VALIDATION_RULE_MISSING",
    "FS_VALIDATION_DECISION_IGNORED",
    "FS_EXTERNAL_PREDICATE_SOURCE_MISSING",
    "FS_TYPED_INPUT_DEGRADED",
    "FS_DECISION_ANSWER_NOT_APPLIED",
    "FS_DECISION_APPLICATION_PATH_INVALID",
    "FS_DECISION_SEMANTIC_EFFECT_MISSING",
    "FS_INSTRUCTION_TEXT_EXPOSED",
    "FS_MESSAGE_PLACEHOLDER_UNRESOLVED",
    "FS_CONFIRMATION_VALUE_MISSING",
    "FS_END_METADATA_EXPOSED",
    "FS_RECORDING_MECHANISM_MISSING",
    "FS_ACTION_RESOURCE_MISSING",
    "FS_ACTION_FAILURE_ROUTE_MISSING",
    "FS_ACTION_SUCCESS_ROUTE_MISSING",
    "FS_MATERIAL_ACTION_NOT_OPERATIONAL",
    "FS_UNSUPPORTED_CAPABILITY",
    "FS_RESOURCE_BINDING_MISSING",
    "FS_GRAPH_UNREACHABLE",
    "FS_TERMINAL_ROUTE_INVALID",
    "FS_SECRET_DETECTED",
)


class FlowSpecValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    path: str = Field(min_length=1)
    message: str = Field(min_length=1)
    classification: str = "repairable_flow_spec"
    repair_instruction: str = Field(min_length=1)


class FlowSpecValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "flow-spec-validation-1.0"
    source_hash: str
    passed: bool
    phase: str = "flow_spec_validation"
    issues: list[FlowSpecValidationIssue] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)
    canonical_hash: str | None = None


_PLACEHOLDER = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}")
_CONFIRMATION_LANGUAGE = re.compile(
    r"\b(confirm|confirmation|confirmed|recorded|scheduled|request received)\b",
    re.IGNORECASE,
)
_VALUE_MODIFIERS = (
    r"(?:selected|chosen|requested|entered|provided|captured|submitted|stored|saved)"
)
_VALUE_NOUNS = r"(?:value|time|date|slot|option|choice|request|answer|response)"
_VALUE_REFERENCE = (
    rf"(?:{_VALUE_MODIFIERS}(?:\s+\w+){{0,3}}\s+{_VALUE_NOUNS}"
    rf"|{_VALUE_NOUNS}(?:\s+\w+){{0,3}}\s+{_VALUE_MODIFIERS})"
)
_PRESENTATION_VERB = (
    r"(?:display\w*|show\w*|repeat\w*|echo\w*|include\w*|state\w*|"
    r"mention\w*|present\w*|provide\w*|return\w*|restate\w*|read\w*)"
)
_VALUE_PRESENTATION_PATTERNS = (
    re.compile(
        rf"\b{_PRESENTATION_VERB}\b[^.!?\n]{{0,80}}\b{_VALUE_REFERENCE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_VALUE_REFERENCE}\b[^.!?\n]{{0,80}}\b{_PRESENTATION_VERB}\b",
        re.IGNORECASE,
    ),
)
_EXPLICIT_CONFIRMATION_VALUE_PATTERNS = (
    re.compile(
        rf"\b(?:confirm|confirmed)\s+(?:that\s+)?(?:the\s+)?{_VALUE_REFERENCE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bconfirmation\s+(?:of|for)\s+(?:the\s+)?{_VALUE_REFERENCE}\b",
        re.IGNORECASE,
    ),
)
_SOURCE_CLAUSE_BREAKS = re.compile(r"\b(?:and|then|but|after|before)\b", re.IGNORECASE)
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[a-z0-9._-]{12,}"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?[a-z0-9/_+=.-]{12,}"),
    re.compile(r"sk-[a-zA-Z0-9]{16,}"),
)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def scan_flow_spec_secrets(value: object) -> list[str]:
    text = canonical_json(value)
    return [pattern.pattern for pattern in _SECRET_PATTERNS if pattern.search(text)]


def _issue(
    issues: list[FlowSpecValidationIssue],
    code: str,
    path: str,
    message: str,
    repair: str,
    *,
    classification: str = "repairable_flow_spec",
) -> None:
    issues.append(
        FlowSpecValidationIssue(
            code=code,
            path=path,
            message=message,
            classification=classification,
            repair_instruction=repair,
        )
    )


def _node_outgoing(node: Any) -> list[str]:
    if isinstance(node, AskChoiceNode):
        result = [choice.next_node_id for choice in node.choices]
        if node.retry:
            result.append(node.retry.on_exhausted_node_id)
        if node.no_response:
            result.append(node.no_response.next_node_id)
        return result
    if isinstance(node, AskInputNode):
        result = [node.next_node_id]
        if node.retry:
            result.append(node.retry.on_exhausted_node_id)
        if node.no_response:
            result.append(node.no_response.next_node_id)
        return result
    if isinstance(node, CallWebhookNode):
        routes = node.webhook.routes
        return [
            value
            for value in (
                routes.success_node_id,
                routes.empty_node_id,
                routes.not_found_node_id,
                routes.conflict_node_id,
                routes.invalid_response_node_id,
                routes.http_error_node_id,
                routes.timeout_node_id,
            )
            if value
        ]
    attributes = (
        "next_node_id",
        "success_node_id",
        "failure_node_id",
        "default_node_id",
    )
    result: list[str] = []
    for attribute in attributes:
        value = getattr(node, attribute, None)
        if value:
            result.append(value)
    if hasattr(node, "cases") and node.cases:
        result.extend(case.next_node_id for case in node.cases)
    return result


def _node_reads(node: Any) -> set[str]:
    reads: set[str] = set()
    if hasattr(node, "message") and node.message:
        reads.update(node.message.variable_refs)
    if hasattr(node, "caption") and node.caption:
        reads.update(node.caption.variable_refs)
    if isinstance(node, AskChoiceNode):
        return reads
    if isinstance(node, AskInputNode):
        return reads
    if (
        hasattr(node, "operand")
        and node.operand is not None
        and node.operand.source == "variable"
        and node.operand.variable
    ):
        reads.add(node.operand.variable)
    if hasattr(node, "source_variable"):
        reads.add(node.source_variable)
    if isinstance(node, (RecordRequestNode,)):
        for template in node.fields.values():
            reads.update(_PLACEHOLDER.findall(template))
    if hasattr(node, "context_fields"):
        for template in node.context_fields.values():
            reads.update(_PLACEHOLDER.findall(template))
    if hasattr(node, "input_mappings"):
        reads.update(node.input_mappings.values())
    if isinstance(node, CallWebhookNode):
        for value in node.webhook.query.values():
            reads.update(_PLACEHOLDER.findall(value))
        if isinstance(node.webhook.body, str):
            reads.update(_PLACEHOLDER.findall(node.webhook.body))
    return reads


def _node_writes(node: Any, variables: dict[str, Any] | None = None) -> set[str]:
    writes: set[str] = set()
    if isinstance(node, (AskChoiceNode, AskInputNode)):
        writes.add(node.save_as)
        variable = (variables or {}).get(node.save_as)
        if variable is not None and variable.display_companion:
            writes.add(variable.display_companion)
    if isinstance(node, CallWebhookNode):
        writes.update(mapping.variable for mapping in node.webhook.response_mappings)
    if hasattr(node, "output_mappings"):
        writes.update(node.output_mappings.values())
    return writes


def _source_texts_for_node(
    node: Any, bundle: Product1Bundle | None
) -> list[str]:
    """Return only source text applicable to this user-facing node."""

    source_units = {unit.id: unit for unit in bundle.source_units} if bundle else {}
    texts: list[str] = []
    for ref in node.source_refs:
        texts.append(ref.source_quote)
        unit = source_units.get(ref.source_unit_id)
        if unit is not None:
            texts.extend((unit.text, unit.normalized_text))
    return texts


def _source_requires_value_interpolation(
    node: Any, bundle: Product1Bundle | None
) -> bool:
    """Detect an explicit source requirement to present a captured value."""

    patterns = (*_VALUE_PRESENTATION_PATTERNS, *_EXPLICIT_CONFIRMATION_VALUE_PATTERNS)
    return any(
        pattern.search(clause)
        for text in _source_texts_for_node(node, bundle)
        for clause in _SOURCE_CLAUSE_BREAKS.split(text)
        for pattern in patterns
    )


def _all_paths_initialized(spec: GlificFlowSpec) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    nodes = {node.id: node for node in spec.nodes}
    states_by_node: dict[str, set[frozenset[str]]] = defaultdict(set)
    work: list[tuple[str, frozenset[str]]] = [(spec.flow.entry_node_id, frozenset())]
    while work:
        node_id, state = work.pop()
        if node_id not in nodes or state in states_by_node[node_id]:
            continue
        states_by_node[node_id].add(state)
        after = set(state) | _node_writes(
            nodes[node_id], {item.name: item for item in spec.variables}
        )
        for target in _node_outgoing(nodes[node_id]):
            work.append((target, frozenset(after)))
    incoming: dict[str, set[str]] = {}
    outgoing: dict[str, set[str]] = {}
    for node_id in nodes:
        states = states_by_node.get(node_id, set())
        incoming[node_id] = set.intersection(*(set(state) for state in states)) if states else set()
        outgoing[node_id] = (
            set.union(
                *(
                    set(state)
                    | _node_writes(nodes[node_id], {item.name: item for item in spec.variables})
                    for state in states
                )
            )
            if states
            else set()
        )
    return incoming, outgoing


def _is_instruction(text: str) -> bool:
    normalized = text.strip().lower()
    return bool(
        re.match(
            r"^(greet|ask|offer|send|confirm|record|save|check|end|route|call|help|tell|show)\b",
            normalized,
        )
        or normalized in {"end the flow", "confirm selected time", "send a confirmation"}
        or "the patient" in normalized
        and normalized.startswith(("ask", "tell", "offer"))
    )


def _ref_path_exists(spec: GlificFlowSpec, path: str) -> bool:
    if not path.startswith("/") or ".." in path:
        return False
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return False
    collections = {
        "nodes": (spec.nodes, "id"),
        "variables": (spec.variables, "name"),
        "resources": (spec.resources, "logical_name"),
        "integrations": (spec.integrations, "name"),
    }
    collection = collections.get(parts[0])
    if collection is None:
        return False
    items, identity = collection
    value_item = next((item for item in items if getattr(item, identity) == parts[1]), None)
    if value_item is None:
        return False
    value: Any = value_item.model_dump(mode="json", by_alias=True)
    for part in parts[2:]:
        if isinstance(value, dict):
            if part not in value:
                return False
            value = value[part]
        elif isinstance(value, list) and part.isdigit():
            index = int(part)
            if index >= len(value):
                return False
            value = value[index]
        else:
            return False
    return True


def _decision_effect_issues(
    spec: GlificFlowSpec,
    issues: list[FlowSpecValidationIssue],
    decisions: Iterable[ImplementationDecision],
) -> None:
    nodes_by_type = defaultdict(list)
    for node in spec.nodes:
        nodes_by_type[node.type].append(node)
    for decision in decisions:
        if decision.answer is None:
            continue
        applied_decision = next(
            (item for item in spec.implementation_decisions if item.id == decision.id),
            decision,
        )
        if applied_decision is decision and decision.id not in {
            item.id for item in spec.implementation_decisions
        }:
            _issue(
                issues,
                "FS_DECISION_ANSWER_NOT_APPLIED",
                "/implementation_decisions",
                f"Answered decision {decision.id} is absent from the generated spec.",
                "Include every answered decision and its application evidence.",
            )
        decision_for_evidence = applied_decision
        if applied_decision is not decision and applied_decision.answer != decision.answer:
            _issue(
                issues,
                "FS_DECISION_ANSWER_NOT_APPLIED",
                f"/implementation_decisions/{decision.id}/answer",
                f"Generated decision {decision.id} does not preserve the answered value.",
                "Copy the resolved answer into the generated implementation_decisions section.",
            )
        if not decision_for_evidence.applied_paths:
            _issue(
                issues,
                "FS_DECISION_ANSWER_NOT_APPLIED",
                f"/implementation_decisions/{decision.id}",
                f"Decision {decision.id} has an answer but no application paths.",
                "Regenerate the complete spec with explicit JSON paths and evidence for this answer.",
            )
        for path in decision_for_evidence.applied_paths:
            if not _ref_path_exists(spec, path):
                _issue(
                    issues,
                    "FS_DECISION_APPLICATION_PATH_INVALID",
                    f"/implementation_decisions/{decision.id}/applied_paths",
                    f"Decision application path {path} does not point into the Flow Spec.",
                    "Use a path to a real node, choice, variable, resource, or integration in the candidate spec.",
                )
        if not decision_for_evidence.expected_semantic_effect or not decision_for_evidence.evidence:
            _issue(
                issues,
                "FS_DECISION_SEMANTIC_EFFECT_MISSING",
                f"/implementation_decisions/{decision.id}",
                f"Decision {decision.id} lacks expected-effect evidence.",
                "Describe the behavior changed by the answer and cite the resulting spec paths.",
            )
        category = decision.category
        answer = decision.answer
        if category == "option_data_source" and answer == "fixed":
            if not any(
                isinstance(node, AskChoiceNode) and len(node.choices) >= 2 for node in spec.nodes
            ):
                _issue(
                    issues,
                    "FS_DECISION_SEMANTIC_EFFECT_MISSING",
                    f"/implementation_decisions/{decision.id}",
                    "The fixed-options answer is not reflected in a visible finite choice interaction.",
                    "Add the visible fixed choices and cite their node path.",
                )
        if category == "validation_source" and answer == "local_12_hour":
            if not any(
                isinstance(node, AskInputNode)
                and node.input_type == "time"
                and node.validation
                and node.validation.parser == "local_12_hour_time"
                for node in spec.nodes
            ):
                _issue(
                    issues,
                    "FS_VALIDATION_DECISION_IGNORED",
                    f"/implementation_decisions/{decision.id}",
                    "The local 12-hour callback validation answer was not applied.",
                    "Add a typed time input with the local_12_hour_time parser and prompt example.",
                )
        if category == "recording_mechanism":
            record_nodes = [node for node in spec.nodes if isinstance(node, RecordRequestNode)]
            if not record_nodes or not any(node.mechanism == answer for node in record_nodes):
                _issue(
                    issues,
                    "FS_MATERIAL_ACTION_NOT_OPERATIONAL",
                    f"/implementation_decisions/{decision.id}",
                    "The recording-mechanism answer does not produce a matching operational record_request node.",
                    "Create a record_request node with the selected mechanism, destination resource, fields, and failure route.",
                )
        if category == "validation_source" and answer == "external_lookup":
            if not nodes_by_type["call_webhook"] or not any(
                isinstance(node, EvaluateNode) for node in spec.nodes
            ):
                _issue(
                    issues,
                    "FS_EXTERNAL_PREDICATE_SOURCE_MISSING",
                    f"/implementation_decisions/{decision.id}",
                    "The external booking-validity answer lacks a webhook result and predicate.",
                    "Add a verified external result mapping and an evaluate predicate, or ask for a different material answer.",
                )


def validate_flow_spec(
    spec: GlificFlowSpec,
    bundle: Product1Bundle | None = None,
    decisions: Any | None = None,
    *,
    capabilities: dict[str, Any] | None = None,
) -> FlowSpecValidationReport:
    """Run all deterministic Flow Spec gates in one report."""

    # Import here to keep the module-level union imports compact and to make
    # the public type boundary explicit.
    issues: list[FlowSpecValidationIssue] = []
    capabilities = capabilities or load_flow_spec_capabilities()
    if bundle is not None and spec.source.source_hash != bundle.source_hash:
        _issue(
            issues,
            "FS_SOURCE_HASH_MISMATCH",
            "/source/source_hash",
            "The Flow Spec source hash does not match the confirmed Product 1 bundle.",
            "Regenerate the complete spec against the current confirmed source.",
        )

    node_ids = {node.id for node in spec.nodes}
    variable_names = {variable.name for variable in spec.variables}
    resource_names = {resource.logical_name for resource in spec.resources}
    integration_names = {integration.name for integration in spec.integrations}
    if spec.flow.entry_node_id not in node_ids:
        _issue(
            issues,
            "FS_REFERENCE_INVALID",
            "/flow/entry_node_id",
            "The flow entry node does not exist.",
            "Point the entry node to a generated Flow Spec node.",
        )

    # References, source quotes, and provenance.
    for node in spec.nodes:
        for target in _node_outgoing(node):
            if target not in node_ids:
                _issue(
                    issues,
                    "FS_REFERENCE_INVALID",
                    f"/nodes/{node.id}",
                    f"Node {node.id} routes to unknown node {target}.",
                    "Regenerate the complete graph with valid node references.",
                )
        if node.type != "end" and not node.source_refs and not node.generated_from_decision_ids:
            _issue(
                issues,
                "FS_GENERATED_BEHAVIOR_PROVENANCE_MISSING",
                f"/nodes/{node.id}/source_refs",
                f"Generated behavior node {node.id} has no source or decision provenance.",
                "Attach exact source refs or a decision/capability provenance record.",
            )
        for ref in node.source_refs:
            if bundle is not None:
                unit = next(
                    (item for item in bundle.source_units if item.id == ref.source_unit_id), None
                )
                if unit is None or not (
                    ref.source_quote in unit.text or ref.source_quote in unit.normalized_text
                ):
                    _issue(
                        issues,
                        "FS_REFERENCE_INVALID",
                        f"/nodes/{node.id}/source_refs",
                        f"Source reference {ref.source_unit_id} is not an exact quote from the confirmed source.",
                        "Use an exact source-unit quote from Product 1.",
                    )
        if node.type not in capabilities.get("nodes", {}):
            _issue(
                issues,
                "FS_UNSUPPORTED_CAPABILITY",
                f"/nodes/{node.id}/type",
                f"Node type {node.type} is not present in the verified capability catalog.",
                "Ask for a material unsupported-behavior resolution; do not silently lower the node.",
                classification="unsupported_capability",
            )
        elif not node_capability(node.type).get("enabled_local", False):
            _issue(
                issues,
                "FS_UNSUPPORTED_CAPABILITY",
                f"/nodes/{node.id}/type",
                f"Node type {node.type} is not enabled by the verified local capability contract.",
                "Bind a verified capability or block the build explicitly.",
                classification="unsupported_capability",
            )

    # Source coverage is required when the bundle is available.
    coverage_by_id = {entry.source_unit_id: entry for entry in spec.source_coverage}
    if bundle is not None:
        for unit in bundle.source_units:
            entry = coverage_by_id.get(unit.id)
            if entry is None:
                _issue(
                    issues,
                    "FS_SOURCE_COVERAGE_MISSING",
                    "/source_coverage",
                    f"Source unit {unit.id} has no coverage entry.",
                    "Add a covered or informational source-coverage entry with the exact quote.",
                )
            elif not (
                entry.source_quote in unit.text or entry.source_quote in unit.normalized_text
            ):
                _issue(
                    issues,
                    "FS_REFERENCE_INVALID",
                    f"/source_coverage/{unit.id}",
                    f"Coverage quote for {unit.id} is not exact source text.",
                    "Use the exact source-unit text.",
                )
        for entry in spec.source_coverage:
            if entry.source_unit_id not in {unit.id for unit in bundle.source_units}:
                _issue(
                    issues,
                    "FS_REFERENCE_INVALID",
                    f"/source_coverage/{entry.source_unit_id}",
                    "Source coverage references an unknown Product 1 source unit.",
                    "Use a source unit ID from the confirmed Product 1 bundle.",
                )
            if any(node_id not in node_ids for node_id in entry.flow_node_ids):
                _issue(
                    issues,
                    "FS_REFERENCE_INVALID",
                    f"/source_coverage/{entry.source_unit_id}/flow_node_ids",
                    "Source coverage references an unknown Flow Spec node.",
                    "Point coverage to existing Flow Spec nodes.",
                )

    # Interaction completeness, message quality, and interpolation.
    for node in spec.nodes:
        if hasattr(node, "message") and node.message:
            message = node.message
            if not message.text.strip():
                _issue(
                    issues,
                    "FS_QUESTION_MESSAGE_MISSING"
                    if node.type in {"ask_choice", "ask_input"}
                    else "FS_INSTRUCTION_TEXT_EXPOSED",
                    f"/nodes/{node.id}/message/text",
                    "The user-facing message is empty.",
                    "Write exact patient-facing copy for this interaction.",
                )
            if bundle is not None:
                for unit in bundle.source_units:
                    if message.text.strip() == unit.text.strip() and _is_instruction(message.text):
                        _issue(
                            issues,
                            "FS_INSTRUCTION_TEXT_EXPOSED",
                            f"/nodes/{node.id}/message/text",
                            "A source implementation instruction was exposed as patient-facing copy.",
                            "Rewrite it as a direct patient-facing message while retaining source traceability.",
                        )
            placeholders = set(_PLACEHOLDER.findall(message.text))
            declared_refs = set(message.variable_refs)
            if placeholders - variable_names:
                _issue(
                    issues,
                    "FS_MESSAGE_PLACEHOLDER_UNRESOLVED",
                    f"/nodes/{node.id}/message",
                    f"Message references undeclared variables: {sorted(placeholders - variable_names)}.",
                    "Declare and initialize every interpolated variable before this message.",
                )
            if placeholders - declared_refs:
                _issue(
                    issues,
                    "FS_MESSAGE_PLACEHOLDER_UNRESOLVED",
                    f"/nodes/{node.id}/message/variable_refs",
                    "Every interpolation must be listed in variable_refs.",
                    "Add the exact interpolated variable names to variable_refs.",
                )
            if _is_instruction(message.text) or message.text.strip().lower() in {
                "end the flow",
                "confirm selected time",
            }:
                _issue(
                    issues,
                    "FS_INSTRUCTION_TEXT_EXPOSED",
                    f"/nodes/{node.id}/message/text",
                    "The message reads like an implementation instruction rather than user-facing copy.",
                    "Rewrite the message in the patient's voice.",
                )
            if (
                _CONFIRMATION_LANGUAGE.search(message.text)
                and not placeholders
                and _source_requires_value_interpolation(node, bundle)
            ):
                _issue(
                    issues,
                    "FS_CONFIRMATION_VALUE_MISSING",
                    f"/nodes/{node.id}/message",
                    "The confirmed source requirement explicitly requires displaying a selected or requested value, but the message has no interpolation.",
                    "Include the applicable stored display value or requested value in the confirmation.",
                )
            if re.search(r"\bend (the )?flow\b|end reason", message.text.lower()):
                _issue(
                    issues,
                    "FS_END_METADATA_EXPOSED",
                    f"/nodes/{node.id}/message/text",
                    "End metadata was emitted as user-facing text.",
                    "Keep the terminal reason internal and use a separate preceding message.",
                )

        if isinstance(node, AskChoiceNode):
            if not node.message.text.strip():
                _issue(
                    issues,
                    "FS_QUESTION_MESSAGE_MISSING",
                    f"/nodes/{node.id}/message",
                    "Choice question has no prompt.",
                    "Add a direct user-facing prompt.",
                )
            if node.save_as not in variable_names:
                _issue(
                    issues,
                    "FS_INPUT_STORAGE_MISSING",
                    f"/nodes/{node.id}/save_as",
                    "Choice response storage variable is undeclared.",
                    "Declare the save_as variable.",
                )
            limit = capabilities.get("interactive_limits", {}).get(
                "quick_reply_max" if node.presentation == "quick_reply" else "list_max",
                3 if node.presentation == "quick_reply" else 10,
            )
            if len(node.choices) > limit:
                _issue(
                    issues,
                    "FS_UNSUPPORTED_CAPABILITY",
                    f"/nodes/{node.id}/choices",
                    "The interactive presentation exceeds the verified item limit.",
                    "Use a verified list, narrowing question, or material follow-up.",
                    classification="unsupported_capability",
                )
            seen_values: set[str] = set()
            for index, choice in enumerate(node.choices):
                if not choice.title.strip():
                    _issue(
                        issues,
                        "FS_CHOICE_TITLE_MISSING",
                        f"/nodes/{node.id}/choices/{index}/title",
                        "Choice title is empty.",
                        "Use a user-facing visible title.",
                    )
                if not choice.submitted_value.strip():
                    _issue(
                        issues,
                        "FS_CHOICE_VALUE_MISSING",
                        f"/nodes/{node.id}/choices/{index}/submitted_value",
                        "Choice value is empty.",
                        "Use a stable submitted value produced by this choice.",
                    )
                if choice.submitted_value in seen_values:
                    _issue(
                        issues,
                        "FS_ROUTER_VALUE_UNPRODUCIBLE",
                        f"/nodes/{node.id}/choices/{index}",
                        "Choice submitted values must be unique.",
                        "Give each visible choice a distinct stable value.",
                    )
                seen_values.add(choice.submitted_value)
                if (
                    choice.id.lower() in choice.title.lower()
                    or choice.id.lower() in choice.submitted_value.lower()
                ):
                    _issue(
                        issues,
                        "FS_CHOICE_TITLE_MISSING",
                        f"/nodes/{node.id}/choices/{index}/title",
                        "An internal choice ID is exposed as user-facing data.",
                        "Use a domain-facing title separate from the stable ID.",
                    )
            if node.retry is None:
                _issue(
                    issues,
                    "FS_INPUT_RETRY_MISSING",
                    f"/nodes/{node.id}/retry",
                    "Choice question has no invalid-input retry policy.",
                    "Add bounded retry copy and an exhausted route.",
                )
        if isinstance(node, AskInputNode):
            if not node.message.text.strip():
                _issue(
                    issues,
                    "FS_QUESTION_MESSAGE_MISSING",
                    f"/nodes/{node.id}/message",
                    "Input question has no prompt.",
                    "Add exact user-facing input instructions.",
                )
            if node.save_as not in variable_names:
                _issue(
                    issues,
                    "FS_INPUT_STORAGE_MISSING",
                    f"/nodes/{node.id}/save_as",
                    "Input response storage variable is undeclared.",
                    "Declare the save_as variable with a compatible type.",
                )
            if node.retry is None:
                _issue(
                    issues,
                    "FS_INPUT_RETRY_MISSING",
                    f"/nodes/{node.id}/retry",
                    "Input question has no invalid-input retry policy.",
                    "Add bounded retry behavior and an exhausted route.",
                )
            if node.input_type != "text" or node.validation is not None:
                if node.validation is None:
                    _issue(
                        issues,
                        "FS_VALIDATION_RULE_MISSING",
                        f"/nodes/{node.id}/validation",
                        "Typed input has no validation rule.",
                        "Provide a verified parser and invalid-input message.",
                    )
                elif not parser_enabled_locally(node.validation.parser):
                    _issue(
                        issues,
                        "FS_TYPED_INPUT_DEGRADED",
                        f"/nodes/{node.id}/validation/parser",
                        f"Parser {node.validation.parser} is not verified locally.",
                        "Use a verified parser or block the unsupported validator.",
                        classification="unsupported_capability",
                    )

    # Variables/resources/integrations and direct dataflow.
    for variable in spec.variables:
        if variable.display_companion and variable.display_companion not in variable_names:
            _issue(
                issues,
                "FS_REFERENCE_INVALID",
                f"/variables/{variable.name}/display_companion",
                "Display companion is undeclared.",
                "Declare the display companion variable.",
            )
        if variable.scope == "persistent" and not any(
            resource.logical_name == variable.name or resource.kind == "contact_field"
            for resource in spec.resources
        ):
            _issue(
                issues,
                "FS_RESOURCE_BINDING_MISSING",
                f"/variables/{variable.name}",
                "Persistent variables require a contact-field resource.",
                "Bind a contact-field resource or keep the value flow-local.",
                classification="missing_resource",
            )
    for resource in spec.resources:
        if (
            resource.binding_state == "required_at_import"
            and resource.platform_id is None
            and resource.kind not in {"ticket_target", "contact_field", "collection"}
        ):
            _issue(
                issues,
                "FS_RESOURCE_BINDING_MISSING",
                f"/resources/{resource.logical_name}",
                "This resource requires a platform binding before packaging.",
                "Provide a verified platform binding or block the build.",
                classification="missing_resource",
            )
    for integration in spec.integrations:
        if integration.auth_ref and not re.fullmatch(r"[A-Z][A-Z0-9_]*", integration.auth_ref):
            _issue(
                issues,
                "FS_SECRET_DETECTED",
                f"/integrations/{integration.name}/auth_ref",
                "Integration auth refs must be symbolic allowlisted names.",
                "Use a symbolic reference; never persist a credential.",
                classification="secret_safety",
            )

    incoming, _ = _all_paths_initialized(spec)
    for node in spec.nodes:
        reads = _node_reads(node)
        for variable in reads - incoming.get(node.id, set()):
            if variable not in variable_names:
                _issue(
                    issues,
                    "FS_REFERENCE_INVALID",
                    f"/nodes/{node.id}",
                    f"Node reads undeclared variable {variable}.",
                    "Declare the variable and initialize it before the read.",
                )
            else:
                _issue(
                    issues,
                    "FS_VARIABLE_READ_BEFORE_WRITE",
                    f"/nodes/{node.id}",
                    f"Variable {variable} may be read before it is initialized on every incoming path.",
                    "Route the writer before this read or provide an explicit default.",
                )
        if isinstance(node, (AskChoiceNode, AskInputNode)) and node.save_as in variable_names:
            variable = next(item for item in spec.variables if item.name == node.save_as)
            expected = (
                "string"
                if isinstance(node, AskChoiceNode)
                else {
                    "text": "string",
                    "number": "number",
                    "date": "date",
                    "time": "time",
                    "datetime": "datetime",
                    "email": "email",
                    "phone": "phone",
                    "location": "location",
                    "media": "media",
                }[node.input_type]
            )
            if variable.type != expected:
                _issue(
                    issues,
                    "FS_VARIABLE_TYPE_MISMATCH",
                    f"/nodes/{node.id}/save_as",
                    f"Variable {node.save_as} has type {variable.type}, incompatible with {expected}.",
                    "Use a compatible declared variable type.",
                )
        if hasattr(node, "operand"):
            operand = node.operand
            if operand is None:
                _issue(
                    issues,
                    "FS_DECISION_OPERAND_MISSING",
                    f"/nodes/{node.id}/operand",
                    "Decision has no operand.",
                    "Name the exact produced value or verified expression.",
                )
            elif operand.source == "variable" and operand.variable not in variable_names:
                _issue(
                    issues,
                    "FS_DECISION_OPERAND_MISSING",
                    f"/nodes/{node.id}/operand/variable",
                    "Decision operand variable is undeclared.",
                    "Reference a declared variable with a producer.",
                )
            elif operand.source == "system_expression" and operand.expression not in {
                "booking_reference_nonempty",
                "contact_locale",
                "session_open",
            }:
                _issue(
                    issues,
                    "FS_EXTERNAL_PREDICATE_SOURCE_MISSING",
                    f"/nodes/{node.id}/operand/expression",
                    "Decision uses an unverified system expression.",
                    "Use an explicitly verified expression or material external result.",
                )
            elif operand.source == "webhook_result":
                webhook_nodes = [item for item in spec.nodes if isinstance(item, CallWebhookNode)]
                if not webhook_nodes or not any(
                    mapping.variable == operand.expression
                    for item in webhook_nodes
                    for mapping in item.webhook.response_mappings
                ):
                    _issue(
                        issues,
                        "FS_EXTERNAL_PREDICATE_SOURCE_MISSING",
                        f"/nodes/{node.id}/operand",
                        "Webhook-result predicate has no declared response mapping producer.",
                        "Add a verified webhook response mapping or use an explicit system expression.",
                    )
            if (
                isinstance(node, EvaluateNode)
                and operand is not None
                and operand.source == "variable"
                and operand.variable
                not in _node_writes(next((item for item in spec.nodes if item.id == node.id), node))
                and not any(operand.variable in _node_writes(item) for item in spec.nodes)
            ):
                _issue(
                    issues,
                    "FS_ROUTER_VALUE_UNPRODUCIBLE",
                    f"/nodes/{node.id}/operand",
                    "Decision operand has no producer.",
                    "Add the input, mapping, or expression that produces the operand.",
                )
            if (
                isinstance(node, EvaluateNode)
                and operand is not None
                and operand.source == "variable"
                and not any(
                    isinstance(item, (AskChoiceNode, AskInputNode, CallWebhookNode))
                    and operand.variable in _node_writes(item)
                    for item in spec.nodes
                )
            ):
                _issue(
                    issues,
                    "FS_USER_DECISION_NOT_INTERACTIVE",
                    f"/nodes/{node.id}/operand",
                    "The decision operand is not produced by an interaction, mapping, or verified expression.",
                    "Add the user-facing ask/capture or a verified system/external producer before evaluating it.",
                )
            if isinstance(node, EvaluateNode) and not node.cases:
                _issue(
                    issues,
                    "FS_DECISION_OPERAND_MISSING",
                    f"/nodes/{node.id}/cases",
                    "Decision has no cases.",
                    "Declare routes over the produced operand.",
                )

    # Actions and resources.
    for node in spec.nodes:
        if isinstance(node, RecordRequestNode):
            if not node.mechanism or not node.resource_ref:
                _issue(
                    issues,
                    "FS_RECORDING_MECHANISM_MISSING",
                    f"/nodes/{node.id}",
                    "record_request has no mechanism or destination.",
                    "Provide a supported mechanism and bound destination resource.",
                )
            if node.resource_ref not in resource_names:
                _issue(
                    issues,
                    "FS_ACTION_RESOURCE_MISSING",
                    f"/nodes/{node.id}/resource_ref",
                    "record_request destination resource is undeclared.",
                    "Declare the destination resource and its binding state.",
                )
            if not node.success_node_id:
                _issue(
                    issues,
                    "FS_ACTION_SUCCESS_ROUTE_MISSING",
                    f"/nodes/{node.id}",
                    "record_request must have a success continuation.",
                    "Add the authored success continuation; platform action failure is an external runtime error.",
                )
            if not node.fields:
                _issue(
                    issues,
                    "FS_MATERIAL_ACTION_NOT_OPERATIONAL",
                    f"/nodes/{node.id}/fields",
                    "record_request has no operational fields.",
                    "Persist the selected values needed for later handling.",
                )
            supported = node_capability("record_request").get("mechanisms", [])
            if node.mechanism not in supported:
                _issue(
                    issues,
                    "FS_UNSUPPORTED_CAPABILITY",
                    f"/nodes/{node.id}/mechanism",
                    f"Recording mechanism {node.mechanism} is not verified locally.",
                    "Select a verified mechanism or return a material unsupported-capability blocker.",
                    classification="unsupported_capability",
                )
        if isinstance(node, CallWebhookNode):
            if node.webhook.integration_ref not in integration_names:
                _issue(
                    issues,
                    "FS_ACTION_RESOURCE_MISSING",
                    f"/nodes/{node.id}/webhook/integration_ref",
                    "Webhook integration is undeclared.",
                    "Declare the symbolic integration binding.",
                )
            if node.webhook.mutating and not node.webhook.idempotency_key:
                _issue(
                    issues,
                    "FS_ACTION_FAILURE_ROUTE_MISSING",
                    f"/nodes/{node.id}/webhook/idempotency_key",
                    "Mutating webhook has no idempotency key.",
                    "Declare a stable idempotency-key source.",
                )

    # Decision answer application and material validity checks.
    source_decisions = (
        decisions.decisions if hasattr(decisions, "decisions") else spec.implementation_decisions
    )
    if bundle is not None:
        for decision in source_decisions:
            for ref in decision.source_refs:
                unit = next(
                    (item for item in bundle.source_units if item.id == ref.source_unit_id),
                    None,
                )
                if unit is None or not (
                    ref.source_quote in unit.text or ref.source_quote in unit.normalized_text
                ):
                    _issue(
                        issues,
                        "FS_REFERENCE_INVALID",
                        f"/implementation_decisions/{decision.id}/source_refs",
                        f"Source reference {ref.source_unit_id} is not a quote from its confirmed source unit.",
                        "Use an exact quote from the referenced Product 1 source unit.",
                    )
    if decisions is not None and hasattr(decisions, "decisions"):
        spec_by_id = {item.id: item for item in spec.implementation_decisions}
        for item in decisions.decisions:
            if item.answer is not None and item.id not in spec_by_id:
                _issue(
                    issues,
                    "FS_DECISION_ANSWER_NOT_APPLIED",
                    "/implementation_decisions",
                    f"Answered decision {item.id} is absent from the generated spec.",
                    "Include every answered decision and its application evidence.",
                )
    _decision_effect_issues(spec, issues, source_decisions)

    # Graph reachability and terminal behavior.
    adjacency = {node.id: _node_outgoing(node) for node in spec.nodes}
    reachable: set[str] = set()
    stack = [spec.flow.entry_node_id]
    while stack:
        node_id = stack.pop()
        if node_id in reachable or node_id not in node_ids:
            continue
        reachable.add(node_id)
        stack.extend(adjacency.get(node_id, []))
    for node_id in sorted(node_ids - reachable):
        _issue(
            issues,
            "FS_GRAPH_UNREACHABLE",
            f"/nodes/{node_id}",
            f"Node {node_id} is unreachable from the entry node.",
            "Remove it or connect it through an explicit route.",
        )
    for node in spec.nodes:
        if node.type == "end" and _node_outgoing(node):
            _issue(
                issues,
                "FS_TERMINAL_ROUTE_INVALID",
                f"/nodes/{node.id}",
                "End nodes cannot have outgoing routes.",
                "Use a separate message/action before the End node.",
            )

    secrets = scan_flow_spec_secrets(spec.model_dump(mode="json", by_alias=True))
    if secrets:
        _issue(
            issues,
            "FS_SECRET_DETECTED",
            "/",
            "A likely credential appears in the Flow Spec.",
            "Remove literal credentials and keep only symbolic refs.",
            classification="secret_safety",
        )

    checks = {
        "source_hash": not any(issue.code == "FS_SOURCE_HASH_MISMATCH" for issue in issues),
        "references": not any(
            issue.code in {"FS_REFERENCE_INVALID", "FS_GRAPH_UNREACHABLE"} for issue in issues
        ),
        "coverage": not any(issue.code == "FS_SOURCE_COVERAGE_MISSING" for issue in issues),
        "user_interactions": not any(
            issue.code.startswith("FS_QUESTION")
            or issue.code.startswith("FS_CHOICE")
            or issue.code.startswith("FS_INPUT")
            for issue in issues
        ),
        "dataflow": not any(
            issue.code.startswith("FS_DECISION")
            or issue.code.startswith("FS_ROUTER")
            or issue.code.startswith("FS_VARIABLE")
            or issue.code == "FS_USER_DECISION_NOT_INTERACTIVE"
            for issue in issues
        ),
        "validation": not any(
            issue.code
            in {
                "FS_VALIDATION_RULE_MISSING",
                "FS_VALIDATION_DECISION_IGNORED",
                "FS_EXTERNAL_PREDICATE_SOURCE_MISSING",
                "FS_TYPED_INPUT_DEGRADED",
            }
            for issue in issues
        ),
        "decisions": not any(
            issue.code
            in {
                "FS_DECISION_ANSWER_NOT_APPLIED",
                "FS_DECISION_APPLICATION_PATH_INVALID",
                "FS_DECISION_SEMANTIC_EFFECT_MISSING",
            }
            for issue in issues
        ),
        "copy": not any(
            issue.code
            in {
                "FS_INSTRUCTION_TEXT_EXPOSED",
                "FS_MESSAGE_PLACEHOLDER_UNRESOLVED",
                "FS_CONFIRMATION_VALUE_MISSING",
                "FS_END_METADATA_EXPOSED",
            }
            for issue in issues
        ),
        "actions": not any(
            issue.code
            in {
                "FS_RECORDING_MECHANISM_MISSING",
                "FS_ACTION_RESOURCE_MISSING",
                "FS_ACTION_SUCCESS_ROUTE_MISSING",
                "FS_MATERIAL_ACTION_NOT_OPERATIONAL",
            }
            for issue in issues
        ),
        "capabilities": not any(
            issue.code in {"FS_UNSUPPORTED_CAPABILITY", "FS_RESOURCE_BINDING_MISSING"}
            for issue in issues
        ),
        "graph": not any(
            issue.code in {"FS_GRAPH_UNREACHABLE", "FS_TERMINAL_ROUTE_INVALID"} for issue in issues
        ),
        "secrets": not any(issue.code == "FS_SECRET_DETECTED" for issue in issues),
    }
    payload = spec.model_dump(mode="json", by_alias=True)
    return FlowSpecValidationReport(
        source_hash=spec.source.source_hash,
        passed=not issues,
        issues=issues,
        checks=checks,
        canonical_hash=canonical_hash(payload),
    )
