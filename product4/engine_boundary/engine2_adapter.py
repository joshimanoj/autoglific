"""Deterministic normalized-graph to Product 2 Flow Spec compatibility.

This boundary lowers only the Product 4 normalized graph.  It does not infer
authoring intent, call a provider, or invoke a downstream compiler.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from product4.capabilities.registry import REGISTRY_VERSION, registry_hash
from product4.capabilities.technical_policy import (
    POLICY,
    TECHNICAL_POLICY_VERSION,
    policy_hash,
)
from product4.contracts.trigger import (
    TRIGGER_METADATA_KEY,
    TriggerMetadataValidationStage,
    validate_trigger_metadata_payload,
)

from .engine1_adapter import NormalizedEdge, NormalizedGraph, NormalizedNode

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCT2_BACKEND = PROJECT_ROOT / "product2" / "backend"
PRODUCT2_CONTRACTS = PROJECT_ROOT / "product2" / "contracts"

P2_FLOW_SPEC_SCHEMA_VERSION = "glific-flow-spec-1.0"
P2_FLOW_SPEC_SCHEMA_SHA256 = "c00b059824ca415d9d8aa0db23b0a9705ee78c005a3f52912e71ceeecf374f06"
P2_FLOW_SPEC_CAPABILITIES_VERSION = "glific-flow-spec-capabilities-1.0"
P2_FLOW_SPEC_CAPABILITIES_SHA256 = "67ec049c2aa92950ed94fa9952bb8d23950146a71990bfd84a0cc5ede9c96f9e"
P2_VERIFIED_CAPABILITIES_VERSION = "glific-import-verified-0.1"
P2_VERIFIED_CAPABILITIES_SHA256 = "047b55fb04f54cc832128ab3b011debedc2729a12c980e21d7c0550645dad609"
P2_TARGET_CONTRACT = "glific-import-verified-0.1"

_AUTHORED_CAPABILITIES = frozenset(
    {
        "send_text_message",
        "capture_user_input",
        "fixed_choice",
        "persist_contact_field",
        "end",
    }
)
_P2_NODE_MAPPING = {
    "send_text_message": "send_message",
    "capture_user_input": "ask_input",
    "fixed_choice": "ask_choice",
    "persist_contact_field": "record_request",
    "end": "end",
}
_P2_INPUT_TYPES = {"text", "number", "email", "phone"}
_P2_VARIABLE_TYPES = {
    "text": "string",
    "number": "number",
    "email": "email",
    "phone": "phone",
}
_P2_PARSERS = {
    "text": "plain_text",
    "number": "integer",
    "email": "email",
    "phone": "phone",
}
_EDGE_ROLE_ORDER = {
    "next": 10,
    "outcome": 20,
    "success": 30,
    "failure": 40,
    "default": 50,
    "invalid": 51,
    "timeout": 52,
    "retry": 60,
    "exhausted": 61,
}
_GENERATED_RULE_PREFIX = "Generated technical behavior: policy:"
_POLICY_RULES = {
    "bounded-invalid-response-retry",
    "input-retry-exhausted-terminal",
    "input-no-response-terminal",
    "choice-retry-exhausted-terminal",
    "choice-no-response-terminal",
    "persistence-failure-terminal",
}
_GENERATED_REASON_BY_RULE = {
    "input-retry-exhausted-terminal": POLICY.retry_exhausted_reason,
    "choice-retry-exhausted-terminal": POLICY.retry_exhausted_reason,
    "input-no-response-terminal": POLICY.no_response_reason,
    "choice-no-response-terminal": POLICY.no_response_reason,
    "persistence-failure-terminal": POLICY.persistence_failure_reason,
}


def _fail(code: str, detail: str | None = None) -> None:
    suffix = f":{detail}" if detail else ""
    raise ValueError(f"P4_E2_{code}{suffix}")


def _trigger_keywords(graph: NormalizedGraph) -> list[str]:
    entries = [item for item in graph.metadata if item.get("key") == TRIGGER_METADATA_KEY]
    if not entries:
        return []
    if len(entries) != 1:
        _fail("TRIGGER_METADATA_COUNT_INVALID")
    entry = entries[0]
    if entry.get("type") != "custom":
        _fail("TRIGGER_METADATA_TYPE_INVALID")
    try:
        return validate_trigger_metadata_payload(
            entry.get("value"),
            entry.get("provenance"),
            source_hash=graph.source_hash,
            stage=TriggerMetadataValidationStage.NORMALIZED_GRAPH,
        )
    except (TypeError, ValueError) as exc:
        _fail("TRIGGER_METADATA_INVALID", str(exc))
    return []


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"P4_E2_P2_CONTRACT_DRIFT:{path.name}:missing") from exc


def _json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"P4_E2_P2_CONTRACT_DRIFT:{path.name}:invalid") from exc


def _assert_pinned_product2_contracts() -> tuple[dict[str, Any], dict[str, Any]]:
    """Fail closed if the P40-pinned Product 2 contracts have changed."""

    files = (
        ("glific-flow-spec-1.0.schema.json", P2_FLOW_SPEC_SCHEMA_SHA256),
        ("glific-flow-spec-capabilities-1.0.json", P2_FLOW_SPEC_CAPABILITIES_SHA256),
        ("glific-capabilities-verified-0.1.json", P2_VERIFIED_CAPABILITIES_SHA256),
    )
    for filename, expected_hash in files:
        actual_hash = _sha256_file(PRODUCT2_CONTRACTS / filename)
        if actual_hash != expected_hash:
            _fail("P2_CONTRACT_DRIFT", f"{filename}:{actual_hash}")

    flow_capabilities = _json_file(PRODUCT2_CONTRACTS / files[1][0])
    verified_capabilities = _json_file(PRODUCT2_CONTRACTS / files[2][0])
    if flow_capabilities.get("capability_version") != P2_FLOW_SPEC_CAPABILITIES_VERSION:
        _fail("P2_CAPABILITY_VERSION_DRIFT", "flow-spec")
    if flow_capabilities.get("target_contract") != P2_TARGET_CONTRACT:
        _fail("P2_CAPABILITY_TARGET_DRIFT", "flow-spec")
    if verified_capabilities.get("contract_version") != P2_VERIFIED_CAPABILITIES_VERSION:
        _fail("P2_CAPABILITY_VERSION_DRIFT", "verified")
    return flow_capabilities, verified_capabilities


def _assert_verified_mapping(
    flow_capabilities: dict[str, Any], verified_capabilities: dict[str, Any]
) -> None:
    nodes = flow_capabilities.get("nodes", {})
    expected_nodes = {
        "send_message": {"enabled_local": True, "compiler_mapping": "send_message"},
        "ask_input": {"enabled_local": True, "compiler_mapping": "wait_router"},
        "ask_choice": {"enabled_local": True, "compiler_mapping": "interactive_wait_router"},
        "record_request": {
            "enabled_local": True,
            "compiler_mapping": "native_set_contact_field",
            "mechanisms": ["contact_fields"],
        },
        "end": {"enabled_local": True, "compiler_mapping": "end"},
    }
    for node_type, expected in expected_nodes.items():
        actual = nodes.get(node_type)
        if actual is None or any(actual.get(key) != value for key, value in expected.items()):
            _fail("P2_CAPABILITY_MAPPING_DRIFT", node_type)

    primitives = verified_capabilities.get("primitives", {})
    expected_primitives = {
        "send_text_message",
        "wait_for_text_response",
        "exact_categorical_branch",
        "set_contact_field",
        "set_run_result",
        "end_terminal",
    }
    if any(not primitives.get(name, {}).get("enabled_local", False) for name in expected_primitives):
        _fail("P2_VERIFIED_PRIMITIVE_DRIFT")
    if verified_capabilities.get("interactive_limits") != {
        "quick_reply_max_items": 3,
        "list_max_items": 10,
    }:
        _fail("P2_INTERACTIVE_LIMIT_DRIFT")


def _load_contract():
    flow_capabilities, verified_capabilities = _assert_pinned_product2_contracts()
    _assert_verified_mapping(flow_capabilities, verified_capabilities)
    backend = str(PRODUCT2_BACKEND)
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.flow_spec.contracts import GlificFlowSpec

    return GlificFlowSpec


def _slug(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("NAME_UNREPRESENTABLE", field)
    result = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not result:
        _fail("NAME_UNREPRESENTABLE", field)
    return result


def _message(text: str, locale: str = "en") -> dict[str, Any]:
    refs = re.findall(r"{{\s*([a-z][a-z0-9_]*)\s*}}", text)
    return {"text": text, "variable_refs": refs, "locale": locale}


def _node_sort_key(node: NormalizedNode) -> str:
    return node.id


def _edge_sort_key(edge: NormalizedEdge) -> tuple[int, str]:
    return (_EDGE_ROLE_ORDER.get(edge.role, 99), edge.id)


def _source_ref_key(ref: dict[str, str]) -> tuple[str, str]:
    return (str(ref.get("source_unit_id", "")), str(ref.get("source_quote", "")))


def _first_source_unit_id(node: NormalizedNode) -> str:
    return min(node.source_refs, key=_source_ref_key)["source_unit_id"]


def _sorted_source_refs(
    node: NormalizedNode, source_units: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    refs = sorted((dict(ref) for ref in node.source_refs), key=_source_ref_key)
    if not refs:
        _fail("SOURCE_REFERENCE_MISSING", node.id)
    for ref in refs:
        source_unit_id = ref.get("source_unit_id")
        source_quote = ref.get("source_quote")
        unit = source_units.get(source_unit_id)
        if unit is None or source_quote != unit.get("text"):
            _fail("SOURCE_REFERENCE_INVALID", node.id)
    return refs


def _generated_rule(node: NormalizedNode) -> str | None:
    if not node.generated_policy:
        return None
    rules: set[str] = set()
    for ref in node.source_refs:
        quote = ref.get("source_quote")
        if not isinstance(quote, str) or not quote.startswith(_GENERATED_RULE_PREFIX):
            _fail("GENERATED_NODE_PROVENANCE_INVALID", node.id)
        rule = quote.removeprefix(_GENERATED_RULE_PREFIX)
        if not rule.startswith(f"{TECHNICAL_POLICY_VERSION}:"):
            _fail("GENERATED_NODE_PROVENANCE_INVALID", node.id)
        rules.add(rule.removeprefix(f"{TECHNICAL_POLICY_VERSION}:"))
    if len(rules) != 1 or next(iter(rules), "") not in _POLICY_RULES:
        _fail("GENERATED_NODE_PROVENANCE_INVALID", node.id)
    return next(iter(rules))


def _is_omitted_lowering_node(node: NormalizedNode) -> bool:
    """Return true for policy branches Glific cannot select on action failure."""

    return node.capability == "end" and node.generated_policy and _generated_rule(node) == "persistence-failure-terminal"


def _validate_graph_lineage(graph: NormalizedGraph) -> tuple[dict[str, NormalizedNode], dict[str, list[NormalizedEdge]], dict[str, dict[str, Any]]]:
    if graph.schema_version != "product4-normalized-graph-1.0":
        _fail("NORMALIZED_SCHEMA_MISMATCH", graph.schema_version)
    if graph.registry_version != REGISTRY_VERSION or graph.registry_hash != registry_hash():
        _fail("REGISTRY_DRIFT")
    if graph.technical_policy_version != TECHNICAL_POLICY_VERSION or graph.technical_policy_hash != policy_hash():
        _fail("TECHNICAL_POLICY_DRIFT")

    nodes = {node.id: node for node in graph.nodes}
    if len(nodes) != len(graph.nodes):
        _fail("DUPLICATE_NODE")
    if not graph.source_units:
        _fail("SOURCE_UNITS_MISSING")
    source_units = {str(unit.get("id")): dict(unit) for unit in graph.source_units}
    if len(source_units) != len(graph.source_units) or any(
        not unit.get("id") or not unit.get("text") for unit in source_units.values()
    ):
        _fail("SOURCE_UNIT_INVALID")
    _trigger_keywords(graph)

    outgoing: dict[str, list[NormalizedEdge]] = defaultdict(list)
    edge_ids: set[str] = set()
    for edge in graph.edges:
        if edge.id in edge_ids:
            _fail("DUPLICATE_EDGE", edge.id)
        edge_ids.add(edge.id)
        outgoing[edge.source_id].append(edge)
        if edge.source_id not in nodes or edge.target_id not in nodes:
            _fail("DANGLING_EDGE", edge.id)
        policy_edge = edge.generated_policy
        if policy_edge:
            if not edge.provenance or any(
                item.get("source") != "approved_versioned_policy"
                or item.get("policy_version") != TECHNICAL_POLICY_VERSION
                or item.get("source_hash") is not None
                or item.get("quote") is not None
                or not str(item.get("reference", "")).startswith(
                    f"policy:{TECHNICAL_POLICY_VERSION}:"
                )
                for item in edge.provenance
            ):
                _fail("POLICY_EDGE_PROVENANCE_INVALID", edge.id)
        else:
            if not edge.provenance or any(
                item.get("source") != "confirmed_prose"
                or item.get("source_hash") != graph.source_hash
                or not isinstance(item.get("quote"), str)
                for item in edge.provenance
            ):
                _fail("AUTHORED_EDGE_PROVENANCE_INVALID", edge.id)
            for item in edge.provenance:
                quote = item["quote"]
                if not [unit for unit in source_units.values() if unit.get("text") == quote]:
                    _fail("EDGE_SOURCE_REFERENCE_INVALID", edge.id)

    for node in nodes.values():
        if node.capability not in _AUTHORED_CAPABILITIES and node.capability != "retry_policy":
            _fail("UNSUPPORTED_CAPABILITY", node.capability)
        if node.capability == "retry_policy" and not node.generated_policy:
            _fail("AUTHORED_POLICY_NODE", node.id)
        if node.generated_policy and node.capability not in {"retry_policy", "end"}:
            _fail("GENERATED_NODE_INVALID", node.id)
        _sorted_source_refs(node, source_units)
        _generated_rule(node)
    for values in outgoing.values():
        values.sort(key=_edge_sort_key)
    return nodes, outgoing, source_units


def _one_edge(
    outgoing: dict[str, list[NormalizedEdge]], node_id: str, role: str, *, generated: bool | None = None
) -> NormalizedEdge:
    matches = [edge for edge in outgoing[node_id] if edge.role == role]
    if generated is not None:
        matches = [edge for edge in matches if edge.generated_policy is generated]
    if len(matches) != 1:
        _fail("ROUTE_COUNT", f"{node_id}:{role}")
    return matches[0]


def _choice_edges(
    outgoing: dict[str, list[NormalizedEdge]], node: NormalizedNode
) -> tuple[list[NormalizedEdge], NormalizedEdge, NormalizedEdge, NormalizedEdge]:
    outcomes = [edge for edge in outgoing[node.id] if edge.role == "outcome" and not edge.generated_policy]
    default = _one_edge(outgoing, node.id, "default", generated=True)
    invalid = _one_edge(outgoing, node.id, "invalid", generated=True)
    timeout = _one_edge(outgoing, node.id, "timeout", generated=True)
    expected = {
        (item.get("value"), item.get("label"), item.get("next_node_id"))
        for item in node.config.get("outcomes", [])
    }
    actual = {
        (
            edge.condition.get("stable_value") if edge.condition else None,
            edge.condition.get("title") if edge.condition else None,
            edge.target_id,
        )
        for edge in outcomes
    }
    if len(outcomes) != len(expected) or actual != expected:
        _fail("CHOICE_OUTCOMES_MISMATCH", node.id)
    if default.target_id != invalid.target_id:
        _fail("CHOICE_RETRY_LOWERING_UNREPRESENTABLE", node.id)
    return outcomes, default, invalid, timeout


def _expand_target(
    target_id: str,
    nodes: dict[str, NormalizedNode],
    outgoing: dict[str, list[NormalizedEdge]],
) -> list[str]:
    target = nodes[target_id]
    if target.capability != "retry_policy":
        return [target_id]
    retry = _one_edge(outgoing, target_id, "retry", generated=True)
    exhausted = _one_edge(outgoing, target_id, "exhausted", generated=True)
    return [retry.target_id, exhausted.target_id]


def _ordered_executable_nodes(
    nodes: dict[str, NormalizedNode], outgoing: dict[str, list[NormalizedEdge]]
) -> tuple[list[str], str]:
    executable_ids = {
        node_id
        for node_id, node in nodes.items()
        if node.capability != "retry_policy" and not _is_omitted_lowering_node(node)
    }
    incoming_from_executable: dict[str, set[str]] = defaultdict(set)
    for source_id, edges in outgoing.items():
        if source_id not in executable_ids:
            continue
        for edge in edges:
            incoming_from_executable[edge.target_id].add(source_id)
    roots = sorted(
        node_id
        for node_id in executable_ids
        if not nodes[node_id].generated_policy and not incoming_from_executable[node_id]
    )
    if len(roots) != 1:
        _fail("ENTRY_INVALID", str(roots))

    queue = deque([roots[0]])
    ordered: list[str] = []
    seen: set[str] = set()
    while queue:
        node_id = queue.popleft()
        if node_id in seen or node_id not in executable_ids:
            continue
        seen.add(node_id)
        ordered.append(node_id)
        for edge in outgoing[node_id]:
            for expanded in _expand_target(edge.target_id, nodes, outgoing):
                if expanded not in seen:
                    queue.append(expanded)
    if seen != executable_ids:
        _fail("UNREACHABLE_NODE", str(sorted(executable_ids - seen)))
    return ordered, roots[0]


def _register_name(
    registry: dict[str, set[str]], *, namespace: str, raw: str, normalized: str
) -> None:
    values = registry.setdefault(f"{namespace}:{normalized}", set())
    values.add(raw)
    if len(values) > 1:
        _fail("NAME_COLLISION", f"{namespace}:{normalized}")


def _prepare_bindings(
    nodes: dict[str, NormalizedNode],
) -> tuple[
    dict[str, str],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
]:
    """Normalize names once and reject lossy collisions before lowering."""

    name_registry: dict[str, set[str]] = {}
    capture_variable_by_node: dict[str, str] = {}
    variables: dict[str, dict[str, Any]] = {}
    resources: dict[str, dict[str, Any]] = {}
    bindings: list[dict[str, Any]] = []
    capture_raw_by_slug: dict[str, set[str]] = defaultdict(set)

    for node in sorted(nodes.values(), key=_node_sort_key):
        if node.capability != "capture_user_input":
            continue
        config = node.config
        raw = config.get("save_as")
        normalized = _slug(raw, field=f"{node.id}.save_as")
        _register_name(name_registry, namespace="variable", raw=raw, normalized=normalized)
        capture_raw_by_slug[normalized].add(raw)
        input_type = config.get("input_type")
        if input_type not in _P2_INPUT_TYPES:
            _fail("INPUT_TYPE_UNSUPPORTED", f"{node.id}:{input_type}")
        if config.get("required") is not True:
            _fail("CAPTURE_REQUIRED_UNREPRESENTABLE", node.id)
        variable_type = _P2_VARIABLE_TYPES[input_type]
        existing = variables.get(normalized)
        if existing and existing["type"] != variable_type:
            _fail("VARIABLE_TYPE_COLLISION", normalized)
        variables[normalized] = {
            "name": normalized,
            "type": variable_type,
            "scope": "flow",
            "sensitive": False,
            "display_companion": None,
            "default": None,
        }
        capture_variable_by_node[node.id] = normalized
        bindings.append(
            {
                "sort_key": ("binding", node.id, "save_as"),
                "kind": "product4-binding-lineage",
                "target_id": node.id,
                "target_kind": "node",
                "source_unit_id": _first_source_unit_id(node),
                "params": {
                    "field": "save_as",
                    "raw": raw,
                    "normalized": normalized,
                    "executable_field": "save_as",
                },
            }
        )
        bindings.append(
            {
                "sort_key": ("required", node.id),
                "kind": "product4-capture-required",
                "target_id": node.id,
                "target_kind": "node",
                "source_unit_id": _first_source_unit_id(node),
                "params": {
                    "authored_required": True,
                    "flow_spec_field": None,
                    "lowering": "ask_input_contract_default",
                    "executable": False,
                },
            }
        )

    for node in sorted(nodes.values(), key=_node_sort_key):
        if node.capability != "fixed_choice":
            continue
        raw = f"choice_{node.id.casefold()}"
        normalized = _slug(raw, field=f"{node.id}.choice_save_as")
        _register_name(name_registry, namespace="variable", raw=raw, normalized=normalized)
        existing = variables.get(normalized)
        if existing and existing["type"] != "string":
            _fail("VARIABLE_TYPE_COLLISION", normalized)
        variables[normalized] = {
            "name": normalized,
            "type": "string",
            "scope": "flow",
            "sensitive": False,
            "display_companion": None,
            "default": None,
        }
        capture_variable_by_node[node.id] = normalized
        bindings.append(
            {
                "sort_key": ("binding", node.id, "choice_save_as"),
                "kind": "product4-binding-lineage",
                "target_id": node.id,
                "target_kind": "node",
                "source_unit_id": _first_source_unit_id(node),
                "params": {
                    "field": "generated_choice_save_as",
                    "raw": raw,
                    "normalized": normalized,
                    "executable_field": "save_as",
                },
            }
        )

    for node in sorted(nodes.values(), key=_node_sort_key):
        if node.capability != "persist_contact_field":
            continue
        config = node.config
        raw_source = config.get("source_variable")
        source_slug = _slug(raw_source, field=f"{node.id}.source_variable")
        candidates = capture_raw_by_slug.get(source_slug, set())
        if not candidates:
            _fail("PERSISTENCE_SOURCE_UNBOUND", node.id)
        if raw_source not in candidates or len(candidates) != 1:
            _fail("BINDING_COLLISION", node.id)
        raw_field = config.get("field_name")
        field_slug = _slug(raw_field, field=f"{node.id}.field_name")
        _register_name(name_registry, namespace="contact_field", raw=raw_field, normalized=field_slug)
        resource_name = f"contact_field_{field_slug}"
        resources[resource_name] = {
            "logical_name": resource_name,
            "kind": "contact_field",
            "platform_id": None,
            "binding_state": "generated_in_package",
        }
        bindings.append(
            {
                "sort_key": ("binding", node.id, "source_variable"),
                "kind": "product4-binding-lineage",
                "target_id": node.id,
                "target_kind": "node",
                "source_unit_id": _first_source_unit_id(node),
                "params": {
                    "field": "source_variable",
                    "raw": raw_source,
                    "normalized": source_slug,
                    "executable_field": "fields",
                },
            }
        )
        bindings.append(
            {
                "sort_key": ("binding", node.id, "field_name"),
                "kind": "product4-binding-lineage",
                "target_id": node.id,
                "target_kind": "node",
                "source_unit_id": _first_source_unit_id(node),
                "params": {
                    "field": "field_name",
                    "raw": raw_field,
                    "normalized": field_slug,
                    "executable_field": "fields",
                },
            }
        )

    return capture_variable_by_node, variables, resources, bindings


def _policy_edge_source_unit(
    edge: NormalizedEdge,
    nodes: dict[str, NormalizedNode],
    source_units: dict[str, dict[str, Any]],
) -> str:
    quotes = sorted(
        {
            item.get("quote")
            for item in edge.provenance
            if isinstance(item.get("quote"), str)
        }
    )
    if quotes:
        source_node_refs = sorted(nodes[edge.source_id].source_refs, key=_source_ref_key)
        owner_matches = [
            ref["source_unit_id"]
            for ref in source_node_refs
            if ref.get("source_quote") == quotes[0]
        ]
        if len(owner_matches) == 1:
            return owner_matches[0]
        matches = [unit_id for unit_id, unit in source_units.items() if unit.get("text") == quotes[0]]
        if len(matches) != 1:
            _fail("EDGE_SOURCE_REFERENCE_AMBIGUOUS", edge.id)
        return matches[0]
    source_refs = sorted(nodes[edge.source_id].source_refs, key=_source_ref_key)
    if not source_refs:
        _fail("EDGE_SOURCE_REFERENCE_MISSING", edge.id)
    return source_refs[0]["source_unit_id"]


def _metadata_entry(
    item: dict[str, Any],
    index: int,
    source_units: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_unit_id = item["source_unit_id"]
    unit = source_units.get(source_unit_id)
    if unit is None:
        _fail("METADATA_SOURCE_REFERENCE_INVALID", source_unit_id)
    return {
        "id": f"M{index:03d}",
        "kind": item["kind"],
        "target_id": item["target_id"],
        "target_kind": item["target_kind"],
        "params": item["params"],
        "source_ref": {"source_unit_id": source_unit_id, "source_quote": unit["text"]},
    }


def _validate_outgoing_route_shapes(
    nodes: dict[str, NormalizedNode], outgoing: dict[str, list[NormalizedEdge]]
) -> None:
    """Reject any route shape that is not part of the normalized contract."""

    for node in sorted(nodes.values(), key=_node_sort_key):
        if node.capability == "send_text_message":
            expected = {"next": (1, 1, False)}
        elif node.capability == "capture_user_input":
            expected = {
                "next": (1, 1, False),
                "exhausted": (1, 1, True),
                "timeout": (1, 1, True),
            }
        elif node.capability == "fixed_choice":
            outcomes = node.config.get("outcomes")
            if not isinstance(outcomes, list) or not 2 <= len(outcomes) <= 10:
                _fail("CHOICE_OUTCOMES_INVALID", node.id)
            expected = {
                "outcome": (len(outcomes), len(outcomes), False),
                "default": (1, 1, True),
                "invalid": (1, 1, True),
                "timeout": (1, 1, True),
            }
        elif node.capability == "persist_contact_field":
            expected = {
                "success": (1, 1, False),
                "failure": (1, 1, True),
            }
        elif node.capability == "end":
            expected = {}
        elif node.capability == "retry_policy":
            expected = {
                "retry": (1, 1, True),
                "exhausted": (1, 1, True),
            }
        else:
            _fail("UNSUPPORTED_CAPABILITY", node.capability)

        edges = sorted(outgoing.get(node.id, []), key=lambda item: item.id)
        counts: dict[str, int] = defaultdict(int)
        for edge in edges:
            shape = expected.get(edge.role)
            if shape is None:
                _fail("ROUTE_ROLE_UNEXPECTED", f"{node.id}:{edge.id}:{edge.role}")
            if edge.generated_policy is not shape[2]:
                _fail(
                    "ROUTE_EDGE_AUTHORSHIP_MISMATCH",
                    f"{node.id}:{edge.id}:{edge.role}",
                )
            counts[edge.role] += 1

        for role, (minimum, maximum, _) in expected.items():
            count = counts.get(role, 0)
            if count < minimum or count > maximum:
                _fail("ROUTE_CARDINALITY_INVALID", f"{node.id}:{role}:{count}")


def _validate_routes(
    nodes: dict[str, NormalizedNode], outgoing: dict[str, list[NormalizedEdge]]
) -> list[dict[str, Any]]:
    _validate_outgoing_route_shapes(nodes, outgoing)
    folded_retry: list[dict[str, Any]] = []
    for node in sorted(nodes.values(), key=_node_sort_key):
        if node.capability == "send_text_message":
            _one_edge(outgoing, node.id, "next", generated=False)
        elif node.capability == "capture_user_input":
            next_edge = _one_edge(outgoing, node.id, "next", generated=False)
            exhausted = _one_edge(outgoing, node.id, "exhausted", generated=True)
            timeout = _one_edge(outgoing, node.id, "timeout", generated=True)
            config = node.config
            retry = config.get("retry", {})
            no_response = config.get("no_response", {})
            if next_edge.target_id != config.get("next_node_id"):
                _fail("ROUTE_MISMATCH", node.id)
            if exhausted.target_id != retry.get("on_exhausted_node_id"):
                _fail("POLICY_ROUTE_MISMATCH", node.id)
            if timeout.target_id != no_response.get("next_node_id"):
                _fail("POLICY_ROUTE_MISMATCH", node.id)
            if len({next_edge.target_id, exhausted.target_id, timeout.target_id}) != 3:
                _fail("POLICY_ROUTE_ALIAS", node.id)
            if retry.get("max_attempts") != POLICY.retry.max_attempts or retry.get("messages") != list(POLICY.retry.messages):
                _fail("RETRY_POLICY_MISMATCH", node.id)
            if no_response.get("timeout_seconds") != POLICY.no_response_timeout_seconds:
                _fail("NO_RESPONSE_POLICY_MISMATCH", node.id)
            for target_id, rule in (
                (exhausted.target_id, "input-retry-exhausted-terminal"),
                (timeout.target_id, "input-no-response-terminal"),
            ):
                target = nodes.get(target_id)
                if target is None or not target.generated_policy or _generated_rule(target) != rule:
                    _fail("POLICY_TARGET_MISMATCH", node.id)
        elif node.capability == "fixed_choice":
            outcomes, default, invalid, timeout = _choice_edges(outgoing, node)
            retry_node = nodes.get(default.target_id)
            if retry_node is None or retry_node.capability != "retry_policy" or not retry_node.generated_policy:
                _fail("CHOICE_RETRY_POLICY_MISSING", node.id)
            retry_rule = _generated_rule(retry_node)
            if retry_rule != "bounded-invalid-response-retry":
                _fail("CHOICE_RETRY_POLICY_MISMATCH", node.id)
            retry_edge = _one_edge(outgoing, retry_node.id, "retry", generated=True)
            exhausted_edge = _one_edge(outgoing, retry_node.id, "exhausted", generated=True)
            if retry_edge.target_id != node.id:
                _fail("RETRY_RETURN_TARGET_INVALID", node.id)
            if retry_node.config.get("on_exhausted_node_id") != exhausted_edge.target_id:
                _fail("RETRY_EXHAUSTION_TARGET_MISMATCH", node.id)
            if retry_node.config.get("max_attempts") != POLICY.retry.max_attempts or retry_node.config.get("messages") != list(POLICY.retry.messages):
                _fail("RETRY_POLICY_MISMATCH", node.id)
            if exhausted_edge.target_id == timeout.target_id or exhausted_edge.target_id in {
                edge.target_id for edge in outcomes
            } or timeout.target_id in {edge.target_id for edge in outcomes}:
                _fail("POLICY_ROUTE_ALIAS", node.id)
            timeout_target = nodes.get(timeout.target_id)
            exhausted_target = nodes.get(exhausted_edge.target_id)
            if (
                timeout_target is None
                or not timeout_target.generated_policy
                or _generated_rule(timeout_target) != "choice-no-response-terminal"
                or exhausted_target is None
                or not exhausted_target.generated_policy
                or _generated_rule(exhausted_target) != "choice-retry-exhausted-terminal"
            ):
                _fail("POLICY_TARGET_MISMATCH", node.id)
            folded_retry.append(
                {
                    "choice_id": node.id,
                    "retry_node_id": retry_node.id,
                    "edge_ids": [default.id, invalid.id, retry_edge.id, exhausted_edge.id, timeout.id],
                    "policy": f"policy:{TECHNICAL_POLICY_VERSION}:bounded-invalid-response-retry",
                    "source_unit_id": _first_source_unit_id(retry_node),
                }
            )
        elif node.capability == "persist_contact_field":
            success = _one_edge(outgoing, node.id, "success", generated=False)
            failure = _one_edge(outgoing, node.id, "failure", generated=True)
            config = node.config
            if success.target_id != config.get("success_node_id") or failure.target_id != config.get("failure_node_id"):
                _fail("ROUTE_MISMATCH", node.id)
            if success.target_id == failure.target_id:
                _fail("PERSISTENCE_FAILURE_ALIAS", node.id)
            target = nodes.get(failure.target_id)
            if target is None or not target.generated_policy or _generated_rule(target) != "persistence-failure-terminal":
                _fail("PERSISTENCE_FAILURE_TARGET_INVALID", node.id)
        elif node.capability == "end":
            if outgoing[node.id]:
                _fail("TERMINAL_HAS_OUTGOING", node.id)
        elif node.capability == "retry_policy":
            retry = _one_edge(outgoing, node.id, "retry", generated=True)
            exhausted = _one_edge(outgoing, node.id, "exhausted", generated=True)
            if retry.target_id not in nodes or nodes[retry.target_id].capability != "fixed_choice":
                _fail("RETRY_RETURN_TARGET_INVALID", node.id)
            if exhausted.target_id != node.config.get("on_exhausted_node_id"):
                _fail("RETRY_EXHAUSTION_TARGET_MISMATCH", node.id)
    return folded_retry


def _build_source_coverage(
    graph: NormalizedGraph,
    nodes: dict[str, NormalizedNode],
    outgoing: dict[str, list[NormalizedEdge]],
    target_id: dict[str, str],
    folded_retry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    flow_node_ids_by_unit: dict[str, set[str]] = defaultdict(set)
    requirement_ids_by_unit: dict[str, set[str]] = defaultdict(set)
    for node_id, node in nodes.items():
        if node.capability == "retry_policy":
            owner = next((item["choice_id"] for item in folded_retry if item["retry_node_id"] == node_id), None)
            flow_ids = {target_id[owner]} if owner else set()
        else:
            flow_ids = {target_id[node_id]} if node_id in target_id else set()
        for ref in node.source_refs:
            source_unit_id = ref["source_unit_id"]
            flow_node_ids_by_unit[source_unit_id].update(flow_ids)
            requirement_ids_by_unit[source_unit_id].add(node_id)

    for edge in graph.edges:
        source_unit_id = _policy_edge_source_unit(edge, nodes, {str(item.get("id")): item for item in graph.source_units})
        source_node = edge.source_id
        if nodes[source_node].capability == "retry_policy":
            owner = next((item["choice_id"] for item in folded_retry if item["retry_node_id"] == source_node), None)
            if owner and owner in target_id:
                flow_node_ids_by_unit[source_unit_id].add(target_id[owner])
        elif source_node in target_id:
            flow_node_ids_by_unit[source_unit_id].add(target_id[source_node])
        requirement_ids_by_unit[source_unit_id].add(edge.id)

    source_units = {str(item["id"]): item for item in graph.source_units}
    ordered_nodes = {node_id: index for index, node_id in enumerate(target_id, start=1)}
    return [
        {
            "source_unit_id": source_unit_id,
            "source_quote": source_units[source_unit_id]["text"],
            "status": "covered" if flow_node_ids_by_unit[source_unit_id] else "informational",
            "flow_node_ids": sorted(
                flow_node_ids_by_unit[source_unit_id], key=lambda value: ordered_nodes.get(next((node for node, flow in target_id.items() if flow == value), ""), 0)
            ),
            "semantic_requirement_ids": sorted(requirement_ids_by_unit[source_unit_id]),
            "rationale": "Product 4 normalized-graph source lineage retained at the Flow Spec boundary.",
        }
        for source_unit_id in sorted(source_units)
    ]


def _build_metadata(
    graph: NormalizedGraph,
    nodes: dict[str, NormalizedNode],
    outgoing: dict[str, list[NormalizedEdge]],
    source_units: dict[str, dict[str, Any]],
    target_id: dict[str, str],
    root_id: str,
    folded_retry: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for edge in sorted(graph.edges, key=lambda item: item.id):
        entries.append(
            {
                "sort_key": ("edge", edge.id),
                "kind": "product4-edge-lineage",
                "target_id": edge.id,
                "target_kind": "edge",
                "source_unit_id": _policy_edge_source_unit(edge, nodes, source_units),
                "params": {
                    "source_id": edge.source_id,
                    "target_id": edge.target_id,
                    "role": edge.role,
                    "condition": edge.condition,
                    "generated_policy": edge.generated_policy,
                    "provenance": sorted(
                        (dict(item) for item in edge.provenance),
                        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
                    ),
                },
            }
        )

    for item in folded_retry:
        entries.append(
            {
                "sort_key": ("policy-fold", item["choice_id"]),
                "kind": "product4-policy-fold",
                "target_id": target_id[item["choice_id"]],
                "target_kind": "node",
                "source_unit_id": item["source_unit_id"],
                "params": {
                    "lowering": "retry_policy_to_flow_spec_retry",
                    "choice_node_id": item["choice_id"],
                    "retry_node_id": item["retry_node_id"],
                    "edge_ids": sorted(item["edge_ids"]),
                    "policy": item["policy"],
                    "retry_exhausted_route": "retry.on_exhausted_node_id",
                    "no_response_route": "no_response.next_node_id",
                },
            }
        )

    for item in bindings:
        item = dict(item)
        item["target_id"] = target_id[item["target_id"]]
        entries.append(item)

    root_source_unit_id = min(source_units)
    entries.append(
        {
            "sort_key": ("graph", "metadata"),
            "kind": "product4-graph-lineage",
            "target_id": target_id[root_id],
            "target_kind": "node",
            "source_unit_id": root_source_unit_id,
            "params": {
                "metadata": sorted(
                    (dict(item) for item in graph.metadata),
                    key=lambda item: (str(item.get("type", "")), str(item.get("key", ""))),
                ),
                "source_package_hash": graph.source_package_hash,
                "source_hash": graph.source_hash,
            },
        }
    )
    entries.sort(key=lambda item: item["sort_key"])
    return [_metadata_entry(item, index, source_units) for index, item in enumerate(entries, start=1)]


def build_flow_spec(graph: NormalizedGraph):
    """Build one pinned Product 2 ``glific-flow-spec-1.0`` object."""

    GlificFlowSpec = _load_contract()
    nodes, outgoing, source_units = _validate_graph_lineage(graph)
    trigger_keywords = _trigger_keywords(graph)
    folded_retry = _validate_routes(nodes, outgoing)
    ordered, root_id = _ordered_executable_nodes(nodes, outgoing)
    target_id = {node_id: f"F{index:03d}" for index, node_id in enumerate(ordered, start=1)}
    capture_variable_by_node, variables, resources, bindings = _prepare_bindings(nodes)

    flow_nodes: list[dict[str, Any]] = []
    choice_counter = 1
    for source_id in ordered:
        node = nodes[source_id]
        config = node.config
        outgoing_edges = sorted(outgoing[source_id], key=lambda item: item.id)
        common = {
            "id": target_id[source_id],
            "name": f"{source_id} {node.capability}"[:200],
            "source_refs": _sorted_source_refs(node, source_units),
            "semantic_reference_ids": {
                "node_ids": [source_id],
                "edge_ids": [edge.id for edge in outgoing_edges],
            },
            "generated_from_decision_ids": [],
        }
        if node.capability == "send_text_message":
            next_node = _one_edge(outgoing, source_id, "next", generated=False).target_id
            flow_nodes.append(
                {
                    **common,
                    "type": _P2_NODE_MAPPING[node.capability],
                    "message": _message(config["copy"], config["locale"]),
                    "next_node_id": target_id[next_node],
                }
            )
        elif node.capability == "capture_user_input":
            input_type = config["input_type"]
            save_as = capture_variable_by_node[source_id]
            retry = config["retry"]
            no_response = config["no_response"]
            flow_nodes.append(
                {
                    **common,
                    "type": _P2_NODE_MAPPING[node.capability],
                    "message": _message(config["prompt"]),
                    "input_type": input_type,
                    "save_as": save_as,
                    "validation": {
                        "parser": _P2_PARSERS[input_type],
                        "constraints": config["validation"],
                        "invalid_message": POLICY.retry.invalid_message,
                    },
                    "retry": {
                        "max_attempts": retry["max_attempts"],
                        "messages": retry["messages"],
                        "on_exhausted_node_id": target_id[retry["on_exhausted_node_id"]],
                    },
                    "no_response": {
                        "timeout_seconds": no_response["timeout_seconds"],
                        "next_node_id": target_id[no_response["next_node_id"]],
                    },
                    "next_node_id": target_id[config["next_node_id"]],
                }
            )
        elif node.capability == "fixed_choice":
            outcomes, _, _, timeout = _choice_edges(outgoing, node)
            retry_node_id = node.config["default_node_id"]
            retry_node = nodes[retry_node_id]
            exhausted_target = retry_node.config["on_exhausted_node_id"]
            choices = []
            for outcome in config["outcomes"]:
                choices.append(
                    {
                        "id": f"CH{choice_counter:03d}",
                        "title": outcome["label"],
                        "submitted_value": outcome["value"],
                        "next_node_id": target_id[outcome["next_node_id"]],
                    }
                )
                choice_counter += 1
            if len(choices) != len(outcomes):
                _fail("CHOICE_OUTCOMES_MISMATCH", source_id)
            flow_nodes.append(
                {
                    **common,
                    "type": _P2_NODE_MAPPING[node.capability],
                    "message": _message(config["title"]),
                    "presentation": "quick_reply" if len(choices) <= 3 else "list",
                    "choices": choices,
                    "save_as": capture_variable_by_node[source_id],
                    "retry": {
                        "max_attempts": retry_node.config["max_attempts"],
                        "messages": retry_node.config["messages"],
                        "on_exhausted_node_id": target_id[exhausted_target],
                    },
                    "no_response": {
                        "timeout_seconds": POLICY.no_response_timeout_seconds,
                        "next_node_id": target_id[timeout.target_id],
                    },
                }
            )
        elif node.capability == "persist_contact_field":
            field_name = _slug(config["field_name"], field=f"{source_id}.field_name")
            source_variable = _slug(config["source_variable"], field=f"{source_id}.source_variable")
            flow_nodes.append(
                {
                    **common,
                    "type": _P2_NODE_MAPPING[node.capability],
                    "mechanism": "contact_fields",
                    "fields": {field_name: "{{" + source_variable + "}}"},
                    "resource_ref": f"contact_field_{field_name}",
                    "success_node_id": target_id[config["success_node_id"]],
                }
            )
        elif node.capability == "end":
            rule = _generated_rule(node)
            if (
                node.generated_policy
                and rule in _GENERATED_REASON_BY_RULE
                and config["reason"] != _GENERATED_REASON_BY_RULE[rule]
            ):
                _fail("GENERATED_TERMINAL_REASON_MISMATCH", source_id)
            flow_nodes.append(
                {
                    **common,
                    "type": _P2_NODE_MAPPING[node.capability],
                    "reason": config["reason"][:200],
                }
            )
        else:
            _fail("UNSUPPORTED_CAPABILITY", node.capability)

    metadata = _build_metadata(
        graph,
        nodes,
        outgoing,
        source_units,
        target_id,
        root_id,
        folded_retry,
        bindings,
    )
    source_coverage = _build_source_coverage(
        graph,
        nodes,
        outgoing,
        target_id,
        folded_retry,
    )
    locales = sorted(
        {
            node.config.get("locale")
            for node in nodes.values()
            if node.capability == "send_text_message" and node.config.get("locale")
        }
    )
    payload = {
        "schema_version": P2_FLOW_SPEC_SCHEMA_VERSION,
        "source": {
            "product1_session_id": "product4-engine1",
            "product1_generation_id": graph.source_package_hash,
            "source_hash": graph.source_hash,
        },
        "target": {
            "platform": "glific",
            "contract_version": P2_TARGET_CONTRACT,
            "language": locales[0] if len(locales) == 1 else "en",
            "timezone": "Asia/Kolkata",
        },
        "flow": {
            "id": str(uuid5(NAMESPACE_URL, f"product4:glific-flow-spec:{graph.source_package_hash}")),
            "name": graph.title[:200],
            "description": "Generated from frozen Product 4 normalized graph.",
            "entry_node_id": target_id[root_id],
            "keywords": trigger_keywords,
        },
        "variables": sorted(variables.values(), key=lambda item: item["name"]),
        "resources": sorted(resources.values(), key=lambda item: item["logical_name"]),
        "integrations": [],
        "nodes": flow_nodes,
        "implementation_decisions": [],
        "metadata": metadata,
        "source_coverage": source_coverage,
        "acceptance_scenarios": [],
    }
    return GlificFlowSpec.model_validate(payload)


def build_compatibility_report(graph: NormalizedGraph, spec: Any) -> dict[str, Any]:
    """Return a concise deterministic P48 transformation/compatibility report."""

    node_types = [node.type for node in spec.nodes]
    choice_nodes = [node for node in spec.nodes if node.type == "ask_choice"]
    capture_nodes = [node for node in spec.nodes if node.type == "ask_input"]
    persistence_nodes = [node for node in spec.nodes if node.type == "record_request"]
    folded = [
        item.params
        for item in spec.metadata
        if item.kind == "product4-policy-fold"
    ]
    normalized_bindings = [
        item.params
        for item in spec.metadata
        if item.kind == "product4-binding-lineage"
    ]
    return {
        "schema_version": "product4-p48-flow-spec-compatibility-1.0",
        "contract": {
            "flow_spec_version": P2_FLOW_SPEC_SCHEMA_VERSION,
            "flow_spec_schema_sha256": P2_FLOW_SPEC_SCHEMA_SHA256,
            "flow_spec_capabilities_version": P2_FLOW_SPEC_CAPABILITIES_VERSION,
            "flow_spec_capabilities_sha256": P2_FLOW_SPEC_CAPABILITIES_SHA256,
            "verified_capabilities_version": P2_VERIFIED_CAPABILITIES_VERSION,
            "verified_capabilities_sha256": P2_VERIFIED_CAPABILITIES_SHA256,
            "target_contract": P2_TARGET_CONTRACT,
        },
        "preserved": [
            "send_text_message copy and locale",
            "capture_user_input prompt, input_type, save_as, validation, success continuation",
            "fixed_choice title, labels, stable submitted values, destinations",
            "persist_contact_field source binding, contact field, and authored success destination",
            "authored and generated terminal reasons",
            "normalized node and edge source references with policy provenance",
        ],
        "deterministically_lowered": [
            "send_text_message -> send_message",
            "capture_user_input -> ask_input",
            "fixed_choice -> ask_choice",
            "persist_contact_field -> record_request/contact_fields (runtime failure is external)",
            "end -> end",
            "choice retry_policy -> ask_choice.retry",
            "choice presentation: <=3 quick_reply, >3 list",
            "variable and contact-field names -> P2 slugs with collision rejection",
        ],
        "intentionally_non_executable_metadata": [
            {
                "field": "capture_user_input.required",
                "decision": "retained as product4-capture-required metadata; P2 ask_input has no required field",
                "accepted_value": True,
            }
        ],
        "lossy_transformations": [
            "Flow Spec has no authored capture-required field; false required values are rejected.",
            "Flow Spec has no standalone retry-policy node; policy node and edge lineage are retained in metadata and folded into retry settings.",
            "Normalized persistence-failure policy route is validated for provenance but omitted because ordinary Glific action nodes follow their first exit only.",
        ],
        "policy_folding": folded,
        "normalized_bindings": normalized_bindings,
        "counts": {
            "source_nodes": len(graph.nodes),
            "source_edges": len(graph.edges),
            "flow_spec_nodes": len(spec.nodes),
            "flow_spec_choices": sum(len(node.choices) for node in choice_nodes),
            "ask_input_nodes": len(capture_nodes),
            "record_request_nodes": len(persistence_nodes),
            "generated_policy_nodes": sum(1 for node in graph.nodes if node.generated_policy),
            "generated_policy_edges": sum(1 for edge in graph.edges if edge.generated_policy),
        },
        "p2_node_types": sorted(set(node_types)),
        "blockers": [],
    }


__all__ = [
    "P2_FLOW_SPEC_CAPABILITIES_SHA256",
    "P2_FLOW_SPEC_SCHEMA_SHA256",
    "P2_FLOW_SPEC_SCHEMA_VERSION",
    "build_compatibility_report",
    "build_flow_spec",
]
