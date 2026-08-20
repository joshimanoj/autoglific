"""Frozen ``authoring-package-1.0`` to canonical normalized graph boundary.

Engine 1 is deliberately a deterministic contract boundary.  It validates the
package and the small amount of expanded technical-policy topology needed by
the lower engines, then copies the typed package values without authoring new
behavior.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from product4.capabilities.registry import (
    REGISTRY,
    REGISTRY_VERSION,
    registry_hash,
    validate_registry_field_value,
)
from product4.capabilities.technical_policy import (
    POLICY,
    TECHNICAL_POLICY_VERSION,
    policy_hash,
    policy_payload,
)
from product4.contracts.authoring_package.capabilities import CAPABILITY_PROFILE_VERSION
from product4.contracts.authoring_package.ledger_contracts import canonical_ledger_hash
from product4.contracts.package_boundary import (
    AUTHORING_PACKAGE_SCHEMA_VERSION,
    canonical_authoring_package_hash,
    validate_frozen_package,
)
from product4.contracts.trigger import (
    TRIGGER_METADATA_KEY,
    TriggerMetadataValidationStage,
    validate_trigger_metadata_payload,
)

NORMALIZED_GRAPH_SCHEMA_VERSION = "product4-normalized-graph-1.0"
_GENERATED_SOURCE_PREFIX = "Generated technical behavior: "
_POLICY_REFERENCE_PREFIX = f"policy:{TECHNICAL_POLICY_VERSION}:"
_EXPECTED_AUTHORED_CAPABILITIES = frozenset(
    {
        "send_text_message",
        "capture_user_input",
        "fixed_choice",
        "persist_contact_field",
        "end",
    }
)

if frozenset(REGISTRY) != _EXPECTED_AUTHORED_CAPABILITIES:
    raise RuntimeError("P4_E1_REGISTRY_PROFILE_DRIFT")

_CAPABILITY_BY_TYPE = {
    definition.engine1_type: definition.id for definition in REGISTRY.values()
}


class FrozenPackageBoundaryError(ValueError):
    """Raised when a frozen package cannot cross the Engine 1 boundary."""


class NormalizedNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    capability: str
    config: dict[str, Any]
    generated_policy: bool = False
    source_refs: list[dict[str, str]]


class NormalizedEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source_id: str
    target_id: str
    role: str
    condition: dict[str, Any] | None = None
    generated_policy: bool = False
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class NormalizedGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = NORMALIZED_GRAPH_SCHEMA_VERSION
    package_schema_version: str
    capability_profile_version: str
    source_package_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_version: str
    registry_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    technical_policy_version: str
    technical_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_units: list[dict[str, Any]]
    metadata: list[dict[str, Any]]
    nodes: list[NormalizedNode]
    edges: list[NormalizedEdge]

    @model_validator(mode="after")
    def references_resolve(self) -> "NormalizedGraph":
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("P4_E1_DUPLICATE_NODE")
        edge_ids = {edge.id for edge in self.edges}
        if len(edge_ids) != len(self.edges):
            raise ValueError("P4_E1_DUPLICATE_EDGE")
        if any(edge.source_id not in ids or edge.target_id not in ids for edge in self.edges):
            raise ValueError("P4_E1_DANGLING_EDGE")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def canonical_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reject(code: str) -> None:
    raise FrozenPackageBoundaryError(code)


def _source_hash(prose: str) -> str:
    return hashlib.sha256(prose.encode("utf-8")).hexdigest()


def _policy_provenance(values: list[Any], *, rule: str | None = None) -> bool:
    if not values:
        return False
    expected_reference = f"{_POLICY_REFERENCE_PREFIX}{rule}" if rule else None
    for item in values:
        if (
            item.source.value != "approved_versioned_policy"
            or item.policy_version != TECHNICAL_POLICY_VERSION
            or item.source_hash is not None
            or item.quote is not None
            or not item.reference.startswith(_POLICY_REFERENCE_PREFIX)
            or (expected_reference is not None and item.reference != expected_reference)
        ):
            return False
    return True


def _authored_provenance(values: list[Any], source_hash: str) -> bool:
    return bool(values) and all(
        item.source.value == "confirmed_prose"
        and item.source_hash == source_hash
        and item.policy_version is None
        for item in values
    )


def _generated_rule(node_payload: dict[str, Any]) -> str | None:
    refs = node_payload.get("source_refs") or []
    quotes = [ref.get("source_quote") for ref in refs]
    if not quotes or any(not isinstance(quote, str) for quote in quotes):
        return None
    if not all(quote.startswith(_GENERATED_SOURCE_PREFIX) for quote in quotes):
        if any(quote.startswith(_GENERATED_SOURCE_PREFIX) for quote in quotes):
            _reject("P4_E1_GENERATED_SOURCE_PROVENANCE_MIXED")
        return None
    rules = {
        quote.removeprefix(_GENERATED_SOURCE_PREFIX).removeprefix(_POLICY_REFERENCE_PREFIX)
        for quote in quotes
    }
    if len(rules) != 1 or next(iter(rules), "") not in {
        "bounded-invalid-response-retry",
        "input-retry-exhausted-terminal",
        "input-no-response-terminal",
        "choice-retry-exhausted-terminal",
        "choice-no-response-terminal",
        "persistence-failure-terminal",
    }:
        _reject("P4_E1_GENERATED_SOURCE_PROVENANCE_INVALID")
    return next(iter(rules))


def _metadata_by_key(package: Any, key: str) -> list[Any]:
    return [item for item in package.metadata if item.key == key]


def _validate_package_lineage(package: Any) -> None:
    if package.schema_version != AUTHORING_PACKAGE_SCHEMA_VERSION:
        _reject("P4_E1_PACKAGE_SCHEMA_MISMATCH")
    if package.capability_profile_version != CAPABILITY_PROFILE_VERSION:
        _reject("P4_E1_CAPABILITY_PROFILE_MISMATCH")

    source_hash = _source_hash(package.source.confirmed_prose)
    if package.source.source_hash != source_hash:
        _reject("P4_E1_SOURCE_HASH_MISMATCH")

    ledger = package.ledger.to_mutable_ledger()
    if ledger.source_hash != source_hash:
        _reject("P4_E1_LEDGER_SOURCE_HASH_MISMATCH")
    if ledger.frozen_hash != canonical_ledger_hash(ledger):
        _reject("P4_E1_LEDGER_HASH_MISMATCH")
    confirmation = ledger.confirmation
    if (
        ledger.status.value != "frozen"
        or confirmation is None
        or confirmation.status.value != "confirmed"
        or confirmation.ledger_hash != ledger.frozen_hash
        or confirmation.source_hash != source_hash
    ):
        _reject("P4_E1_FROZEN_CONFIRMATION_INVALID")


def _validate_policy_metadata(package: Any) -> None:
    entries = [
        item
        for item in package.metadata
        if item.type.value == "policy" and item.key == "product4.technical-policy"
    ]
    if len(entries) != 1:
        _reject("P4_E1_POLICY_METADATA_MISSING")
    entry = entries[0]
    expected = {**policy_payload(), "canonical_hash": policy_hash()}
    if _canonical_json(entry.value) != _canonical_json(expected):
        _reject("P4_E1_POLICY_METADATA_MISMATCH")
    if not _policy_provenance(entry.provenance, rule="registry"):
        _reject("P4_E1_POLICY_PROVENANCE_MISMATCH")


def _validate_trigger_metadata(package: Any) -> None:
    entries = _metadata_by_key(package, TRIGGER_METADATA_KEY)
    if not entries:
        return
    if len(entries) != 1:
        _reject("P4_E1_TRIGGER_METADATA_COUNT_INVALID")
    entry = entries[0]
    if entry.type.value != "custom":
        _reject("P4_E1_TRIGGER_METADATA_TYPE_INVALID")
    try:
        validate_trigger_metadata_payload(
            entry.value,
            entry.provenance,
            source_hash=package.source.source_hash,
            stage=TriggerMetadataValidationStage.FROZEN_PACKAGE,
            confirmed_prose=package.source.confirmed_prose,
        )
    except (TypeError, ValueError) as exc:
        _reject(f"P4_E1_TRIGGER_METADATA_INVALID:{exc}")


def _validate_coverage_and_sources(package: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source_units = {unit.id: unit for unit in package.source.source_units}
    nodes = {node.id: node for node in package.nodes}
    edges = {edge.id: edge for edge in package.edges}
    requirements = {item.id: item for item in package.ledger.requirements}

    coverage_by_source: dict[str, Any] = {}
    node_coverage: dict[str, Any] = {}
    edge_coverage: dict[str, Any] = {}
    requirement_coverage: dict[str, Any] = {}
    for entry in package.source_coverage:
        if entry.source_unit_id in coverage_by_source:
            _reject("P4_E1_DUPLICATE_COVERAGE")
        coverage_by_source[entry.source_unit_id] = entry
        for node_id in entry.node_ids:
            if node_id in node_coverage:
                _reject("P4_E1_DUPLICATE_NODE_COVERAGE")
            if node_id not in nodes:
                _reject("P4_E1_COVERAGE_NODE_REFERENCE")
            node_coverage[node_id] = entry
        for edge_id in entry.edge_ids:
            if edge_id in edge_coverage:
                _reject("P4_E1_DUPLICATE_EDGE_COVERAGE")
            if edge_id not in edges:
                _reject("P4_E1_COVERAGE_EDGE_REFERENCE")
            edge_coverage[edge_id] = entry
        for requirement_id in entry.requirement_ids:
            if requirement_id in requirement_coverage:
                _reject("P4_E1_DUPLICATE_REQUIREMENT_COVERAGE")
            if requirement_id not in requirements:
                _reject("P4_E1_COVERAGE_REQUIREMENT_REFERENCE")
            requirement_coverage[requirement_id] = entry

    if set(coverage_by_source) != set(source_units):
        _reject("P4_E1_SOURCE_COVERAGE_INCOMPLETE")
    if set(node_coverage) != set(nodes):
        _reject("P4_E1_NODE_COVERAGE_INCOMPLETE")
    if set(edge_coverage) != set(edges):
        _reject("P4_E1_EDGE_COVERAGE_INCOMPLETE")
    if set(requirement_coverage) != set(requirements):
        _reject("P4_E1_REQUIREMENT_COVERAGE_INCOMPLETE")

    for coverage in node_coverage.values():
        if len(coverage.node_ids) != 1 or len(coverage.requirement_ids) != 1:
            _reject("P4_E1_NODE_REQUIREMENT_BINDING_INVALID")

    requirement_for_node = {
        node_id: requirements[coverage.requirement_ids[0]]
        for node_id, coverage in node_coverage.items()
    }

    for node in package.nodes:
        node_payload = node.model_dump(mode="json")
        generated_rule = _generated_rule(node_payload)
        for source_ref in node.source_refs:
            unit = source_units.get(source_ref.source_unit_id)
            if unit is None or source_ref.source_quote != unit.text:
                _reject("P4_E1_SOURCE_REFERENCE_INVALID")
            if generated_rule is None and source_ref.source_quote not in package.source.confirmed_prose:
                _reject("P4_E1_SOURCE_QUOTE_NOT_IN_CONFIRMED_PROSE")
            if generated_rule is not None and not source_ref.source_quote.startswith(
                f"{_GENERATED_SOURCE_PREFIX}{_POLICY_REFERENCE_PREFIX}"
            ):
                _reject("P4_E1_GENERATED_SOURCE_REFERENCE_INVALID")

        coverage = node_coverage[node.id]
        if len(coverage.node_ids) != 1 or len(coverage.requirement_ids) != 1:
            _reject("P4_E1_NODE_REQUIREMENT_BINDING_INVALID")
        requirement = requirements[coverage.requirement_ids[0]]
        expected_capability = (
            "retry_policy"
            if node_payload["type"] == "retry_policy"
            else _CAPABILITY_BY_TYPE.get(node_payload["type"])
        )
        if expected_capability is None:
            _reject(f"P4_E1_UNSUPPORTED_NODE_TYPE:{node_payload['type']}")
        if requirement.capability != expected_capability:
            _reject("P4_E1_REQUIREMENT_CAPABILITY_MISMATCH")
        if _canonical_json(_requirement_payload_for_node(node_payload, requirement_for_node)) != _canonical_json(
            _model_dump(requirement.payload)
        ):
            _reject("P4_E1_REQUIREMENT_PAYLOAD_MISMATCH")
        if generated_rule is not None:
            if node_payload["type"] not in {"send_message", "end", "retry_policy"}:
                _reject("P4_E1_GENERATED_NODE_TYPE_INVALID")
            if not _policy_provenance(requirement.provenance, rule=generated_rule):
                _reject("P4_E1_GENERATED_NODE_PROVENANCE_MISMATCH")
        elif not _authored_provenance(requirement.provenance, package.source.source_hash):
            _reject("P4_E1_AUTHORED_NODE_PROVENANCE_MISMATCH")

    ledger_requirements = {item.id: item for item in package.ledger.requirements}
    ledger_decisions = {item.id: item for item in package.ledger.decisions}
    for requirement in ledger_requirements.values():
        for decision_id in requirement.decision_ids:
            decision = ledger_decisions.get(decision_id)
            if decision is None or requirement.id not in decision.requirement_ids:
                _reject("P4_E1_LEDGER_LINK_MISMATCH")
    for decision in ledger_decisions.values():
        for requirement_id in decision.requirement_ids:
            requirement = ledger_requirements.get(requirement_id)
            if requirement is None or decision.id not in requirement.decision_ids:
                _reject("P4_E1_LEDGER_LINK_MISMATCH")

    return nodes, edges, requirements


def _model_dump(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else value


def _requirement_payload_for_node(node_payload: dict[str, Any], requirements_by_node: dict[str, Any]) -> dict[str, Any]:
    node_id = node_payload["id"]
    node_type = node_payload["type"]
    next_requirement = lambda target: requirements_by_node[target].id
    if node_type == "send_message":
        return {
            "copy": node_payload["copy"],
            "locale": node_payload["locale"],
            "next_requirement_id": next_requirement(node_payload["next_node_id"]),
        }
    if node_type == "capture_input":
        retry = node_payload["retry"]
        no_response = node_payload["no_response"]
        return {
            "prompt": node_payload["prompt"],
            "input_type": node_payload["input_type"],
            "save_as": node_payload["save_as"],
            "required": node_payload["required"],
            "validation": node_payload["validation"],
            "next_requirement_id": next_requirement(node_payload["next_node_id"]),
            "retry": {
                "max_attempts": retry["max_attempts"],
                "messages": retry["messages"],
                "on_exhausted_requirement_id": next_requirement(retry["on_exhausted_node_id"]),
            },
            "no_response": {
                "timeout_seconds": no_response["timeout_seconds"],
                "next_requirement_id": next_requirement(no_response["next_node_id"]),
            },
        }
    if node_type == "fixed_choice":
        return {
            "title": node_payload["title"],
            "outcomes": [
                {
                    "label": item["label"],
                    "value": item["value"],
                    "next_requirement_id": next_requirement(item["next_node_id"]),
                }
                for item in node_payload["outcomes"]
            ],
            "stable_values": node_payload["stable_values"],
            "default_requirement_id": next_requirement(node_payload["default_node_id"]),
            "invalid_requirement_id": next_requirement(node_payload["invalid_node_id"]),
            "timeout_requirement_id": next_requirement(node_payload["timeout_node_id"]),
        }
    if node_type == "persist_contact_field":
        return {
            "source_variable": node_payload["source_variable"],
            "field_name": node_payload["field_name"],
            "success_requirement_id": next_requirement(node_payload["success_node_id"]),
            "failure_requirement_id": next_requirement(node_payload["failure_node_id"]),
        }
    if node_type == "retry_policy":
        return {
            "max_attempts": node_payload["max_attempts"],
            "messages": node_payload["messages"],
            "on_exhausted_requirement_id": next_requirement(node_payload["on_exhausted_node_id"]),
        }
    if node_type == "end":
        return {"reason": node_payload["reason"]}
    _reject(f"P4_E1_UNSUPPORTED_NODE_TYPE:{node_type}")


def _validate_registry_config(node_payload: dict[str, Any], capability: str) -> None:
    definition = REGISTRY[capability]
    for field in definition.fields:
        source_key = "outcomes" if capability == "fixed_choice" and field.path == "options" else field.path
        if source_key not in node_payload:
            _reject(f"P4_E1_REGISTRY_FIELD_MISSING:{capability}.{field.path}")
        value = node_payload[source_key]
        if capability == "fixed_choice" and field.path == "options":
            value = [
                {"label": item["label"], "value": item["value"]}
                for item in value
            ]
        try:
            validate_registry_field_value(field, value)
        except Exception as exc:
            raise FrozenPackageBoundaryError(
                f"P4_E1_REGISTRY_FIELD_INVALID:{capability}.{field.path}"
            ) from exc


def _edge_payload(edge: Any) -> dict[str, Any]:
    return edge.model_dump(mode="json")


def _edge_rule(edge: Any) -> str | None:
    if not _policy_provenance(edge.provenance):
        return None
    references = {item.reference for item in edge.provenance}
    if len(references) != 1:
        _reject("P4_E1_POLICY_PROVENANCE_MISMATCH")
    return next(iter(references)).removeprefix(_POLICY_REFERENCE_PREFIX)


def _require_exact_edges(
    outgoing: dict[str, list[Any]],
    node_id: str,
    role: str,
    *,
    count: int,
) -> list[Any]:
    matches = [edge for edge in outgoing[node_id] if edge.role == role]
    if len(matches) != count:
        _reject(f"P4_E1_TOPOLOGY_ROLE_COUNT:{node_id}:{role}")
    return matches


def _validate_authored_tree(
    nodes: dict[str, Any],
    edges: dict[str, Any],
    generated_ids: set[str],
    outgoing: dict[str, list[Any]],
) -> str:
    authored_ids = set(nodes) - generated_ids
    # The package contract already validates edge endpoints.  Here, user edges
    # must remain entirely within the authored tree; policy edges cannot carry
    # business routing between authored nodes.
    authored_edges = [
        edge
        for edge in edges.values()
        if _edge_rule(edge) is None
    ]
    if any(edge.source_id not in authored_ids or edge.target_id not in authored_ids for edge in authored_edges):
        _reject("P4_E1_AUTHORED_EDGE_CROSSES_POLICY_GRAPH")

    incoming: dict[str, list[Any]] = defaultdict(list)
    authored_outgoing: dict[str, list[Any]] = defaultdict(list)
    for edge in authored_edges:
        incoming[edge.target_id].append(edge)
        authored_outgoing[edge.source_id].append(edge)
    roots = [node_id for node_id in authored_ids if not incoming[node_id]]
    if len(roots) != 1:
        _reject("P4_E1_AUTHORED_ROOT_COUNT_INVALID")
    root = roots[0]
    if any(len(incoming[node_id]) > 1 for node_id in authored_ids):
        _reject("P4_E1_AUTHORED_JOIN_UNSUPPORTED")

    seen: set[str] = set()
    queue = deque([root])
    while queue:
        node_id = queue.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)
        queue.extend(edge.target_id for edge in authored_outgoing[node_id])
    if seen != authored_ids:
        _reject("P4_E1_AUTHORED_NODE_UNREACHABLE")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            _reject("P4_E1_AUTHORED_CYCLE")
        if node_id in visited:
            return
        visiting.add(node_id)
        for edge in authored_outgoing[node_id]:
            visit(edge.target_id)
        visiting.remove(node_id)
        visited.add(node_id)

    visit(root)

    for node_id in authored_ids:
        node_payload = nodes[node_id].model_dump(mode="json")
        node_type = node_payload["type"]
        node_edges = authored_outgoing[node_id]
        if node_type == "end":
            if node_edges:
                _reject("P4_E1_AUTHORED_END_HAS_OUTGOING")
            continue
        if node_type == "fixed_choice":
            outcomes = node_payload["outcomes"]
            outcome_edges = _require_exact_edges(outgoing, node_id, "outcome", count=len(outcomes))
            if len(node_edges) != len(outcomes):
                _reject("P4_E1_AUTHORED_CHOICE_EXIT_MISMATCH")
            expected = {
                (item["value"], item["label"], item["next_node_id"])
                for item in outcomes
            }
            actual = {
                (
                    edge.condition.stable_value,
                    edge.condition.title,
                    edge.target_id,
                )
                for edge in outcome_edges
            }
            if actual != expected:
                _reject("P4_E1_AUTHORED_CHOICE_EXIT_MISMATCH")
        elif node_type in {"send_message", "capture_input"}:
            if len(node_edges) != 1 or node_edges[0].role != "next":
                _reject("P4_E1_AUTHORED_LINEAR_EXIT_INVALID")
            expected_target = node_payload["next_node_id"]
            if node_edges[0].target_id != expected_target:
                _reject("P4_E1_AUTHORED_ROUTE_MISMATCH")
        elif node_type == "persist_contact_field":
            if len(node_edges) != 1 or node_edges[0].role != "success":
                _reject("P4_E1_AUTHORED_PERSISTENCE_EXIT_INVALID")
            if node_edges[0].target_id != node_payload["success_node_id"]:
                _reject("P4_E1_AUTHORED_ROUTE_MISMATCH")
        else:
            _reject(f"P4_E1_UNSUPPORTED_NODE_TYPE:{node_type}")
    return root


def _validate_policy_edge(
    edge: Any,
    source_type: str,
    role: str,
    source_rule: str | None = None,
) -> None:
    expected_rules = {
        ("capture_input", "exhausted"): "input-retry-exhausted-terminal",
        ("capture_input", "timeout"): "input-no-response-terminal",
        ("fixed_choice", "default"): "bounded-invalid-response-retry",
        ("fixed_choice", "invalid"): "bounded-invalid-response-retry",
        ("fixed_choice", "timeout"): "choice-no-response-terminal",
        ("retry_policy", "retry"): "bounded-invalid-response-retry",
        ("retry_policy", "exhausted"): "bounded-invalid-response-retry",
        ("send_message", "next"): source_rule,
        ("persist_contact_field", "failure"): "persistence-failure-terminal",
    }
    expected = expected_rules.get((source_type, role))
    if expected is None or _edge_rule(edge) != expected:
        _reject("P4_E1_POLICY_EDGE_PROVENANCE_MISMATCH")
    condition = edge.condition.model_dump(mode="json") if edge.condition else None
    expected_condition = (
        {"type": "timeout", "seconds": POLICY.no_response_timeout_seconds}
        if role == "timeout"
        else {"type": role}
    )
    if condition != expected_condition:
        _reject("P4_E1_POLICY_EDGE_CONDITION_MISMATCH")


def _require_generated_target_rule(nodes: dict[str, Any], target_id: str, rule: str) -> None:
    target = nodes.get(target_id)
    if target is None or _generated_rule(target.model_dump(mode="json")) != rule:
        _reject("P4_E1_GENERATED_POLICY_TARGET_MISMATCH")


def _validate_expanded_policy_topology(
    nodes: dict[str, Any],
    edges: dict[str, Any],
    generated_ids: set[str],
    outgoing: dict[str, list[Any]],
) -> str:
    for edge in edges.values():
        policy_edge = _edge_rule(edge) is not None
        touches_generated = edge.source_id in generated_ids or edge.target_id in generated_ids
        if policy_edge != touches_generated:
            _reject("P4_E1_EDGE_GENERATED_MARKING_MISMATCH")
        if policy_edge:
            source_payload = nodes[edge.source_id].model_dump(mode="json")
            _validate_policy_edge(
                edge,
                nodes[edge.source_id].type,
                edge.role,
                _generated_rule(source_payload),
            )

    for node_id in generated_ids:
        node_payload = nodes[node_id].model_dump(mode="json")
        node_type = node_payload["type"]
        rule = _generated_rule(node_payload)
        if rule is None:
            _reject("P4_E1_GENERATED_NODE_PROVENANCE_MISSING")
        if node_type == "retry_policy":
            if node_payload["max_attempts"] != POLICY.retry.max_attempts:
                _reject("P4_E1_RETRY_POLICY_NOT_BOUNDED")
            if node_payload["messages"] != list(POLICY.retry.messages):
                _reject("P4_E1_RETRY_POLICY_MESSAGE_MISMATCH")
            retry_edges = _require_exact_edges(outgoing, node_id, "retry", count=1)
            exhausted_edges = _require_exact_edges(outgoing, node_id, "exhausted", count=1)
            if nodes[retry_edges[0].target_id].type != "fixed_choice":
                _reject("P4_E1_RETRY_RETURN_TARGET_INVALID")
            if exhausted_edges[0].target_id != node_payload["on_exhausted_node_id"]:
                _reject("P4_E1_RETRY_POLICY_ROUTE_MISMATCH")
            _require_generated_target_rule(
                nodes,
                exhausted_edges[0].target_id,
                "choice-retry-exhausted-terminal",
            )
        elif node_type == "send_message":
            if rule not in {
                "input-retry-exhausted-terminal",
                "choice-retry-exhausted-terminal",
            }:
                _reject("P4_E1_GENERATED_MESSAGE_RULE_INVALID")
            if (
                node_payload["copy"] != POLICY.retry_exhausted_message
                or node_payload["locale"] != "en"
            ):
                _reject("P4_E1_RETRY_EXHAUSTED_MESSAGE_MISMATCH")
            next_edges = _require_exact_edges(outgoing, node_id, "next", count=1)
            if next_edges[0].target_id != node_payload["next_node_id"]:
                _reject("P4_E1_RETRY_EXHAUSTED_MESSAGE_ROUTE_MISMATCH")
            if nodes[next_edges[0].target_id].type != "end":
                _reject("P4_E1_RETRY_EXHAUSTED_TERMINAL_MISSING")
            _require_generated_target_rule(nodes, next_edges[0].target_id, rule)
        elif node_type == "end":
            if outgoing[node_id]:
                _reject("P4_E1_GENERATED_END_HAS_OUTGOING")
            expected_reason = {
                "input-retry-exhausted-terminal": POLICY.retry_exhausted_reason,
                "choice-retry-exhausted-terminal": POLICY.retry_exhausted_reason,
                "input-no-response-terminal": POLICY.no_response_reason,
                "choice-no-response-terminal": POLICY.no_response_reason,
                "persistence-failure-terminal": POLICY.persistence_failure_reason,
            }[rule]
            if node_payload["reason"] != expected_reason:
                _reject("P4_E1_GENERATED_TERMINAL_REASON_MISMATCH")
        else:
            _reject(f"P4_E1_UNSUPPORTED_GENERATED_NODE:{node_type}")

    for node in nodes.values():
        node_payload = node.model_dump(mode="json")
        node_type = node_payload["type"]
        if node.id in generated_ids:
            continue
        if node_type == "capture_input":
            retry = node_payload.get("retry")
            no_response = node_payload.get("no_response")
            if not retry or retry["max_attempts"] != POLICY.retry.max_attempts or retry["messages"] != list(POLICY.retry.messages):
                _reject("P4_E1_INPUT_RETRY_POLICY_MISMATCH")
            if not no_response or no_response["timeout_seconds"] != POLICY.no_response_timeout_seconds:
                _reject("P4_E1_INPUT_TIMEOUT_POLICY_MISMATCH")
            exhausted = _require_exact_edges(outgoing, node.id, "exhausted", count=1)[0]
            timeout = _require_exact_edges(outgoing, node.id, "timeout", count=1)[0]
            if exhausted.target_id != retry["on_exhausted_node_id"] or timeout.target_id != no_response["next_node_id"]:
                _reject("P4_E1_INPUT_POLICY_ROUTE_MISMATCH")
            if nodes[exhausted.target_id].type != "send_message" or nodes[timeout.target_id].type != "end":
                _reject("P4_E1_INPUT_POLICY_TERMINAL_MISSING")
            _require_generated_target_rule(
                nodes,
                exhausted.target_id,
                "input-retry-exhausted-terminal",
            )
            _require_generated_target_rule(nodes, timeout.target_id, "input-no-response-terminal")
        elif node_type == "fixed_choice":
            retry_node_id = node_payload["default_node_id"]
            if node_payload["invalid_node_id"] != retry_node_id:
                _reject("P4_E1_CHOICE_RETRY_TARGET_MISMATCH")
            retry_node = nodes.get(retry_node_id)
            if retry_node is None or retry_node.id not in generated_ids or retry_node.type != "retry_policy":
                _reject("P4_E1_CHOICE_RETRY_POLICY_MISSING")
            retry_return = _require_exact_edges(outgoing, retry_node_id, "retry", count=1)[0]
            if retry_return.target_id != node.id:
                _reject("P4_E1_RETRY_RETURN_TARGET_INVALID")
            default = _require_exact_edges(outgoing, node.id, "default", count=1)[0]
            invalid = _require_exact_edges(outgoing, node.id, "invalid", count=1)[0]
            timeout = _require_exact_edges(outgoing, node.id, "timeout", count=1)[0]
            if default.target_id != retry_node_id or invalid.target_id != retry_node_id:
                _reject("P4_E1_CHOICE_RETRY_ROUTE_MISMATCH")
            if timeout.target_id != node_payload["timeout_node_id"] or nodes[timeout.target_id].type != "end":
                _reject("P4_E1_CHOICE_TIMEOUT_ROUTE_MISMATCH")
            _require_generated_target_rule(nodes, retry_node_id, "bounded-invalid-response-retry")
            _require_generated_target_rule(nodes, timeout.target_id, "choice-no-response-terminal")
        elif node_type == "persist_contact_field":
            failure = _require_exact_edges(outgoing, node.id, "failure", count=1)[0]
            if failure.target_id != node_payload["failure_node_id"] or failure.target_id not in generated_ids:
                _reject("P4_E1_PERSISTENCE_FAILURE_ROUTE_MISMATCH")
            if nodes[failure.target_id].type != "end":
                _reject("P4_E1_PERSISTENCE_FAILURE_TERMINAL_MISSING")
            _require_generated_target_rule(nodes, failure.target_id, "persistence-failure-terminal")
        elif node_type == "send_message" or node_type == "end":
            if any(_edge_rule(edge) is not None for edge in outgoing[node.id]):
                _reject("P4_E1_UNEXPECTED_POLICY_ROUTE")
        else:
            _reject(f"P4_E1_UNSUPPORTED_NODE_TYPE:{node_type}")

    # Remove only the registered choice↔retry cycle.  Any remaining cycle is
    # an unapproved topology, including a user-authored loop or a policy route
    # that was relabeled to another role.
    allowed_cycle_edges = {
        edge.id
        for edge in edges.values()
        if (
            (nodes[edge.source_id].type == "fixed_choice" and edge.role in {"default", "invalid"})
            or (nodes[edge.source_id].type == "retry_policy" and edge.role == "retry")
        )
    }
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges.values():
        if edge.id not in allowed_cycle_edges:
            adjacency[edge.source_id].append(edge.target_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            _reject("P4_E1_UNREGISTERED_POLICY_CYCLE")
        if node_id in visited:
            return
        visiting.add(node_id)
        for target_id in adjacency[node_id]:
            visit(target_id)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in nodes:
        visit(node_id)

    return _validate_full_reachability(nodes, edges, outgoing)


def _validate_full_reachability(nodes: dict[str, Any], edges: dict[str, Any], outgoing: dict[str, list[Any]]) -> str:
    authored_ids = {node_id for node_id, node in nodes.items() if _generated_rule(node.model_dump(mode="json")) is None}
    roots = [
        node_id
        for node_id in authored_ids
        if not any(edge.source_id in authored_ids for edge in edges.values() if edge.target_id == node_id)
    ]
    if len(roots) != 1:
        _reject("P4_E1_FULL_GRAPH_ROOT_INVALID")
    root = roots[0]
    seen: set[str] = set()
    queue = deque([root])
    while queue:
        node_id = queue.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)
        queue.extend(edge.target_id for edge in outgoing[node_id])
    if seen != set(nodes):
        _reject("P4_E1_FULL_GRAPH_UNREACHABLE")
    return root


def _validate_edge_provenance(package: Any, edges: dict[str, Any], generated_ids: set[str]) -> None:
    source_hash = package.source.source_hash
    for edge in edges.values():
        policy = _edge_rule(edge) is not None
        touches_generated = edge.source_id in generated_ids or edge.target_id in generated_ids
        if policy != touches_generated:
            _reject("P4_E1_EDGE_GENERATED_MARKING_MISMATCH")
        if policy:
            if not _policy_provenance(edge.provenance):
                _reject("P4_E1_POLICY_EDGE_PROVENANCE_MISMATCH")
        elif not _authored_provenance(edge.provenance, source_hash):
            _reject("P4_E1_AUTHORED_EDGE_PROVENANCE_MISMATCH")


def ingest_frozen_package(package_value: dict[str, Any], declared_hash: str) -> NormalizedGraph:
    """Validate and normalize one frozen package without a model/provider client."""

    try:
        package = validate_frozen_package(package_value)
    except Exception as exc:
        raise FrozenPackageBoundaryError("P4_E1_PACKAGE_CONTRACT_INVALID") from exc

    actual_hash = canonical_authoring_package_hash(package)
    if declared_hash != actual_hash:
        _reject("P4_E1_PACKAGE_HASH_MISMATCH")

    _validate_package_lineage(package)
    _validate_policy_metadata(package)
    _validate_trigger_metadata(package)
    nodes, edges, _ = _validate_coverage_and_sources(package)

    node_payloads = {node_id: node.model_dump(mode="json") for node_id, node in nodes.items()}
    generated_ids: set[str] = set()
    for node_id, payload in node_payloads.items():
        capability = (
            "retry_policy"
            if payload["type"] == "retry_policy"
            else _CAPABILITY_BY_TYPE.get(payload["type"])
        )
        if capability is None:
            _reject(f"P4_E1_UNSUPPORTED_NODE_TYPE:{payload['type']}")
        generated_rule = _generated_rule(payload)
        if generated_rule is not None:
            generated_ids.add(node_id)
            if payload["type"] not in {"send_message", "end", "retry_policy"}:
                _reject("P4_E1_GENERATED_NODE_TYPE_INVALID")
        elif payload["type"] == "retry_policy":
            _reject("P4_E1_RETRY_POLICY_MUST_BE_GENERATED")
        elif capability in _EXPECTED_AUTHORED_CAPABILITIES:
            _validate_registry_config(payload, capability)

    outgoing: dict[str, list[Any]] = defaultdict(list)
    for edge in edges.values():
        outgoing[edge.source_id].append(edge)
    for values in outgoing.values():
        values.sort(key=lambda edge: edge.id)

    _validate_edge_provenance(package, edges, generated_ids)
    _validate_authored_tree(nodes, edges, generated_ids, outgoing)
    _validate_expanded_policy_topology(nodes, edges, generated_ids, outgoing)

    normalized_nodes: list[NormalizedNode] = []
    for node_id, node in nodes.items():
        payload = node_payloads[node_id]
        node_type = payload.pop("type")
        normalized_id = payload.pop("id")
        source_refs = payload.pop("source_refs")
        payload.pop("label", None)
        capability = "retry_policy" if node_type == "retry_policy" else _CAPABILITY_BY_TYPE[node_type]
        normalized_nodes.append(
            NormalizedNode(
                id=normalized_id,
                capability=capability,
                config=payload,
                generated_policy=node_id in generated_ids,
                source_refs=source_refs,
            )
        )

    normalized_edges = [
        NormalizedEdge(
            id=edge.id,
            source_id=edge.source_id,
            target_id=edge.target_id,
            role=edge.role,
            condition=(edge.condition.model_dump(mode="json") if edge.condition else None),
            generated_policy=edge.id in {
                candidate.id for candidate in edges.values() if _edge_rule(candidate) is not None
            },
            provenance=[item.model_dump(mode="json") for item in edge.provenance],
        )
        for edge in edges.values()
    ]

    title_entries = _metadata_by_key(package, "product4.flow-title")
    if title_entries and not isinstance(title_entries[0].value, str):
        _reject("P4_E1_FLOW_TITLE_INVALID")
    title = title_entries[0].value if title_entries else "Product 4 flow"

    return NormalizedGraph(
        package_schema_version=package.schema_version,
        capability_profile_version=package.capability_profile_version,
        source_package_hash=actual_hash,
        registry_version=REGISTRY_VERSION,
        registry_hash=registry_hash(),
        technical_policy_version=TECHNICAL_POLICY_VERSION,
        technical_policy_hash=policy_hash(),
        title=title,
        source_hash=package.source.source_hash,
        source_units=sorted(
            (unit.model_dump(mode="json") for unit in package.source.source_units),
            key=lambda item: item["id"],
        ),
        metadata=sorted(
            (item.model_dump(mode="json") for item in package.metadata),
            key=lambda item: (item["type"], item["key"]),
        ),
        nodes=sorted(normalized_nodes, key=lambda item: item.id),
        edges=sorted(normalized_edges, key=lambda item: item.id),
    )
