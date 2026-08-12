from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from product4.capabilities.technical_policy import (
    POLICY,
    TECHNICAL_POLICY_VERSION,
    policy_hash,
    policy_payload,
    policy_reference,
)
from product4.contracts.authoring_package.ledger_merge import (
    CombinedRequirementsLedger,
    confirm_ledger,
    freeze_ledger,
)
from product4.contracts.package_boundary import validate_frozen_package
from product4.contracts.session import AuthoringSession, DraftEdge, DraftNode
from product4.contracts.trigger import (
    TRIGGER_METADATA_KEY,
    trigger_provenance_reference,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _user_provenance(reference: str, source_hash: str, quote: str) -> dict[str, Any]:
    return {
        "source": "confirmed_prose", "reference": reference, "quote": quote,
        "source_hash": source_hash, "policy_version": None,
    }


def _policy_provenance(rule: str) -> dict[str, Any]:
    return {
        "source": "approved_versioned_policy", "reference": policy_reference(rule),
        "quote": None, "source_hash": None, "policy_version": TECHNICAL_POLICY_VERSION,
    }


def validate_draft(session: AuthoringSession) -> None:
    issues: list[str] = []
    if session.open_positions:
        issues.append("P4_OPEN_POSITIONS")
    if not session.nodes:
        issues.append("P4_EMPTY_GRAPH")
    if session.original_brief:
        for node in session.nodes:
            if not node.source_excerpt or node.source_excerpt not in session.original_brief:
                issues.append("P4_SOURCE_EXCERPT_NOT_IN_ORIGINAL_BRIEF")
    ids = {node.id for node in session.nodes}
    if len(ids) != len(session.nodes):
        issues.append("P4_DUPLICATE_NODE")
    outgoing: dict[str, list[DraftEdge]] = defaultdict(list)
    incoming: dict[str, list[DraftEdge]] = defaultdict(list)
    for edge in session.edges:
        if edge.source_id not in ids or edge.target_id not in ids:
            issues.append("P4_DANGLING_EDGE")
        outgoing[edge.source_id].append(edge)
        incoming[edge.target_id].append(edge)
    roots = [node.id for node in session.nodes if not incoming[node.id]]
    if len(roots) != 1:
        issues.append("P4_ROOT_COUNT_INVALID")
    if roots:
        seen: set[str] = set()
        queue = deque(roots)
        while queue:
            node_id = queue.popleft()
            if node_id in seen:
                issues.append("P4_GRAPH_NOT_TREE")
                continue
            seen.add(node_id)
            queue.extend(edge.target_id for edge in outgoing[node_id])
        if seen != ids:
            issues.append("P4_UNREACHABLE_NODE")
    for node in session.nodes:
        edges = outgoing[node.id]
        if node.capability == "end" and edges:
            issues.append("P4_END_HAS_OUTGOING")
        elif node.capability == "fixed_choice":
            expected = {item["value"] for item in node.config["options"]}
            if {edge.stable_value for edge in edges} != expected:
                issues.append("P4_CHOICE_EXIT_MISMATCH")
        elif node.capability != "end" and len(edges) != 1:
            issues.append("P4_LINEAR_EXIT_INVALID")
    if issues:
        raise ValueError(";".join(sorted(set(issues))))


@dataclass(frozen=True)
class ExpandedPackageGraph:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    requirements: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    source_units: list[dict[str, Any]]
    coverage: list[dict[str, Any]]
    metadata: list[dict[str, Any]]


class _Expansion:
    def __init__(self, session: AuthoringSession, source_hash: str):
        self.session = session
        self.source_hash = source_hash
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.requirements: list[dict[str, Any]] = []
        self.source_units: list[dict[str, Any]] = []
        self.coverage: list[dict[str, Any]] = []
        self.outgoing: dict[str, list[DraftEdge]] = defaultdict(list)
        for edge in session.edges:
            self.outgoing[edge.source_id].append(edge)
        for edges in self.outgoing.values():
            edges.sort(key=lambda item: item.id)
        self.req_by_node = {node.id: f"REQ-{index:03d}" for index, node in enumerate(session.nodes, 1)}
        self.next_node_number = len(session.nodes) + 1
        self.next_edge_number = len(session.edges) + 1
        self.next_req_number = len(session.nodes) + 1
        self.next_source_number = len(session.nodes) + 1

    def _policy_unit(self, rule: str) -> tuple[str, str]:
        unit_id = f"S{self.next_source_number:03d}"
        self.next_source_number += 1
        text = f"Generated technical behavior: {policy_reference(rule)}"
        self.source_units.append({"id": unit_id, "text": text, "source_hash": None})
        return unit_id, text

    def _generated_node_id(self) -> str:
        result = f"N{self.next_node_number:03d}"
        self.next_node_number += 1
        return result

    def _generated_requirement_id(self) -> str:
        result = f"REQ-{self.next_req_number:03d}"
        self.next_req_number += 1
        return result

    def _generated_edge_id(self) -> str:
        result = f"E{self.next_edge_number:03d}"
        self.next_edge_number += 1
        return result

    def add_policy_end(self, rule: str, reason: str) -> tuple[str, str]:
        node_id = self._generated_node_id()
        requirement_id = self._generated_requirement_id()
        unit_id, quote = self._policy_unit(rule)
        source_ref = {"source_unit_id": unit_id, "source_quote": quote}
        self.nodes.append({"id": node_id, "type": "end", "label": None, "reason": reason, "source_refs": [source_ref]})
        self.requirements.append({
            "id": requirement_id, "capability": "end", "summary": reason,
            "status": "proposed", "provenance": [_policy_provenance(rule)],
            "payload": {"reason": reason}, "decision_ids": [], "depends_on": [], "cross_references": [],
        })
        self.coverage.append({"source_unit_id": unit_id, "requirement_ids": [requirement_id], "node_ids": [node_id], "edge_ids": []})
        return node_id, requirement_id

    def add_policy_retry(
        self,
        *,
        originating_choice_id: str,
        exhausted_node_id: str,
        exhausted_req_id: str,
    ) -> tuple[str, str, tuple[str, str]]:
        rule = "bounded-invalid-response-retry"
        node_id = self._generated_node_id()
        requirement_id = self._generated_requirement_id()
        unit_id, quote = self._policy_unit(rule)
        source_ref = {"source_unit_id": unit_id, "source_quote": quote}
        self.nodes.append({
            "id": node_id, "type": "retry_policy", "label": None,
            "max_attempts": POLICY.retry.max_attempts, "messages": list(POLICY.retry.messages),
            "on_exhausted_node_id": exhausted_node_id, "source_refs": [source_ref],
        })
        self.requirements.append({
            "id": requirement_id, "capability": "retry_policy",
            "summary": "Apply the registered bounded invalid-response retry policy.",
            "status": "proposed", "provenance": [_policy_provenance(rule)],
            "payload": {
                "max_attempts": POLICY.retry.max_attempts,
                "messages": list(POLICY.retry.messages),
                "on_exhausted_requirement_id": exhausted_req_id,
            },
            "decision_ids": [], "depends_on": [], "cross_references": [],
        })
        return_edge_id = self.add_policy_edge(
            node_id,
            originating_choice_id,
            "retry",
            {"type": "retry"},
            rule,
        )
        exhausted_edge_id = self.add_policy_edge(
            node_id, exhausted_node_id, "exhausted", {"type": "exhausted"}, rule,
        )
        self.coverage.append({
            "source_unit_id": unit_id,
            "requirement_ids": [requirement_id],
            "node_ids": [node_id],
            "edge_ids": [return_edge_id, exhausted_edge_id],
        })
        return node_id, requirement_id, (return_edge_id, exhausted_edge_id)

    def add_policy_edge(self, source: str, target: str, role: str, condition: dict[str, Any] | None, rule: str) -> str:
        edge_id = self._generated_edge_id()
        self.edges.append({
            "id": edge_id, "source_id": source, "target_id": target,
            "role": role, "condition": condition, "provenance": [_policy_provenance(rule)],
        })
        return edge_id

    def expand(self) -> ExpandedPackageGraph:
        # User-authored source units are stable and precede generated policy units.
        for index, node in enumerate(self.session.nodes, 1):
            self.source_units.append({
                "id": f"S{index:03d}",
                "text": node.source_excerpt or node.source_statement,
                "source_hash": None,
            })

        for index, node in enumerate(self.session.nodes, 1):
            unit_id = f"S{index:03d}"
            source_ref = {
                "source_unit_id": unit_id,
                "source_quote": node.source_excerpt or node.source_statement,
            }
            outgoing = self.outgoing[node.id]
            req_id = self.req_by_node[node.id]
            node_edges: list[str] = []
            config = node.config
            next_id = outgoing[0].target_id if outgoing else None
            next_req = self.req_by_node[next_id] if next_id else None

            if node.capability == "send_text_message":
                node_payload = {"id": node.id, "type": "send_message", "label": None, "copy": config["copy"], "locale": config["locale"], "next_node_id": next_id, "source_refs": [source_ref]}
                requirement_payload = {"copy": config["copy"], "locale": config["locale"], "next_requirement_id": next_req}
            elif node.capability == "capture_user_input":
                exhausted_node, exhausted_req = self.add_policy_end("input-retry-exhausted-terminal", POLICY.retry_exhausted_reason)
                timeout_node, timeout_req = self.add_policy_end("input-no-response-terminal", POLICY.no_response_reason)
                retry = {"max_attempts": POLICY.retry.max_attempts, "messages": list(POLICY.retry.messages), "on_exhausted_node_id": exhausted_node}
                no_response = {"timeout_seconds": POLICY.no_response_timeout_seconds, "next_node_id": timeout_node}
                node_payload = {"id": node.id, "type": "capture_input", "label": None, "prompt": config["prompt"], "input_type": config["input_type"], "save_as": config["save_as"], "required": config["required"], "validation": config["validation"], "next_node_id": next_id, "retry": retry, "no_response": no_response, "source_refs": [source_ref]}
                requirement_payload = {"prompt": config["prompt"], "input_type": config["input_type"], "save_as": config["save_as"], "required": config["required"], "validation": config["validation"], "next_requirement_id": next_req, "retry": {"max_attempts": POLICY.retry.max_attempts, "messages": list(POLICY.retry.messages), "on_exhausted_requirement_id": exhausted_req}, "no_response": {"timeout_seconds": POLICY.no_response_timeout_seconds, "next_requirement_id": timeout_req}}
                node_edges.extend([
                    self.add_policy_edge(node.id, exhausted_node, "exhausted", {"type": "exhausted"}, "input-retry-exhausted-terminal"),
                    self.add_policy_edge(node.id, timeout_node, "timeout", {"type": "timeout", "seconds": POLICY.no_response_timeout_seconds}, "input-no-response-terminal"),
                ])
            elif node.capability == "fixed_choice":
                exhausted_node, exhausted_req = self.add_policy_end("choice-retry-exhausted-terminal", POLICY.retry_exhausted_reason)
                timeout_node, timeout_req = self.add_policy_end("choice-no-response-terminal", POLICY.no_response_reason)
                retry_node, retry_req, _ = self.add_policy_retry(
                    originating_choice_id=node.id,
                    exhausted_node_id=exhausted_node,
                    exhausted_req_id=exhausted_req,
                )
                target = {edge.stable_value: edge.target_id for edge in outgoing}
                target_req = {edge.stable_value: self.req_by_node[edge.target_id] for edge in outgoing}
                node_payload = {"id": node.id, "type": "fixed_choice", "label": None, "title": config["title"], "outcomes": [{"label": item["label"], "value": item["value"], "next_node_id": target[item["value"]]} for item in config["options"]], "stable_values": [item["value"] for item in config["options"]], "default_node_id": retry_node, "invalid_node_id": retry_node, "timeout_node_id": timeout_node, "source_refs": [source_ref]}
                requirement_payload = {"title": config["title"], "outcomes": [{"label": item["label"], "value": item["value"], "next_requirement_id": target_req[item["value"]]} for item in config["options"]], "stable_values": [item["value"] for item in config["options"]], "default_requirement_id": retry_req, "invalid_requirement_id": retry_req, "timeout_requirement_id": timeout_req}
                node_edges.extend([
                    self.add_policy_edge(node.id, retry_node, "default", {"type": "default"}, "bounded-invalid-response-retry"),
                    self.add_policy_edge(node.id, retry_node, "invalid", {"type": "invalid"}, "bounded-invalid-response-retry"),
                    self.add_policy_edge(node.id, timeout_node, "timeout", {"type": "timeout", "seconds": POLICY.no_response_timeout_seconds}, "choice-no-response-terminal"),
                ])
            elif node.capability == "persist_contact_field":
                failure_node, failure_req = self.add_policy_end("persistence-failure-terminal", POLICY.persistence_failure_reason)
                node_payload = {"id": node.id, "type": "persist_contact_field", "label": None, "source_variable": config["source_variable"], "field_name": config["field_name"], "success_node_id": next_id, "failure_node_id": failure_node, "source_refs": [source_ref]}
                requirement_payload = {"source_variable": config["source_variable"], "field_name": config["field_name"], "success_requirement_id": next_req, "failure_requirement_id": failure_req}
                node_edges.append(self.add_policy_edge(node.id, failure_node, "failure", {"type": "failure"}, "persistence-failure-terminal"))
            elif node.capability == "end":
                node_payload = {"id": node.id, "type": "end", "label": None, "reason": config["reason"], "source_refs": [source_ref]}
                requirement_payload = {"reason": config["reason"]}
            else:
                raise ValueError(f"P4_UNSUPPORTED_CAPABILITY:{node.capability}")

            self.nodes.append(node_payload)
            self.requirements.append({
                "id": req_id, "capability": node.capability,
                "summary": f"Execute {node.capability} at {node.id}.", "status": "proposed",
                "provenance": [_user_provenance(
                    f"product4:{node.id}",
                    self.source_hash,
                    node.source_excerpt or node.source_statement,
                )],
                "payload": requirement_payload, "decision_ids": [], "depends_on": [], "cross_references": [],
            })
            self.coverage.append({"source_unit_id": unit_id, "requirement_ids": [req_id], "node_ids": [node.id], "edge_ids": [edge.id for edge in outgoing] + node_edges})

        # Authored edges remain the only business exits and retain user provenance.
        for edge in self.session.edges:
            role = "outcome" if edge.stable_value is not None else ("success" if next(node for node in self.session.nodes if node.id == edge.source_id).capability == "persist_contact_field" else "next")
            condition = ({"type": "choice", "title": edge.label, "stable_value": edge.stable_value} if role == "outcome" else ({"type": "success"} if role == "success" else None))
            source_node = next(node for node in self.session.nodes if node.id == edge.source_id)
            self.edges.append({
                "id": edge.id, "source_id": edge.source_id, "target_id": edge.target_id,
                "role": role, "condition": condition,
                "provenance": [_user_provenance(
                    f"product4:{edge.id}",
                    self.source_hash,
                    source_node.source_excerpt or source_node.source_statement,
                )],
            })

        metadata = [{
            "type": "policy", "key": "product4.technical-policy",
            "value": {**policy_payload(), "canonical_hash": policy_hash()},
            "provenance": [_policy_provenance("registry")],
        }, {
            "type": "review", "key": "product4.flow-title",
            "value": self.session.title,
            "provenance": [_user_provenance(
                "product4:flow-title",
                self.source_hash,
                self.session.nodes[0].source_statement,
            )],
        }, {
            "type": "review", "key": "product4.derived-statements",
            "value": {node.id: node.source_statement for node in self.session.nodes},
            "provenance": [_user_provenance(
                "product4:semantic-translation",
                self.source_hash,
                self.session.original_brief or self.session.nodes[0].source_statement,
            )],
        }]
        trigger_state = self.session.flow_trigger_metadata
        if trigger_state is not None:
            confirmed_prose = self.session.original_brief or "\n".join(
                node.source_statement for node in self.session.nodes
            )
            trigger_provenance = []
            for keyword in trigger_state.keywords:
                if (
                    keyword.source == "confirmed_prose"
                    and keyword.source_excerpt not in confirmed_prose
                ):
                    raise ValueError("P4_TRIGGER_PROSE_QUOTE_NOT_IN_CONFIRMED_PROSE")
                trigger_provenance.append({
                    "source": keyword.source,
                    "reference": trigger_provenance_reference(keyword.value),
                    "quote": keyword.source_excerpt,
                    "source_hash": self.source_hash if keyword.source == "confirmed_prose" else None,
                    "policy_version": None,
                })
            metadata.append({
                "type": "custom",
                "key": TRIGGER_METADATA_KEY,
                "value": [keyword.value for keyword in trigger_state.keywords],
                "provenance": trigger_provenance,
            })
        simulated_records = [
            record for record in self.session.answer_records
            if record.source == "simulated_user_evaluation_decision"
        ]
        if simulated_records:
            metadata.append({
                "type": "custom",
                "key": "product4.simulated-user-evaluation-decisions",
                "value": [
                    {
                        "answer_record_id": record.id,
                        "question_id": record.question_id,
                        "role": "simulated_user",
                        "provenance": "simulated_user_evaluation_decision",
                        "rationale": record.rationale,
                        "answered_at": record.answered_at,
                        "model_identity": record.model_identity,
                        "prior_answer_context_hash": record.prior_answer_context_hash,
                    }
                    for record in simulated_records
                ],
                # The pinned authoring-package-1.0 provenance enum has no evaluation
                # role. This metadata is explicitly policy provenance; decisions below
                # retain the exact evaluation role in their reference.
                "provenance": [_policy_provenance("simulated-user-evaluation-overlay")],
            })
        decisions: list[dict[str, Any]] = []
        field_paths = {
            ("send_text_message", "copy"): "message.copy",
            ("send_text_message", "locale"): "message.locale",
            ("capture_user_input", "prompt"): "input.prompt",
            ("capture_user_input", "input_type"): "input.input_type",
            ("capture_user_input", "save_as"): "input.save_as",
            ("capture_user_input", "required"): "input.required",
            ("capture_user_input", "validation"): "input.validation",
            ("fixed_choice", "title"): "choice.title",
            ("fixed_choice", "options"): "choice.outcomes",
            ("persist_contact_field", "source_variable"): "persistence.source_variable",
            ("persist_contact_field", "field_name"): "persistence.field_name",
            ("end", "reason"): "end.reason",
        }
        requirements_by_id = {item["id"]: item for item in self.requirements}
        for record in sorted(
            (item for item in self.session.answer_records if item.node_id),
            key=lambda item: (item.node_id or "", item.id),
        ):
            raw_field = record.field_path.removeprefix("config.")
            mapped_path = field_paths.get((record.capability, raw_field))
            requirement_id = self.req_by_node.get(record.node_id or "")
            if not requirement_id:
                continue
            evaluation_source = record.source == "simulated_user_evaluation_decision"
            # authoring-package-1.0 pins this enum. Preserve its valid decision source
            # while carrying the exact evaluation provenance in the reference/metadata.
            source = "confirmed_user_decision" if evaluation_source else record.source
            provenance = {
                "source": source,
                "reference": (
                    f"simulated_user_evaluation_decision:{record.id}"
                    if evaluation_source else f"answer:{record.id}"
                ),
                "quote": record.prompt if source in {"confirmed_user_decision", "confirmed_prose"} else None,
                "source_hash": self.source_hash if source == "confirmed_prose" else None,
                "policy_version": (
                    "product4-context-derivation-1.0"
                    if source == "approved_versioned_policy"
                    else None
                ),
            }
            if mapped_path:
                decision_id = f"DEC-{len(decisions) + 1:03d}"
                decisions.append({
                    "id": decision_id,
                    "field_path": mapped_path,
                    "value": record.value,
                    "source": source,
                    "status": "proposed",
                    "provenance": provenance,
                    "requirement_ids": [requirement_id],
                    "cross_references": [],
                })
                requirements_by_id[requirement_id]["decision_ids"].append(decision_id)
            else:
                requirements_by_id[requirement_id]["provenance"].append(provenance)
        return ExpandedPackageGraph(
            self.nodes,
            self.edges,
            self.requirements,
            decisions,
            self.source_units,
            self.coverage,
            metadata,
        )


def expand_package_graph(session: AuthoringSession) -> ExpandedPackageGraph:
    validate_draft(session)
    prose = session.original_brief or "\n".join(node.source_statement for node in session.nodes)
    graph = _Expansion(session, _sha(prose)).expand()
    validate_expanded_graph(graph, authored_node_ids={node.id for node in session.nodes})
    return graph


def validate_expanded_graph(
    graph: ExpandedPackageGraph,
    *,
    authored_node_ids: set[str],
) -> None:
    """Validate generated policy topology separately from the authored tree."""

    nodes = {node["id"]: node for node in graph.nodes}
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in graph.edges:
        if edge["source_id"] not in nodes or edge["target_id"] not in nodes:
            raise ValueError("P4_EXPANDED_DANGLING_EDGE")
        outgoing[edge["source_id"]].append(edge)

    generated_ids = set(nodes) - authored_node_ids
    for node_id in generated_ids:
        refs = nodes[node_id].get("source_refs", [])
        if not refs or not all(
            ref["source_quote"].startswith("Generated technical behavior:") for ref in refs
        ):
            raise ValueError("P4_GENERATED_NODE_PROVENANCE_MISSING")
    for edge in graph.edges:
        if edge["source_id"] in generated_ids or edge["target_id"] in generated_ids:
            provenance = edge.get("provenance", [])
            if not provenance or not all(
                item.get("source") == "approved_versioned_policy"
                and item.get("policy_version") == TECHNICAL_POLICY_VERSION
                for item in provenance
            ):
                raise ValueError("P4_GENERATED_EDGE_PROVENANCE_MISSING")

    for node in graph.nodes:
        routes = outgoing[node["id"]]
        if node["type"] == "fixed_choice" and node["id"] in authored_node_ids:
            retry_node_id = node["default_node_id"]
            if node["invalid_node_id"] != retry_node_id:
                raise ValueError("P4_CHOICE_RETRY_TARGET_MISMATCH")
            retry_node = nodes.get(retry_node_id)
            if not retry_node or retry_node["type"] != "retry_policy":
                raise ValueError("P4_CHOICE_RETRY_POLICY_MISSING")
            if retry_node["max_attempts"] != POLICY.retry.max_attempts:
                raise ValueError("P4_CHOICE_RETRY_NOT_BOUNDED")
            retry_routes = outgoing[retry_node_id]
            if not any(
                edge["role"] == "retry" and edge["target_id"] == node["id"]
                for edge in retry_routes
            ):
                raise ValueError("P4_CHOICE_RETRY_RETURN_MISSING")
            exhausted = [edge for edge in retry_routes if edge["role"] == "exhausted"]
            if len(exhausted) != 1 or nodes[exhausted[0]["target_id"]]["type"] != "end":
                raise ValueError("P4_CHOICE_RETRY_EXHAUSTION_MISSING")
            if not any(edge["role"] == "default" and edge["target_id"] == retry_node_id for edge in routes):
                raise ValueError("P4_CHOICE_DEFAULT_ROUTE_MISSING")
            if not any(edge["role"] == "invalid" and edge["target_id"] == retry_node_id for edge in routes):
                raise ValueError("P4_CHOICE_INVALID_ROUTE_MISSING")
            if nodes[node["timeout_node_id"]]["type"] != "end":
                raise ValueError("P4_CHOICE_TIMEOUT_TERMINAL_MISSING")
        elif node["type"] == "capture_input" and node["id"] in authored_node_ids:
            retry = node.get("retry") or {}
            no_response = node.get("no_response") or {}
            if retry.get("max_attempts") != POLICY.retry.max_attempts:
                raise ValueError("P4_INPUT_RETRY_NOT_BOUNDED")
            if nodes.get(retry.get("on_exhausted_node_id"), {}).get("type") != "end":
                raise ValueError("P4_INPUT_EXHAUSTION_TERMINAL_MISSING")
            if no_response.get("timeout_seconds") != POLICY.no_response_timeout_seconds:
                raise ValueError("P4_INPUT_TIMEOUT_POLICY_MISMATCH")
            if nodes.get(no_response.get("next_node_id"), {}).get("type") != "end":
                raise ValueError("P4_INPUT_TIMEOUT_TERMINAL_MISSING")
        elif node["type"] == "persist_contact_field" and node["id"] in authored_node_ids:
            failure_id = node["failure_node_id"]
            if failure_id in authored_node_ids or nodes.get(failure_id, {}).get("type") != "end":
                raise ValueError("P4_PERSISTENCE_FAILURE_POLICY_MISSING")

    # The expanded graph may cycle only on explicit generated retry-return edges.
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge["role"] != "retry":
            adjacency[edge["source_id"]].append(edge["target_id"])
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("P4_EXPANDED_UNREGISTERED_CYCLE")
        if node_id in visited:
            return
        visiting.add(node_id)
        for target in adjacency[node_id]:
            visit(target)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in nodes:
        visit(node_id)


def build_frozen_package(
    session: AuthoringSession,
    confirmed_by: str = "user",
    *,
    confirmed_at: Any = None,
) -> dict[str, Any]:
    validate_draft(session)
    confirmed_prose = session.original_brief or "\n".join(node.source_statement for node in session.nodes)
    source_hash = _sha(confirmed_prose)
    expanded = _Expansion(session, source_hash).expand()
    validate_expanded_graph(expanded, authored_node_ids={node.id for node in session.nodes})
    ledger = CombinedRequirementsLedger.model_validate({
        "schema_version": "requirements-ledger-1.0",
        "id": f"LEDGER-{_sha(session.id)[:12].upper()}", "source_hash": source_hash,
        "requirements": expanded.requirements, "decisions": expanded.decisions, "status": "draft",
        "confirmation": None, "frozen_hash": None, "revision": session.revision,
    })
    frozen = freeze_ledger(confirm_ledger(
        ledger, confirmed_by=confirmed_by, confirmed_at=confirmed_at,
    ))
    package = {
        "schema_version": "authoring-package-1.0",
        "source": {"confirmed_prose": confirmed_prose, "source_hash": source_hash, "source_units": expanded.source_units},
        "ledger": frozen.to_mutable_ledger().model_dump(mode="json"),
        "nodes": expanded.nodes, "edges": expanded.edges, "resources": [],
        "integrations": [], "metadata": expanded.metadata,
        "source_coverage": expanded.coverage,
        "capability_profile_version": "authoring-capability-profile-1.0",
    }
    return validate_frozen_package(package).model_dump(mode="json")
