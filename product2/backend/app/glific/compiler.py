from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid5

from app.contracts import ExecutableIR, ExecutableNode, InteractionContract
from app.flow_spec.capabilities import node_enabled_locally
from app.flow_spec.contracts import (
    AskChoiceNode,
    AskInputNode,
    CallWebhookNode,
    EndNode,
    EvaluateNode,
    FlowSpecNode,
    GlificFlowSpec,
    RecordRequestNode,
    SendMessageNode,
    UpdateContactNode,
)
from app.glific.capabilities import load_capabilities

COMPILER_VERSION = "product2-glific-compiler-0.3"
FLOW_SPEC_COMPILER_VERSION = "product2-glific-compiler-1.0"
SPEC_VERSION = "13.1.0"
FLOW_SPEC_INTERACTIVE_TEMPLATE_SOURCE_ID_MAX = (1 << 48) - 1


class CompilerError(Exception):
    pass


@dataclass(frozen=True)
class CompilationResult:
    artifact: dict[str, Any]
    compilation_map: dict[str, list[str]]
    canonical_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _uid(flow_uuid: str, logical_id: str, role: str) -> str:
    return str(uuid5(UUID(flow_uuid), f"{logical_id}:{role}"))


def _action_uuid(flow_uuid: str, node_id: str, role: str = "action") -> str:
    return _uid(flow_uuid, node_id, role)


def _interactive_runtime_value(choice: Any, mode: str) -> str:
    capabilities = load_capabilities()
    mapping = capabilities.get("interactive_selection", {}).get(mode, {})
    if mapping.get("runtime_value", "visible_title") == "visible_title":
        return str(choice.label)
    return str(choice.value)


def _interactive_payload(node: ExecutableNode) -> dict[str, Any]:
    interactive = node.config.interactive
    assert interactive is not None
    if interactive.mode == "quick_reply":
        return {
            "type": "quick_reply",
            "content": {"type": "text", "header": interactive.title, "text": interactive.body},
            "options": [{"type": "text", "title": choice.label} for choice in interactive.choices],
        }
    return {
        "type": "list",
        "title": interactive.title,
        "body": interactive.body,
        "globalButtons": [{"type": "text", "title": "Choose"}],
        "items": [
            {
                "title": interactive.title,
                "subtitle": interactive.footer or "Choose one option",
                "options": [
                    {"type": "text", "title": choice.label} for choice in interactive.choices
                ],
            }
        ],
    }


def _message_action(flow_uuid: str, node: ExecutableNode) -> dict[str, Any]:
    if node.kind != "send_message":
        raise CompilerError("MESSAGE_ACTION_KIND_INVALID")
    action_id = _action_uuid(flow_uuid, node.id)
    if node.config.format == "interactive":
        return {
            "id": int(sha256(action_id.encode()).hexdigest()[:10], 16),
            "name": f"product2_{node.id}",
            "text": json.dumps(
                _interactive_payload(node), ensure_ascii=False, separators=(",", ":")
            ),
            "type": "send_interactive_msg",
            "uuid": action_id,
        }
    return {
        "uuid": action_id,
        "type": "send_msg",
        "text": node.config.text or "",
        "quick_replies": [],
        "labels": [],
        "attachments": [],
    }


def _contract_for_node(ir: ExecutableIR, node: ExecutableNode) -> InteractionContract:
    contract_id = node.interaction_contract_id or (
        node.interaction_contract_ids[0] if node.interaction_contract_ids else None
    )
    if not contract_id:
        raise CompilerError(f"INTERACTION_CONTRACT_REFERENCE_MISSING:{node.id}")
    contract = next(
        (item for item in ir.interaction_contracts.contracts if item.id == contract_id), None
    )
    if contract is None:
        raise CompilerError(f"INTERACTION_CONTRACT_NOT_FOUND:{contract_id}")
    return contract


def _router_for_node(
    flow_uuid: str,
    ir: ExecutableIR,
    node: ExecutableNode,
    outgoing: list[dict[str, Any]],
    edge_by_id: dict[str, Any],
    var_names: dict[str, str],
) -> dict[str, Any]:
    if node.kind == "switch":
        contract = _contract_for_node(ir, node)
        config = node.config
        result_name = "choice"
        if config.operand.variable_id:
            result_name = var_names.get(config.operand.variable_id, "choice")
        cases = []
        categories = []
        for case in config.cases:
            exit_uuid = _uid(flow_uuid, case.edge_id, "exit")
            case_key = f"{node.id}:{case.option_id or case.outcome_id or case.id}"
            category_uuid = _uid(flow_uuid, case_key, "category")
            cases.append(
                {
                    "uuid": _uid(flow_uuid, case_key, "case"),
                    "type": "has_only_phrase" if case.operator == "equals" else "has_any_word",
                    "arguments": [str(case.value)],
                    "category_uuid": category_uuid,
                }
            )
            categories.append(
                {
                    "uuid": category_uuid,
                    "name": str(case.value),
                    "exit_uuid": exit_uuid,
                }
            )
        if not cases:
            raise CompilerError(f"SWITCH_CASES_MISSING:{contract.id}")
        default_exit = _uid(flow_uuid, node.config.default_edge_id, "exit")
        default_category = _uid(flow_uuid, node.id, "default_category")
        categories.append({"uuid": default_category, "name": "Other", "exit_uuid": default_exit})
        operand = config.operand.expression or f"@results.{result_name}"
        return {
            "type": "switch",
            "operand": operand,
            "result_name": result_name,
            "wait": {"type": "msg"},
            "cases": cases,
            "categories": categories,
            "default_category_uuid": default_category,
        }
    if node.kind == "wait_for_response":
        contract = _contract_for_node(ir, node)
        result_name = var_names.get(node.config.result_variable_id, "response")
        response_edges = [
            edge
            for edge in edge_by_id.values()
            if edge.from_ == node.id and edge.label not in {"retry exhausted", "no response"}
        ]
        edge_by_option = {edge.option_id: edge for edge in response_edges if edge.option_id}
        cases: list[dict[str, Any]] = []
        categories: list[dict[str, Any]] = []
        options = (
            list(getattr(contract, "options", [])) if node.config.criteria.get("choices") else []
        )
        for index, choice in enumerate(options):
            edge = edge_by_option.get(choice.id)
            if edge is None:
                raise CompilerError(f"WAIT_OPTION_EDGE_MISSING:{contract.id}:{choice.id}")
            category_uuid = _uid(flow_uuid, f"{node.id}:{choice.id}", "category")
            mode = "list" if len(options) > 3 else "quick_reply"
            # The compiled Glific representation is title-based per the pinned
            # capability mapping; the stable contract value remains in IR and
            # the compilation map.
            mapping = load_capabilities().get("interactive_selection", {}).get(mode, {})
            runtime_value = (
                choice.title
                if mapping.get("runtime_value", "visible_title") == "visible_title"
                else choice.submitted_value
            )
            cases.append(
                {
                    "uuid": _uid(flow_uuid, f"{node.id}:{choice.id}", "case"),
                    "type": "has_only_phrase",
                    "arguments": [runtime_value],
                    "category_uuid": category_uuid,
                }
            )
            categories.append(
                {
                    "uuid": category_uuid,
                    "name": choice.title,
                    "exit_uuid": _uid(flow_uuid, edge.id, "exit"),
                }
            )
        retry_edge = next(
            (
                edge
                for edge in edge_by_id.values()
                if edge.from_ == node.id and edge.label == "retry exhausted"
            ),
            None,
        )
        ordinary = [
            edge for edge in response_edges if edge.id != (retry_edge.id if retry_edge else None)
        ]
        default_edge = retry_edge if options else (ordinary[0] if ordinary else retry_edge)
        default_category_uuid = None
        if default_edge is not None:
            default_category_uuid = _uid(flow_uuid, node.id, "default_category")
            categories.append(
                {
                    "uuid": default_category_uuid,
                    "name": "Other",
                    "exit_uuid": _uid(flow_uuid, default_edge.id, "exit"),
                }
            )
        router = {
            "type": "switch",
            "operand": "@input.text",
            "result_name": result_name,
            "wait": {"type": "msg"},
            "cases": cases,
            "categories": categories,
        }
        if default_category_uuid is not None:
            router["default_category_uuid"] = default_category_uuid
        return router
    raise CompilerError("ROUTER_NODE_KIND_UNSUPPORTED")


def compile_ir(ir: ExecutableIR) -> CompilationResult:
    if ir.schema_version != "glific-executable-ir-0.3":
        raise CompilerError("EXECUTABLE_IR_V03_REQUIRED")
    flow_uuid = ir.flow.id
    edge_by_id = {edge.id: edge for edge in ir.edges}
    outgoing: dict[str, list[Any]] = {}
    for edge in ir.edges:
        source_node = next((node for node in ir.nodes if node.id == edge.from_), None)
        if source_node and source_node.kind == "end":
            raise CompilerError("TERMINAL_HAS_OUTGOING_EDGE")
        outgoing.setdefault(edge.from_, []).append(edge)
    var_names = {variable.id: variable.name for variable in ir.variables}
    glific_nodes: list[dict[str, Any]] = []
    ui_nodes: dict[str, Any] = {}
    compilation_map: dict[str, list[str]] = {
        node.id: [_uid(flow_uuid, node.id, "node")] for node in ir.nodes
    }
    compilation_map.update(
        {
            operation_id: [_uid(flow_uuid, operation_id, "operation")]
            for node in ir.nodes
            for operation_id in node.normalized_operation_ids
        }
    )
    compilation_map.update({edge.id: [_uid(flow_uuid, edge.id, "exit")] for edge in ir.edges})
    compilation_map.update(
        {variable.id: [_uid(flow_uuid, variable.id, "variable")] for variable in ir.variables}
    )
    for contract in ir.interaction_contracts.contracts:
        owner_node = next(
            (
                node
                for node in ir.nodes
                if node.interaction_contract_id == contract.id
                or contract.id in node.interaction_contract_ids
            ),
            None,
        )
        if owner_node is None:
            continue
        owner_uuid = _uid(flow_uuid, owner_node.id, "node")
        for outcome in contract.outcomes:
            route = next(
                (edge for edge in ir.edges if edge.outcome_id == outcome.id),
                None,
            )
            compilation_map[outcome.id] = [
                _uid(flow_uuid, route.id, "exit") if route else owner_uuid
            ]

    indegree = {node.id: 0 for node in ir.nodes}
    for edge in ir.edges:
        indegree[edge.to] = indegree.get(edge.to, 0) + 1
    layer: dict[str, int] = {node_id: 0 for node_id, degree in indegree.items() if degree == 0}
    for _ in range(len(ir.nodes) + 1):
        for edge in ir.edges:
            if edge.from_ in layer:
                layer[edge.to] = max(layer.get(edge.to, 0), layer[edge.from_] + 1)
    siblings: dict[int, list[str]] = {}
    for node in ir.nodes:
        siblings.setdefault(layer.get(node.id, 0), []).append(node.id)

    for node in ir.nodes:
        glific_id = compilation_map[node.id][0]
        if node.interaction_contract_id:
            compilation_map.setdefault(node.interaction_contract_id, []).append(glific_id)
        exits = [
            {
                "uuid": _uid(flow_uuid, edge.id, "exit"),
                "destination_uuid": _uid(flow_uuid, edge.to, "node"),
            }
            for edge in outgoing.get(node.id, [])
        ]
        if not exits:
            exits.append(
                {"uuid": _uid(flow_uuid, node.id, "terminal_exit"), "destination_uuid": None}
            )
        compiled: dict[str, Any] = {"uuid": glific_id, "actions": [], "exits": exits}
        if node.kind == "send_message":
            compiled["actions"] = [_message_action(flow_uuid, node)]
            if node.config.interactive:
                for choice in node.config.interactive.choices:
                    if choice.option_id:
                        compilation_map.setdefault(choice.option_id, []).append(
                            _uid(flow_uuid, f"{node.id}:{choice.option_id}", "option")
                        )
        elif node.kind in {"switch", "wait_for_response"}:
            compiled["router"] = _router_for_node(flow_uuid, ir, node, exits, edge_by_id, var_names)
            if node.kind == "wait_for_response":
                contract = _contract_for_node(ir, node)
                for option in getattr(contract, "options", []):
                    compilation_map.setdefault(option.id, []).append(
                        _uid(flow_uuid, f"{node.id}:{option.id}", "case")
                    )
                    route = next(
                        (
                            edge
                            for edge in ir.edges
                            if edge.from_ == node.id and edge.option_id == option.id
                        ),
                        None,
                    )
                    if route:
                        compilation_map.setdefault(option.semantic_outcome_id, []).append(
                            _uid(flow_uuid, route.id, "exit")
                        )
            for case in getattr(node.config, "cases", []):
                if case.option_id:
                    compilation_map.setdefault(case.option_id, []).append(
                        _uid(flow_uuid, f"{node.id}:{case.option_id}", "case")
                    )
                if case.outcome_id:
                    compilation_map.setdefault(case.outcome_id, []).append(
                        _uid(flow_uuid, case.edge_id, "exit")
                    )
        elif node.kind == "call_webhook":
            integration = next(iter(ir.integrations), None)
            compiled["actions"] = [
                {
                    "uuid": _action_uuid(flow_uuid, node.id),
                    "type": "call_webhook",
                    "name": node.config.result_name,
                    "method": node.config.method,
                    "url": f"@env.{integration.base_url_ref if integration else 'EXTERNAL_INTEGRATION_URL'}",
                    "body": node.config.body or {},
                }
            ]
        elif node.kind == "end":
            # End is control-only.  Any user-facing final content must be a
            # preceding SendMessageNode, never synthesized from EndConfig.
            compiled["actions"] = []
        else:
            compiled["actions"] = [{"uuid": _action_uuid(flow_uuid, node.id), "type": node.kind}]
        glific_nodes.append(compiled)
        siblings_for_layer = sorted(siblings.get(layer.get(node.id, 0), []))
        left = 100 + siblings_for_layer.index(node.id) * 320
        top = 100 + layer.get(node.id, 0) * 180
        ui_nodes[glific_id] = {
            "type": "wait_for_response"
            if node.kind in {"switch", "wait_for_response"}
            else "execute_actions",
            "position": {"top": top, "left": left},
        }

    # Preserve every declared outcome, including retry/no-response/failure
    # outcomes and message-only terminal outcomes, in the cross-layer map.
    for contract in ir.interaction_contracts.contracts:
        for outcome in contract.outcomes:
            mapped: list[str] = []
            for edge in ir.edges:
                if edge.interaction_contract_id == contract.id and edge.outcome_id == outcome.id:
                    mapped.append(_uid(flow_uuid, edge.id, "exit"))
            if not mapped and contract.failure_policy:
                contract_node_ids = {
                    node.id for node in ir.nodes if node.interaction_contract_id == contract.id
                }
                failure_edges = [
                    edge
                    for edge in ir.edges
                    if edge.from_ in contract_node_ids
                    and edge.label in {"retry exhausted", "no response"}
                ]
                no_response_ids = {
                    contract.failure_policy.no_response.outcome_id
                    if contract.failure_policy.no_response
                    else None,
                    contract.failure_policy.timeout_outcome_id,
                }
                preferred_label = (
                    "no response" if outcome.id in no_response_ids else "retry exhausted"
                )
                fallback_edge = next(
                    (edge for edge in failure_edges if edge.label == preferred_label),
                    None,
                )
                if fallback_edge is not None:
                    mapped.append(_uid(flow_uuid, fallback_edge.id, "exit"))
            if not mapped and outcome.target_semantic_node_id:
                mapped.extend(
                    compilation_map.get(
                        next(
                            (
                                node.id
                                for node in ir.nodes
                                if node.interaction_contract_id == contract.id
                                and outcome.target_semantic_node_id in node.semantic_node_ids
                            ),
                            "",
                        ),
                        [],
                    )
                )
            if mapped:
                existing = compilation_map.setdefault(outcome.id, [])
                for reference in mapped:
                    if reference not in existing:
                        existing.append(reference)

    templates = []
    for node in ir.nodes:
        if (
            node.kind == "send_message"
            and node.config.format == "interactive"
            and node.config.interactive
        ):
            templates.append(
                {
                    "source_id": int(sha256(node.id.encode()).hexdigest()[:10], 16),
                    "label": f"product2_{node.id}",
                    "type": node.config.interactive.mode,
                    "interactive_content": _interactive_payload(node),
                    "translations": {},
                    "language_id": 1,
                    "send_with_title": False,
                }
            )
    definition = {
        "uuid": flow_uuid,
        "name": ir.flow.name,
        "spec_version": SPEC_VERSION,
        "language": ir.flow.language,
        "type": "messaging",
        "nodes": glific_nodes,
        "_ui": {"nodes": ui_nodes},
        "localization": {},
        "vars": [_uid(flow_uuid, variable.id, "variable") for variable in ir.variables]
        or [_uid(flow_uuid, "flow", "variable")],
        "revision": 1,
        "expire_after_minutes": ir.flow.expire_after_minutes,
    }
    artifact = {
        "flows": [{"definition": definition, "keywords": ir.flow.keywords}],
        "contact_field": [],
        "collections": [],
        "interactive_templates": templates,
    }
    canonical = canonical_json(artifact)
    return CompilationResult(
        artifact=artifact,
        compilation_map=compilation_map,
        canonical_hash=sha256(canonical.encode()).hexdigest(),
    )


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Product 2 single-spec compiler
# ---------------------------------------------------------------------------


def _flow_spec_uid(flow_uuid: str, logical_id: str, role: str) -> str:
    return _uid(flow_uuid, logical_id, role)


def _flow_spec_node_outgoing(node: FlowSpecNode) -> list[tuple[str, str]]:
    """Return (route key, destination) pairs in authoring order."""

    if isinstance(node, AskChoiceNode):
        return [
            *[(choice.id, choice.next_node_id) for choice in node.choices],
            ("retry_exhausted", node.retry.on_exhausted_node_id),
            ("no_response", node.no_response.next_node_id),
        ]
    if isinstance(node, AskInputNode):
        result = [
            ("success", node.next_node_id),
            ("retry_exhausted", node.retry.on_exhausted_node_id),
        ]
        if node.no_response:
            result.append(("no_response", node.no_response.next_node_id))
        return result
    if isinstance(node, EvaluateNode):
        return [
            *[(f"case_{index}", case.next_node_id) for index, case in enumerate(node.cases)],
            ("default", node.default_node_id),
        ]
    if isinstance(node, CallWebhookNode):
        routes = node.webhook.routes
        return [
            (key, value)
            for key, value in (
                ("success", routes.success_node_id),
                ("empty", routes.empty_node_id),
                ("not_found", routes.not_found_node_id),
                ("conflict", routes.conflict_node_id),
                ("invalid_response", routes.invalid_response_node_id),
                ("http_error", routes.http_error_node_id),
                ("timeout", routes.timeout_node_id),
            )
            if value
        ]
    result: list[tuple[str, str]] = []
    for key in ("next_node_id", "success_node_id", "failure_node_id"):
        value = getattr(node, key, None)
        if value:
            result.append((key.removesuffix("_node_id"), value))
    return result


def _flow_spec_interactive_payload(node: AskChoiceNode) -> dict[str, Any]:
    if node.presentation == "quick_reply":
        return {
            "type": "quick_reply",
            "content": {
                "type": "text",
                "header": node.name[:60],
                "text": node.message.text,
            },
            "options": [{"type": "text", "title": choice.title} for choice in node.choices],
        }
    return {
        "type": "list",
        "title": node.name[:60],
        "body": node.message.text,
        "globalButtons": [{"type": "text", "title": "Choose"}],
        "items": [
            {
                "title": node.name[:60],
                "subtitle": "Choose one option",
                "options": [{"type": "text", "title": choice.title} for choice in node.choices],
            }
        ],
    }


def _flow_spec_interactive_template_source_id(flow_uuid: str, node_id: str) -> int:
    """Return a flow-scoped positive ID safe for Glific's bigint template ID."""

    seed = f"{flow_uuid}\x00{node_id}".encode()
    digest_value = int.from_bytes(sha256(seed).digest()[:8], "big")
    return digest_value % FLOW_SPEC_INTERACTIVE_TEMPLATE_SOURCE_ID_MAX + 1


def _flow_spec_interactive_template_label(flow_uuid: str, node_id: str) -> str:
    """Return a flow-scoped label for Glific's global import lookup."""

    return f"product2_{flow_uuid}_{node_id}"


def _flow_spec_operand(node: EvaluateNode) -> str:
    operand = node.operand
    if operand.source == "variable":
        return f"@results.{operand.variable}"
    if operand.source == "contact_field":
        return f"@contact.{operand.resource_ref}"
    if operand.source == "collection_membership":
        return f"@collection.{operand.resource_ref}"
    return operand.expression or ""


def _flow_spec_action_uuid(flow_uuid: str, node_id: str, role: str = "action") -> str:
    return _flow_spec_uid(flow_uuid, node_id, role)


def _flow_spec_exit(
    flow_uuid: str,
    node_id: str,
    route_key: str,
    destination: str | None = None,
    *,
    destination_uuid: str | None = None,
) -> dict[str, Any]:
    if destination_uuid is None:
        if destination is None:
            raise CompilerError("FS_ROUTE_DESTINATION_MISSING")
        destination_uuid = _flow_spec_uid(flow_uuid, destination, "node")
    return {
        "uuid": _flow_spec_uid(flow_uuid, f"{node_id}:{route_key}", "exit"),
        "destination_uuid": destination_uuid,
    }


def _flow_spec_null_exit(flow_uuid: str, node_id: str, route_key: str = "terminal") -> dict[str, Any]:
    """Build the explicit null destination consumed by Glific Exit.execute."""

    return {
        "uuid": _flow_spec_uid(flow_uuid, f"{node_id}:{route_key}", "exit"),
        "destination_uuid": None,
    }


def _flow_spec_terminal_action(flow_uuid: str, node: EndNode) -> dict[str, Any]:
    """Make an end node executable while keeping its reason internal."""

    return {
        "uuid": _flow_spec_action_uuid(flow_uuid, node.id, "set_run_result"),
        "type": "set_run_result",
        "name": "terminal_reason",
        "value": node.reason,
        "category": node.reason,
    }


def _flow_spec_router_for_choice(
    spec: GlificFlowSpec,
    node: AskChoiceNode,
    logical_id: str,
    route_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    cases = []
    categories = []
    for choice in node.choices:
        category_uuid = _flow_spec_uid(spec.flow.id, f"{logical_id}:{choice.id}", "category")
        cases.append(
            {
                "uuid": _flow_spec_uid(spec.flow.id, f"{logical_id}:{choice.id}", "case"),
                "type": "has_only_phrase",
                "arguments": [choice.title],
                "category_uuid": category_uuid,
            }
        )
        categories.append(
            {
                "uuid": category_uuid,
                "name": choice.title,
                "exit_uuid": route_map[choice.id]["uuid"],
            }
        )
    default_uuid = _flow_spec_uid(spec.flow.id, logical_id, "other_category")
    no_response_uuid = _flow_spec_uid(spec.flow.id, logical_id, "no_response_category")
    categories.append(
        {
            "uuid": default_uuid,
            "name": "Other",
            "exit_uuid": route_map["invalid"]["uuid"],
        }
    )
    categories.append(
        {
            "uuid": no_response_uuid,
            "name": "No Response",
            "exit_uuid": route_map["no_response"]["uuid"],
        }
    )
    return {
        "type": "switch",
        "operand": "@input.text",
        "result_name": node.save_as,
        "wait": {
            "type": "msg",
            "timeout": {
                "seconds": node.no_response.timeout_seconds,
                "category_uuid": no_response_uuid,
                "expression": None,
            },
        },
        "cases": cases,
        "categories": categories,
        "default_category_uuid": default_uuid,
    }


def _flow_spec_router_for_input(
    spec: GlificFlowSpec,
    node: AskInputNode,
    logical_id: str,
    route_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    cases, valid_categories = _flow_spec_input_cases(spec, node, logical_id)
    accepted_uuid = _flow_spec_uid(spec.flow.id, logical_id, "accepted_category")
    other_uuid = _flow_spec_uid(spec.flow.id, logical_id, "other_category")
    no_response_uuid = _flow_spec_uid(spec.flow.id, logical_id, "no_response_category")
    categories = [
        *valid_categories,
        {"uuid": other_uuid, "name": "Other", "exit_uuid": route_map["invalid"]["uuid"]},
    ]
    if node.no_response:
        categories.append(
            {
                "uuid": no_response_uuid,
                "name": "No Response",
                "exit_uuid": route_map["no_response"]["uuid"],
            }
        )
    if not valid_categories:
        categories.insert(
            0,
            {"uuid": accepted_uuid, "name": "Accepted", "exit_uuid": route_map["success"]["uuid"]},
        )
    result: dict[str, Any] = {
        "type": "switch",
        "operand": "@input.text",
        "result_name": node.save_as,
        "wait": {
            "type": "msg",
            "timeout": {
                "seconds": node.no_response.timeout_seconds,
                "category_uuid": no_response_uuid,
                "expression": None,
            },
        }
        if node.no_response
        else {"type": "msg"},
        "cases": cases,
        "categories": categories,
        "default_category_uuid": other_uuid if cases else categories[0]["uuid"],
    }
    return result


def _flow_spec_input_cases(
    spec: GlificFlowSpec, node: AskInputNode, logical_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lower only parser/constraint combinations proven by official cases."""

    validation = node.validation
    parser = validation.parser if validation else "plain_text"
    constraints = validation.constraints if validation else {}
    supported_input_parsers = {
        "text": {"plain_text"},
        "number": {"integer"},
        "time": {"local_12_hour_time"},
        "email": {"email"},
        "phone": {"phone"},
    }
    if parser not in supported_input_parsers.get(node.input_type, set()):
        raise CompilerError(
            f"FS_TYPED_INPUT_UNREPRESENTABLE:{node.id}:{node.input_type}:{parser}"
        )
    cases: list[dict[str, Any]] = []
    valid_categories: list[dict[str, Any]] = []

    def add_case(case_type: str, arguments: list[str], suffix: str, name: str) -> None:
        category_uuid = _flow_spec_uid(spec.flow.id, f"{logical_id}:{suffix}", "category")
        valid_categories.append(
            {
                "uuid": category_uuid,
                "name": name,
                "exit_uuid": _flow_spec_uid(spec.flow.id, f"{logical_id}:success", "exit"),
            }
        )
        cases.append(
            {
                "uuid": _flow_spec_uid(spec.flow.id, f"{logical_id}:{suffix}", "case"),
                "type": case_type,
                "arguments": arguments,
                "category_uuid": category_uuid,
            }
        )

    if parser == "plain_text":
        if not constraints:
            return cases, valid_categories
        if set(constraints) == {"pattern"} and isinstance(constraints["pattern"], str):
            add_case("has_pattern", [constraints["pattern"]], "valid", "Accepted")
            return cases, valid_categories
        if set(constraints) == {"allowed_values"} and isinstance(constraints["allowed_values"], list):
            values = constraints["allowed_values"]
            if not values or any(not isinstance(value, str) or not value for value in values):
                raise CompilerError(f"FS_TYPED_INPUT_UNREPRESENTABLE:{node.id}:allowed_values")
            for index, value in enumerate(values):
                add_case("has_only_phrase", [value], f"valid_{index}", "Accepted")
            return cases, valid_categories
        raise CompilerError(f"FS_TYPED_INPUT_UNREPRESENTABLE:{node.id}:{parser}")

    if parser == "local_12_hour_time" and not constraints:
        add_case(
            "has_pattern",
            [r"^(0?[1-9]|1[0-2])(:[0-5][0-9])?\s?(AM|PM|am|pm)$"],
            "valid",
            "Accepted",
        )
        return cases, valid_categories
    if parser == "email" and not constraints:
        add_case("has_email", [], "valid", "Accepted")
        return cases, valid_categories
    if parser == "phone" and not constraints:
        add_case("has_phone", [], "valid", "Accepted")
        return cases, valid_categories
    if parser == "integer":
        if not constraints:
            add_case("has_pattern", [r"^-?[0-9]+$"], "valid", "Accepted")
            return cases, valid_categories
        if set(constraints) == {"minimum"} and constraints["minimum"] == 0:
            add_case("has_pattern", [r"^(0|[1-9][0-9]*)$"], "valid", "Accepted")
            return cases, valid_categories
    raise CompilerError(f"FS_TYPED_INPUT_UNREPRESENTABLE:{node.id}:{parser}")


def _flow_spec_result_action(
    flow_uuid: str, node_id: str, result_name: str, choice: Any
) -> dict[str, Any]:
    return {
        "uuid": _flow_spec_action_uuid(flow_uuid, f"{node_id}:{choice.id}", "set_run_result"),
        "type": "set_run_result",
        "name": result_name,
        "value": choice.submitted_value,
        "category": choice.title,
    }


def _flow_spec_contact_actions(
    flow_uuid: str, node: RecordRequestNode
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for field_name, expression in sorted(node.fields.items()):
        if not isinstance(field_name, str) or not field_name:
            raise CompilerError(f"FS_CONTACT_FIELD_INVALID:{node.id}")
        if not isinstance(expression, str) or not expression:
            raise CompilerError(f"FS_CONTACT_VALUE_INVALID:{node.id}:{field_name}")
        match = re.fullmatch(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}", expression)
        value = f"@results.{match.group(1)}" if match else expression
        actions.append(
            {
                "uuid": _flow_spec_action_uuid(
                    flow_uuid, f"{node.id}:{field_name}", "set_contact_field"
                ),
                "type": "set_contact_field",
                "value": value,
                "field": {"name": field_name, "key": field_name},
            }
        )
    return actions


def _flow_spec_evaluate_router(
    flow_uuid: str, node: EvaluateNode, route_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    categories: list[dict[str, Any]] = []
    case_types = {
        "equals": ("has_only_phrase", lambda value: [str(value)]),
        "contains_phrase": ("has_phrase", lambda value: [str(value)]),
        "matches_regex": ("has_pattern", lambda value: [str(value)]),
        "number_equals": ("has_number_eq", lambda value: [str(value)]),
        "number_between_inclusive": (
            "has_number_between",
            lambda value: [str(item) for item in value],
        ),
        "contains_any": ("has_any_word", lambda value: [", ".join(value)]),
        "contains_all": ("has_all_words", lambda value: [", ".join(value)]),
    }
    for index, case in enumerate(node.cases):
        mapping = case_types.get(case.operator)
        if mapping is None:
            raise CompilerError(f"FS_EVALUATE_UNREPRESENTABLE:{node.id}:{case.operator}")
        case_type, argument_builder = mapping
        category_uuid = _flow_spec_uid(flow_uuid, f"{node.id}:case_{index}", "category")
        cases.append(
            {
                "uuid": _flow_spec_uid(flow_uuid, f"{node.id}:case_{index}", "case"),
                "type": case_type,
                "arguments": argument_builder(case.value),
                "category_uuid": category_uuid,
            }
        )
        categories.append(
            {
                "uuid": category_uuid,
                "name": str(case.value),
                "exit_uuid": route_map[f"case_{index}"]["uuid"],
            }
        )
    default_category = _flow_spec_uid(flow_uuid, f"{node.id}:default", "category")
    categories.append(
        {"uuid": default_category, "name": "Other", "exit_uuid": route_map["default"]["uuid"]}
    )
    return {
        "type": "switch",
        "operand": _flow_spec_operand(node),
        "result_name": node.operand.variable or "predicate",
        "wait": {"type": "none"},
        "cases": cases,
        "categories": categories,
        "default_category_uuid": default_category,
    }


def compile_flow_spec(spec: GlificFlowSpec) -> CompilationResult:
    """Compile one canonical Flow Spec directly to the pinned Glific JSON contract."""

    if spec.schema_version != "glific-flow-spec-1.0":
        raise CompilerError("GLIFIC_FLOW_SPEC_V10_REQUIRED")
    for node in spec.nodes:
        if not node_enabled_locally(node.type):
            raise CompilerError(f"FS_UNSUPPORTED_CAPABILITY:{node.id}:{node.type}")

    resource_by_name = {resource.logical_name: resource for resource in spec.resources}
    flow_uuid = spec.flow.id
    source_node_uuid = {
        node.id: _flow_spec_uid(flow_uuid, node.id, "node") for node in spec.nodes
    }
    choice_nodes = [node for node in spec.nodes if isinstance(node, AskChoiceNode)]
    interactive_template_source_ids = {
        node.id: _flow_spec_interactive_template_source_id(flow_uuid, node.id)
        for node in choice_nodes
    }
    if len(set(interactive_template_source_ids.values())) != len(interactive_template_source_ids):
        raise CompilerError("FS_INTERACTIVE_TEMPLATE_SOURCE_ID_COLLISION")
    attempt_logicals: dict[str, list[str]] = {}
    wait_logicals: dict[str, list[str]] = {}
    retry_logicals: dict[str, list[str]] = {}
    for node in spec.nodes:
        if isinstance(node, (AskChoiceNode, AskInputNode)):
            attempt_count = node.retry.max_attempts
            attempt_logicals[node.id] = [
                node.id,
                *[f"{node.id}:attempt:{attempt}" for attempt in range(2, attempt_count + 1)],
            ]
            wait_logicals[node.id] = [
                f"{logical_id}:wait" for logical_id in attempt_logicals[node.id]
            ]
            retry_logicals[node.id] = [
                f"{node.id}:attempt:{attempt}:retry_message"
                for attempt in range(1, attempt_count)
                if node.retry.messages
            ]

    choice_value_logicals: dict[str, str] = {}
    for node in spec.nodes:
        if isinstance(node, AskChoiceNode):
            for choice in node.choices:
                choice_value_logicals[choice.id] = f"{node.id}:{choice.id}:value"

    physical: list[tuple[str, str, Any, int | None]] = []
    for node in spec.nodes:
        if isinstance(node, (AskChoiceNode, AskInputNode)):
            for attempt, logical_id in enumerate(attempt_logicals[node.id], start=1):
                physical.append((logical_id, "prompt", node, attempt))
                physical.append((wait_logicals[node.id][attempt - 1], "wait", node, attempt))
            for attempt, logical_id in enumerate(retry_logicals[node.id], start=1):
                physical.append((logical_id, "retry_message", node, attempt))
        else:
            physical.append((node.id, "source", node, None))
    for node in spec.nodes:
        if isinstance(node, AskChoiceNode):
            for choice in node.choices:
                physical.append((choice_value_logicals[choice.id], "choice_value", (node, choice), None))

    physical_uuid = {
        logical_id: _flow_spec_uid(flow_uuid, logical_id, "node")
        for logical_id, _, _, _ in physical
    }
    compilation_map: dict[str, list[str]] = {}
    for node in spec.nodes:
        if isinstance(node, (AskChoiceNode, AskInputNode)):
            compilation_map[node.id] = [
                *[
                    physical_uuid[item]
                    for pair in zip(attempt_logicals[node.id], wait_logicals[node.id])
                    for item in pair
                ],
                *[physical_uuid[item] for item in retry_logicals[node.id]],
            ]
        else:
            compilation_map[node.id] = [physical_uuid[node.id]]

    metadata: dict[str, Any] = {
        "compiler_version": FLOW_SPEC_COMPILER_VERSION,
        "glific_contract": {"spec_version": SPEC_VERSION},
        "node_routes": {},
        "attempt_routes": {},
        "attempt_expansion": {},
        "choices": {},
        "interactive_templates": {},
        "variables": {},
    }
    for variable in spec.variables:
        variable_uuid = _flow_spec_uid(flow_uuid, variable.name, "variable")
        compilation_map[variable.name] = [variable_uuid]
        metadata["variables"][variable.name] = {
            "uuid": variable_uuid,
            "type": variable.type,
            "scope": variable.scope,
        }
    for resource in spec.resources:
        compilation_map[resource.logical_name] = [
            _flow_spec_uid(flow_uuid, resource.logical_name, "resource")
        ]

    def route_map_for(
        logical_id: str, routes: list[tuple[str, str | None, str | None]]
    ) -> dict[str, dict[str, Any]]:
        return {
            route_key: _flow_spec_exit(
                flow_uuid,
                logical_id,
                route_key,
                destination,
                destination_uuid=destination_uuid,
            )
            for route_key, destination, destination_uuid in routes
        }

    compiled_nodes: list[dict[str, Any]] = []
    ui_nodes: dict[str, Any] = {}
    templates: list[dict[str, Any]] = []
    for index, (logical_id, physical_kind, value, attempt) in enumerate(physical):
        if physical_kind == "choice_value":
            node, choice = value
            exit_item = _flow_spec_exit(
                flow_uuid,
                logical_id,
                "next",
                destination=choice.next_node_id,
            )
            compiled = {
                "uuid": physical_uuid[logical_id],
                "actions": [_flow_spec_result_action(flow_uuid, node.id, node.save_as, choice)],
                "exits": [exit_item],
            }
            metadata["choices"][choice.id] = {
                "node_id": node.id,
                "title": choice.title,
                "submitted_value": choice.submitted_value,
                "runtime_matcher": choice.title,
                "destination_node_id": choice.next_node_id,
                "stable_value_node_uuid": physical_uuid[logical_id],
                "stable_value_action_uuid": compiled["actions"][0]["uuid"],
                "stable_value_exit_uuid": exit_item["uuid"],
            }
        elif physical_kind == "retry_message":
            node = value
            message_index = int(attempt or 1) - 1
            message = node.retry.messages[min(message_index, len(node.retry.messages) - 1)]
            next_attempt = attempt_logicals[node.id][int(attempt or 1)]
            compiled = {
                "uuid": physical_uuid[logical_id],
                "actions": [
                    {
                        "uuid": _flow_spec_action_uuid(flow_uuid, logical_id),
                        "type": "send_msg",
                        "text": message,
                        "quick_replies": [],
                        "labels": [],
                        "attachments": [],
                    }
                ],
                "exits": [
                    _flow_spec_exit(flow_uuid, logical_id, "next", destination_uuid=physical_uuid[next_attempt])
                ],
            }
        elif physical_kind in {"prompt", "wait"}:
            node = value
            source_id = node.id
            attempt_index = int(attempt or 1)
            prompt_logical = attempt_logicals[source_id][attempt_index - 1]
            wait_logical = wait_logicals[source_id][attempt_index - 1]
            if physical_kind == "prompt":
                if isinstance(node, AskChoiceNode):
                    action = {
                        "id": interactive_template_source_ids[node.id],
                        "name": f"product2_{node.id}_attempt_{attempt_index}",
                        "text": json.dumps(
                            _flow_spec_interactive_payload(node),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        "type": "send_interactive_msg",
                        "uuid": _flow_spec_action_uuid(flow_uuid, prompt_logical),
                    }
                else:
                    action = {
                        "uuid": _flow_spec_action_uuid(flow_uuid, prompt_logical),
                        "type": "send_msg",
                        "text": node.message.text,
                        "quick_replies": [],
                        "labels": [],
                        "attachments": [],
                    }
                prompt_exit = _flow_spec_exit(
                    flow_uuid,
                    prompt_logical,
                    "wait",
                    destination_uuid=physical_uuid[wait_logical],
                )
                compiled = {
                    "uuid": physical_uuid[prompt_logical],
                    "actions": [action],
                    "exits": [prompt_exit],
                }
                metadata.setdefault("prompt_routes", {})[prompt_logical] = {
                    "wait": prompt_exit
                }
                if isinstance(node, AskChoiceNode) and attempt_index == 1:
                    template_source_id = interactive_template_source_ids[node.id]
                    templates.append(
                        {
                            "source_id": template_source_id,
                            "label": _flow_spec_interactive_template_label(flow_uuid, node.id),
                            "type": node.presentation,
                            "interactive_content": _flow_spec_interactive_payload(node),
                            "translations": {},
                            "language_id": 1,
                            "send_with_title": False,
                        }
                    )
                    metadata["interactive_templates"][node.id] = {
                        "source_id": template_source_id,
                        "label": _flow_spec_interactive_template_label(flow_uuid, node.id),
                        "choice_ids": [choice.id for choice in node.choices],
                        "choice_titles": [choice.title for choice in node.choices],
                        "attempt_action_ids": [
                            template_source_id for _ in attempt_logicals[node.id]
                        ],
                        "attempt_action_uuids": [
                            _flow_spec_action_uuid(flow_uuid, item)
                            for item in attempt_logicals[node.id]
                        ],
                    }
            else:
                is_last = attempt_index == len(attempt_logicals[source_id])
                next_retry_target = (
                    physical_uuid[retry_logicals[source_id][attempt_index - 1]]
                    if not is_last and retry_logicals[source_id]
                    else (
                        physical_uuid[attempt_logicals[source_id][attempt_index]]
                        if not is_last
                        else source_node_uuid[node.retry.on_exhausted_node_id]
                    )
                )
                if isinstance(node, AskChoiceNode):
                    routes: list[tuple[str, str | None, str | None]] = [
                        (
                            choice.id,
                            None,
                            physical_uuid[choice_value_logicals[choice.id]],
                        )
                        for choice in node.choices
                    ]
                    routes.extend(
                        [
                            ("invalid", None, next_retry_target),
                            ("no_response", node.no_response.next_node_id, None),
                        ]
                    )
                    route_map = route_map_for(wait_logical, routes)
                    router = _flow_spec_router_for_choice(spec, node, wait_logical, route_map)
                else:
                    routes = [
                        ("success", node.next_node_id, None),
                        ("invalid", None, next_retry_target),
                    ]
                    if node.no_response:
                        routes.append(("no_response", node.no_response.next_node_id, None))
                    route_map = route_map_for(wait_logical, routes)
                    router = _flow_spec_router_for_input(spec, node, wait_logical, route_map)
                compiled = {
                    "uuid": physical_uuid[wait_logical],
                    "actions": [],
                    "exits": list(route_map.values()),
                    "router": router,
                }
                metadata["attempt_routes"][wait_logical] = route_map
                if attempt_index == 1:
                    metadata["node_routes"][source_id] = route_map
            expansion = metadata["attempt_expansion"].setdefault(source_id, {
                "source_node_id": source_id,
                "first_entry_node_uuid": physical_uuid[attempt_logicals[source_id][0]],
                "prompt_node_uuids": [physical_uuid[item] for item in attempt_logicals[source_id]],
                "wait_node_uuids": [physical_uuid[item] for item in wait_logicals[source_id]],
                "attempt_node_uuids": [physical_uuid[item] for item in attempt_logicals[source_id]],
                "retry_message_node_uuids": [physical_uuid[item] for item in retry_logicals[source_id]],
                "max_attempts": node.retry.max_attempts,
                "retry_messages": list(node.retry.messages),
                "retry_exhausted_destination_node_id": node.retry.on_exhausted_node_id,
                "no_response_destination_node_id": node.no_response.next_node_id if node.no_response else None,
            })
            if physical_kind == "prompt":
                expansion.setdefault("prompt_to_wait_exit_uuids", []).append(
                    _flow_spec_uid(flow_uuid, f"{prompt_logical}:wait", "exit")
                )
        else:
            node = value
            routes = [
                (route_key, destination, None)
                for route_key, destination in _flow_spec_node_outgoing(node)
            ]
            route_map = route_map_for(logical_id, routes)
            compiled = {"uuid": physical_uuid[logical_id], "actions": [], "exits": list(route_map.values())}
            if isinstance(node, SendMessageNode):
                compiled["actions"] = [
                    {
                        "uuid": _flow_spec_action_uuid(flow_uuid, node.id),
                        "type": "send_msg",
                        "text": node.message.text,
                        "quick_replies": [],
                        "labels": [],
                        "attachments": [],
                    }
                ]
            elif isinstance(node, EvaluateNode):
                compiled["router"] = _flow_spec_evaluate_router(flow_uuid, node, route_map)
                metadata["node_routes"][node.id] = route_map
            elif isinstance(node, RecordRequestNode):
                resource = resource_by_name.get(node.resource_ref)
                if resource is None:
                    raise CompilerError(f"FS_ACTION_RESOURCE_MISSING:{node.id}:{node.resource_ref}")
                if node.mechanism != "contact_fields":
                    raise CompilerError(f"FS_UNSUPPORTED_CAPABILITY:{node.id}:record_request:{node.mechanism}")
                actions = _flow_spec_contact_actions(flow_uuid, node)
                compiled["actions"] = actions
                metadata["node_routes"][node.id] = route_map
                metadata.setdefault("record_requests", {})[node.id] = {
                    "mechanism": node.mechanism,
                    "resource_ref": node.resource_ref,
                    "fields": node.fields,
                    "success_node_id": node.success_node_id,
                    "action_uuids": [action["uuid"] for action in actions],
                    "failure_handling": "external_glific_action_error",
                }
            elif isinstance(node, (CallWebhookNode, UpdateContactNode)):
                raise CompilerError(f"FS_UNSUPPORTED_CAPABILITY:{node.id}:{node.type}")
            elif isinstance(node, EndNode):
                compiled["actions"] = [_flow_spec_terminal_action(flow_uuid, node)]
                compiled["exits"] = [_flow_spec_null_exit(flow_uuid, node.id)]
                metadata.setdefault("terminals", {})[node.id] = {
                    "reason": node.reason,
                    "category": node.reason,
                    "action_uuid": compiled["actions"][0]["uuid"],
                    "exit_uuid": compiled["exits"][0]["uuid"],
                    "destination_uuid": None,
                    "generated_policy": any(
                        "policy:product4-technical-policy-1.0:" in ref.source_quote
                        for ref in node.source_refs
                    ),
                }
            else:
                raise CompilerError(f"FS_UNSUPPORTED_CAPABILITY:{node.id}:{node.type}")
            if isinstance(node, (SendMessageNode, EndNode)):
                metadata["node_routes"].setdefault(node.id, route_map)

        ui_nodes[physical_uuid[logical_id]] = {
            "type": "wait_for_response"
            if physical_kind == "wait" or isinstance(value, EvaluateNode)
            else "execute_actions",
            "position": {"top": 100 + (index // 3) * 220, "left": 100 + (index % 3) * 360},
        }
        compiled_nodes.append(compiled)

    for node in spec.nodes:
        if isinstance(node, AskChoiceNode):
            for choice in node.choices:
                option_uuid = _flow_spec_uid(flow_uuid, f"{node.id}:{choice.id}", "option")
                value_uuid = physical_uuid[choice_value_logicals[choice.id]]
                metadata["choices"][choice.id]["option_uuid"] = option_uuid
                compilation_map[choice.id] = [
                    option_uuid,
                    value_uuid,
                    metadata["choices"][choice.id]["stable_value_exit_uuid"],
                ]

    definition = {
        "uuid": flow_uuid,
        "name": spec.flow.name,
        "spec_version": SPEC_VERSION,
        "language": "base",
        "type": "messaging",
        "nodes": compiled_nodes,
        "_ui": {"nodes": ui_nodes},
        "localization": {},
        "vars": [_flow_spec_uid(flow_uuid, variable.name, "variable") for variable in spec.variables],
        "revision": 1,
        "expire_after_minutes": 10080,
    }
    artifact = {
        "flows": [{"definition": definition, "keywords": spec.flow.keywords}],
        "contact_field": [],
        "collections": [],
        "interactive_templates": templates,
    }
    canonical = canonical_json(artifact)
    return CompilationResult(
        artifact=artifact,
        compilation_map=compilation_map,
        canonical_hash=sha256(canonical.encode()).hexdigest(),
        metadata=metadata,
    )
