"""Deterministic Product 2 Flow Spec to the pinned Glific JSON contract."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCT2_BACKEND = PROJECT_ROOT / "product2" / "backend"

_RESULT_KEYS = {"artifact", "canonical_hash", "compilation_map", "metadata"}
_ARTIFACT_KEYS = {"flows", "contact_field", "collections", "interactive_templates"}
_FLOW_KEYS = {"definition", "keywords"}
_DEFINITION_KEYS = {
    "uuid",
    "name",
    "spec_version",
    "language",
    "type",
    "nodes",
    "_ui",
    "localization",
    "vars",
    "revision",
    "expire_after_minutes",
}
_ACTION_KEYS = {
    "send_msg": {"uuid", "type", "text", "quick_replies", "labels", "attachments"},
    "send_interactive_msg": {"id", "name", "text", "type", "uuid"},
    "set_contact_field": {"uuid", "type", "value", "field"},
    "set_run_result": {"uuid", "type", "name", "value", "category"},
}
_EXIT_KEYS = {"uuid", "destination_uuid"}
_CATEGORY_KEYS = {"uuid", "name", "exit_uuid"}
_CASE_KEYS = {"uuid", "type", "arguments", "category_uuid"}
_INTERACTIVE_TEMPLATE_KEYS = {
    "source_id",
    "label",
    "type",
    "interactive_content",
    "translations",
    "language_id",
    "send_with_title",
}
_ROUTER_KEYS = {
    "type",
    "operand",
    "result_name",
    "wait",
    "cases",
    "categories",
    "default_category_uuid",
}
_CASE_TYPES = {
    "has_number_eq",
    "has_number_between",
    "has_number",
    "has_any_word",
    "has_phrase",
    "has_only_phrase",
    "has_only_text",
    "has_all_words",
    "has_multiple",
    "has_phone",
    "has_email",
    "has_pattern",
    "has_beginning",
    "has_intent",
    "has_top_intent",
    "has_group",
    "has_category",
    "has_location",
    "has_media",
    "has_audio",
    "has_video",
    "has_image",
    "has_file",
}
_REPORT_SCHEMA = "product4-p49-structural-route-report-2.0"
_INTERACTIVE_TEMPLATE_SOURCE_ID_MAX = (1 << 48) - 1


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _uid(flow_uuid: str, logical_id: str, role: str) -> str:
    return str(uuid5(UUID(flow_uuid), f"{logical_id}:{role}"))


def compile_glific(flow_spec: Any) -> dict[str, Any]:
    """Call only Product 2's direct Flow Spec compiler."""

    backend = str(PRODUCT2_BACKEND)
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.glific.compiler import compile_flow_spec

    result = compile_flow_spec(flow_spec)
    artifact = result.artifact
    return {
        "artifact": artifact,
        "canonical_hash": hashlib.sha256(_canonical_json(artifact).encode()).hexdigest(),
        "compilation_map": result.compilation_map,
        "metadata": result.metadata,
    }


def _model_dump(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else value


def _issue(issues: list[dict[str, str]], code: str, path: str, message: str) -> None:
    issues.append({"code": code, "path": path, "message": message})


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _exact_keys(value: Any, expected: set[str], issues: list[dict[str, str]], code: str, path: str) -> bool:
    if not isinstance(value, dict) or set(value) != expected:
        _issue(issues, code, path, f"Expected exactly {sorted(expected)}.")
        return False
    return True


def _generated_policy_node(node: dict[str, Any]) -> bool:
    refs = node.get("source_refs") or []
    return bool(refs) and all(
        isinstance(ref, dict)
        and str(ref.get("source_quote", "")).startswith(
            "Generated technical behavior: policy:product4-technical-policy-1.0:"
        )
        for ref in refs
    )


def _physical_layout(spec: dict[str, Any]) -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    physical: list[str] = []
    groups: dict[str, list[str]] = {}
    choice_values: dict[str, str] = {}
    for node in spec.get("nodes", []):
        node_id = node["id"]
        if node.get("type") in {"ask_choice", "ask_input"}:
            retry = node["retry"]
            attempts = [
                node_id,
                *[f"{node_id}:attempt:{attempt}" for attempt in range(2, retry["max_attempts"] + 1)],
            ]
            waits = [f"{logical_id}:wait" for logical_id in attempts]
            retries = [
                f"{node_id}:attempt:{attempt}:retry_message"
                for attempt in range(1, retry["max_attempts"])
                if retry["messages"]
            ]
            groups[node_id] = [item for pair in zip(attempts, waits) for item in pair] + retries
            physical.extend(groups[node_id])
        else:
            groups[node_id] = [node_id]
            physical.append(node_id)
    for node in spec.get("nodes", []):
        if node.get("type") != "ask_choice":
            continue
        for choice in node.get("choices", []):
            logical_id = f"{node['id']}:{choice['id']}:value"
            choice_values[choice["id"]] = logical_id
            physical.append(logical_id)
    return physical, groups, choice_values


def _interactive_payload(node: dict[str, Any]) -> dict[str, Any]:
    choices = node.get("choices", [])
    if node.get("presentation") == "quick_reply":
        return {
            "type": "quick_reply",
            "content": {
                "type": "text",
                "header": str(node.get("name", ""))[:60],
                "text": node["message"]["text"],
            },
            "options": [{"type": "text", "title": choice["title"]} for choice in choices],
        }
    return {
        "type": "list",
        "title": str(node.get("name", ""))[:60],
        "body": node["message"]["text"],
        "globalButtons": [{"type": "text", "title": "Choose"}],
        "items": [
            {
                "title": str(node.get("name", ""))[:60],
                "subtitle": "Choose one option",
                "options": [{"type": "text", "title": choice["title"]} for choice in choices],
            }
        ],
    }


def _interactive_template_source_id(flow_uuid: str, node_id: str) -> int:
    seed = f"{flow_uuid}\x00{node_id}".encode()
    digest_value = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
    return digest_value % _INTERACTIVE_TEMPLATE_SOURCE_ID_MAX + 1


def _interactive_template_label(flow_uuid: str, node_id: str) -> str:
    """Return the flow-scoped label used by Glific's global import lookup."""

    return f"product2_{flow_uuid}_{node_id}"


def _interactive_text_matches(text: Any, content: Any) -> bool:
    if not isinstance(text, str):
        return False
    try:
        return json.loads(text) == content
    except (TypeError, json.JSONDecodeError):
        return False


def _routes(flow_uuid: str, logical_id: str, targets: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    return {
        key: {"uuid": _uid(flow_uuid, f"{logical_id}:{key}", "exit"), "destination_uuid": destination}
        for key, destination in targets
    }


def _input_cases(node: dict[str, Any], logical_id: str, flow_uuid: str) -> list[tuple[str, list[str], str]]:
    validation = node.get("validation")
    parser = validation.get("parser") if validation else "plain_text"
    constraints = validation.get("constraints", {}) if validation else {}
    supported_input_parsers = {
        "text": {"plain_text"},
        "number": {"integer"},
        "time": {"local_12_hour_time"},
        "email": {"email"},
        "phone": {"phone"},
    }
    if parser not in supported_input_parsers.get(node.get("input_type"), set()):
        return [("__unsupported__", [node.get("input_type", ""), parser], "unsupported")]
    if parser == "plain_text" and not constraints:
        return []
    if parser == "plain_text" and set(constraints) == {"pattern"}:
        return [("has_pattern", [str(constraints["pattern"])], "valid")]
    if parser == "plain_text" and set(constraints) == {"allowed_values"}:
        return [
            ("has_only_phrase", [str(value)], f"valid_{index}")
            for index, value in enumerate(constraints["allowed_values"])
        ]
    if parser == "local_12_hour_time" and not constraints:
        return [("has_pattern", [r"^(0?[1-9]|1[0-2])(:[0-5][0-9])?\s?(AM|PM|am|pm)$"], "valid")]
    if parser == "email" and not constraints:
        return [("has_email", [], "valid")]
    if parser == "phone" and not constraints:
        return [("has_phone", [], "valid")]
    if parser == "integer" and not constraints:
        return [("has_pattern", [r"^-?[0-9]+$"], "valid")]
    if parser == "integer" and set(constraints) == {"minimum"} and constraints["minimum"] == 0:
        return [("has_pattern", [r"^(0|[1-9][0-9]*)$"], "valid")]
    return [("__unsupported__", [parser], "unsupported")]


def _expected_router(
    flow_uuid: str,
    node: dict[str, Any],
    logical_id: str,
    route_uuids: dict[str, str],
) -> dict[str, Any]:
    categories: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    node_type = node["type"]
    if node_type == "ask_choice":
        for choice in node["choices"]:
            category_uuid = _uid(flow_uuid, f"{logical_id}:{choice['id']}", "category")
            cases.append(
                {
                    "uuid": _uid(flow_uuid, f"{logical_id}:{choice['id']}", "case"),
                    "type": "has_only_phrase",
                    "arguments": [choice["title"]],
                    "category_uuid": category_uuid,
                }
            )
            categories.append(
                {"uuid": category_uuid, "name": choice["title"], "exit_uuid": route_uuids[choice["id"]]}
            )
        other_uuid = _uid(flow_uuid, logical_id, "other_category")
        no_response_uuid = _uid(flow_uuid, logical_id, "no_response_category")
        categories.extend(
            [
                {"uuid": other_uuid, "name": "Other", "exit_uuid": route_uuids["invalid"]},
                {"uuid": no_response_uuid, "name": "No Response", "exit_uuid": route_uuids["no_response"]},
            ]
        )
        return {
            "type": "switch",
            "operand": "@input.text",
            "result_name": node["save_as"],
            "wait": {
                "type": "msg",
                "timeout": {
                    "seconds": node["no_response"]["timeout_seconds"],
                    "category_uuid": no_response_uuid,
                    "expression": None,
                },
            },
            "cases": cases,
            "categories": categories,
            "default_category_uuid": other_uuid,
        }
    valid = _input_cases(node, logical_id, flow_uuid)
    for case_type, arguments, suffix in valid:
        category_uuid = _uid(flow_uuid, f"{logical_id}:{suffix}", "category")
        cases.append(
            {
                "uuid": _uid(flow_uuid, f"{logical_id}:{suffix}", "case"),
                "type": case_type,
                "arguments": arguments,
                "category_uuid": category_uuid,
            }
        )
        categories.append(
            {"uuid": category_uuid, "name": "Accepted", "exit_uuid": route_uuids["success"]}
        )
    other_uuid = _uid(flow_uuid, logical_id, "other_category")
    categories.append({"uuid": other_uuid, "name": "Other", "exit_uuid": route_uuids["invalid"]})
    no_response_uuid = _uid(flow_uuid, logical_id, "no_response_category")
    if node.get("no_response"):
        categories.append(
            {"uuid": no_response_uuid, "name": "No Response", "exit_uuid": route_uuids["no_response"]}
        )
    if not valid:
        accepted_uuid = _uid(flow_uuid, logical_id, "accepted_category")
        categories.insert(0, {"uuid": accepted_uuid, "name": "Accepted", "exit_uuid": route_uuids["success"]})
    return {
        "type": "switch",
        "operand": "@input.text",
        "result_name": node["save_as"],
        "wait": {
            "type": "msg",
            "timeout": {
                "seconds": node["no_response"]["timeout_seconds"],
                "category_uuid": no_response_uuid,
                "expression": None,
            },
        }
        if node.get("no_response")
        else {"type": "msg"},
        "cases": cases,
        "categories": categories,
        "default_category_uuid": other_uuid if valid else categories[0]["uuid"],
    }


def _expected_compilation_map(flow_uuid: str, spec: dict[str, Any]) -> dict[str, list[str]]:
    _, groups, choice_values = _physical_layout(spec)
    expected = {
        node_id: [_uid(flow_uuid, logical_id, "node") for logical_id in logicals]
        for node_id, logicals in groups.items()
    }
    for variable in spec.get("variables", []):
        expected[variable["name"]] = [_uid(flow_uuid, variable["name"], "variable")]
    for resource in spec.get("resources", []):
        expected[resource["logical_name"]] = [_uid(flow_uuid, resource["logical_name"], "resource")]
    for node in spec.get("nodes", []):
        if node.get("type") != "ask_choice":
            continue
        for choice in node["choices"]:
            logical_id = choice_values[choice["id"]]
            expected[choice["id"]] = [
                _uid(flow_uuid, f"{node['id']}:{choice['id']}", "option"),
                _uid(flow_uuid, logical_id, "node"),
                _uid(flow_uuid, f"{logical_id}:next", "exit"),
            ]
    return expected


def _validate_generic_artifact(
    artifact: dict[str, Any], issues: list[dict[str, str]]
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, str], dict[str, str]]:
    flows = artifact.get("flows")
    first_flow = flows[0] if isinstance(flows, list) and flows and isinstance(flows[0], dict) else {}
    definition = first_flow.get("definition")
    compiled_nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
    compiled_by_uuid: dict[str, dict[str, Any]] = {}
    exit_owner: dict[str, str] = {}
    category_owner: dict[str, str] = {}
    case_owner: dict[str, str] = {}
    seen_nodes: set[str] = set()
    seen_actions: set[str] = set()
    seen_exits: set[str] = set()
    seen_categories: set[str] = set()
    seen_cases: set[str] = set()
    for node_index, node in enumerate(compiled_nodes if isinstance(compiled_nodes, list) else []):
        path = f"/artifact/flows/0/definition/nodes/{node_index}"
        if not isinstance(node, dict):
            _issue(issues, "P4_E3_NODE_INVALID", path, "Compiled node must be an object.")
            continue
        node_uuid = node.get("uuid")
        if not _is_uuid(node_uuid):
            _issue(issues, "P4_E3_NODE_UUID_INVALID", f"{path}/uuid", "Node UUID is not canonical.")
        elif node_uuid in seen_nodes:
            _issue(issues, "P4_E3_NODE_UUID_INVALID", f"{path}/uuid", "Node UUID is duplicated.")
        else:
            seen_nodes.add(node_uuid)
            compiled_by_uuid[node_uuid] = node
        expected_keys = {"uuid", "actions", "exits"} | ({"router"} if "router" in node else set())
        _exact_keys(node, expected_keys, issues, "P4_E3_NODE_KEYS_INVALID", path)
        actions = node.get("actions", [])
        if not isinstance(actions, list):
            _issue(issues, "P4_E3_ACTIONS_INVALID", f"{path}/actions", "Actions must be a list.")
            actions = []
        for action_index, action in enumerate(actions):
            action_path = f"{path}/actions/{action_index}"
            if not isinstance(action, dict):
                _issue(issues, "P4_E3_ACTION_INVALID", action_path, "Action must be an object.")
                continue
            action_type = action.get("type")
            expected_action_keys = _ACTION_KEYS.get(action_type)
            if expected_action_keys is None:
                _issue(issues, "P4_E3_ACTION_TYPE_INVALID", f"{action_path}/type", "Action type is not consumed by pinned Glific.")
            else:
                _exact_keys(action, expected_action_keys, issues, "P4_E3_ACTION_KEYS_INVALID", action_path)
            action_uuid = action.get("uuid")
            if not _is_uuid(action_uuid):
                _issue(issues, "P4_E3_ACTION_UUID_INVALID", f"{action_path}/uuid", "Action UUID is not canonical.")
            elif action_uuid in seen_actions:
                _issue(issues, "P4_E3_DUPLICATE_ACTION_UUID", f"{action_path}/uuid", "Action UUID is duplicated.")
            else:
                seen_actions.add(action_uuid)
        exits = node.get("exits", [])
        if not isinstance(exits, list):
            _issue(issues, "P4_E3_EXITS_INVALID", f"{path}/exits", "Exits must be a list.")
            exits = []
        if actions and "router" in node:
            _issue(
                issues,
                "P4_E3_PROMPT_ACTION_ROUTER_INVALID",
                path,
                "Five-capability prompt/action nodes cannot combine actions with a router.",
            )
        elif actions and len(exits) != 1:
            _issue(
                issues,
                "P4_E3_ACTION_EXIT_COUNT_INVALID",
                f"{path}/exits",
                "Every executable action node must have exactly one exit.",
            )
        elif not actions and "router" not in node:
            _issue(
                issues,
                "P4_E3_UNSUPPORTED_NODE_SHAPE",
                path,
                "Every compiled node must be executable or router-only.",
            )
        for exit_index, exit_item in enumerate(exits):
            exit_path = f"{path}/exits/{exit_index}"
            if not isinstance(exit_item, dict):
                _issue(issues, "P4_E3_EXIT_INVALID", exit_path, "Exit must be an object.")
                continue
            _exact_keys(exit_item, _EXIT_KEYS, issues, "P4_E3_EXIT_KEYS_INVALID", exit_path)
            exit_uuid = exit_item.get("uuid")
            if not _is_uuid(exit_uuid):
                _issue(issues, "P4_E3_EXIT_UUID_INVALID", f"{exit_path}/uuid", "Exit UUID is not canonical.")
            elif exit_uuid in seen_exits:
                _issue(issues, "P4_E3_DUPLICATE_EXIT_UUID", f"{exit_path}/uuid", "Exit UUID is duplicated.")
            else:
                seen_exits.add(exit_uuid)
                if _is_uuid(node_uuid):
                    exit_owner[exit_uuid] = node_uuid
        router = node.get("router")
        if not isinstance(router, dict):
            continue
        _exact_keys(router, _ROUTER_KEYS, issues, "P4_E3_ROUTER_KEYS_INVALID", f"{path}/router")
        if router.get("type") != "switch" or not isinstance(router.get("operand"), str) or not isinstance(router.get("result_name"), str):
            _issue(issues, "P4_E3_ROUTER_VALUE_INVALID", f"{path}/router", "Router type/value fields are invalid.")
        wait = router.get("wait")
        if not isinstance(wait, dict) or wait.get("type") not in {"msg", "none"}:
            _issue(issues, "P4_E3_WAIT_INVALID", f"{path}/router/wait", "Wait type is not official.")
        elif wait.get("type") == "none":
            _exact_keys(wait, {"type"}, issues, "P4_E3_WAIT_KEYS_INVALID", f"{path}/router/wait")
        elif "timeout" in wait:
            _exact_keys(wait, {"type", "timeout"}, issues, "P4_E3_WAIT_KEYS_INVALID", f"{path}/router/wait")
            timeout = wait.get("timeout")
            _exact_keys(timeout, {"seconds", "category_uuid", "expression"}, issues, "P4_E3_WAIT_TIMEOUT_INVALID", f"{path}/router/wait/timeout")
            if isinstance(timeout, dict) and (not isinstance(timeout.get("seconds"), int) or timeout.get("seconds") <= 0):
                _issue(issues, "P4_E3_WAIT_TIMEOUT_INVALID", f"{path}/router/wait/timeout/seconds", "Timeout seconds must be positive.")
        else:
            _exact_keys(wait, {"type"}, issues, "P4_E3_WAIT_KEYS_INVALID", f"{path}/router/wait")
        categories = router.get("categories", [])
        for category_index, category in enumerate(categories if isinstance(categories, list) else []):
            category_path = f"{path}/router/categories/{category_index}"
            if not isinstance(category, dict):
                _issue(issues, "P4_E3_CATEGORY_INVALID", category_path, "Category must be an object.")
                continue
            _exact_keys(category, _CATEGORY_KEYS, issues, "P4_E3_CATEGORY_KEYS_INVALID", category_path)
            category_uuid = category.get("uuid")
            if not _is_uuid(category_uuid):
                _issue(issues, "P4_E3_CATEGORY_UUID_INVALID", f"{category_path}/uuid", "Category UUID is not canonical.")
            elif category_uuid in seen_categories:
                _issue(issues, "P4_E3_DUPLICATE_CATEGORY_UUID", f"{category_path}/uuid", "Category UUID is duplicated.")
            else:
                seen_categories.add(category_uuid)
                if _is_uuid(node_uuid):
                    category_owner[category_uuid] = node_uuid
        for case_index, case in enumerate(router.get("cases", []) if isinstance(router.get("cases"), list) else []):
            case_path = f"{path}/router/cases/{case_index}"
            if not isinstance(case, dict):
                _issue(issues, "P4_E3_CASE_INVALID", case_path, "Case must be an object.")
                continue
            _exact_keys(case, _CASE_KEYS, issues, "P4_E3_CASE_KEYS_INVALID", case_path)
            if case.get("type") not in _CASE_TYPES:
                _issue(issues, "P4_E3_CASE_TYPE_INVALID", f"{case_path}/type", "Case type is not official.")
            if not isinstance(case.get("arguments"), list):
                _issue(issues, "P4_E3_CASE_ARGUMENTS_INVALID", f"{case_path}/arguments", "Case arguments must be a list.")
            case_uuid = case.get("uuid")
            if not _is_uuid(case_uuid):
                _issue(issues, "P4_E3_CASE_UUID_INVALID", f"{case_path}/uuid", "Case UUID is not canonical.")
            elif case_uuid in seen_cases:
                _issue(issues, "P4_E3_DUPLICATE_CASE_UUID", f"{case_path}/uuid", "Case UUID is duplicated.")
            else:
                seen_cases.add(case_uuid)
                if _is_uuid(node_uuid):
                    case_owner[case_uuid] = node_uuid
    for node_index, node in enumerate(compiled_nodes if isinstance(compiled_nodes, list) else []):
        if not isinstance(node, dict) or not isinstance(node.get("router"), dict):
            continue
        path = f"/artifact/flows/0/definition/nodes/{node_index}/router"
        node_uuid = node.get("uuid")
        exits = node.get("exits", [])
        if not isinstance(exits, list):
            exits = []
        own_exits = {
            item.get("uuid")
            for item in exits
            if isinstance(item, dict) and isinstance(item.get("uuid"), str)
        }
        categories = node["router"].get("categories", [])
        if not isinstance(categories, list):
            categories = []
        own_categories = {
            item.get("uuid")
            for item in categories
            if isinstance(item, dict) and isinstance(item.get("uuid"), str)
        }
        cases = node["router"].get("cases", [])
        if not isinstance(cases, list):
            cases = []
        for category_index, category in enumerate(categories):
            if not isinstance(category, dict):
                continue
            category_uuid = category.get("uuid")
            if not isinstance(category_uuid, str) or category_uuid not in own_categories or category_owner.get(category_uuid) != node_uuid:
                _issue(issues, "P4_E3_ROUTER_CATEGORY_OWNERSHIP_INVALID", f"{path}/categories/{category_index}", "Category is not owned by its router node.")
            exit_uuid = category.get("exit_uuid")
            if not isinstance(exit_uuid, str) or exit_uuid not in own_exits or exit_owner.get(exit_uuid) != node_uuid:
                _issue(issues, "P4_E3_ROUTER_EXIT_OWNERSHIP_INVALID", f"{path}/categories/{category_index}/exit_uuid", "Category references another node's exit.")
        for case_index, case in enumerate(cases):
            if not isinstance(case, dict):
                continue
            case_uuid = case.get("uuid")
            if not isinstance(case_uuid, str) or case_uuid not in case_owner or case_owner.get(case_uuid) != node_uuid:
                _issue(issues, "P4_E3_ROUTER_CASE_OWNERSHIP_INVALID", f"{path}/cases/{case_index}", "Case is not owned by its router node.")
            category_uuid = case.get("category_uuid")
            if not isinstance(category_uuid, str) or category_uuid not in own_categories or category_owner.get(category_uuid) != node_uuid:
                _issue(issues, "P4_E3_ROUTER_CATEGORY_OWNERSHIP_INVALID", f"{path}/cases/{case_index}/category_uuid", "Case references another node's category.")
        default_uuid = node["router"].get("default_category_uuid")
        if not isinstance(default_uuid, str) or default_uuid not in own_categories or category_owner.get(default_uuid) != node_uuid:
            _issue(issues, "P4_E3_ROUTER_CATEGORY_OWNERSHIP_INVALID", f"{path}/default_category_uuid", "Default category is not owned by its node.")
    return compiled_by_uuid, exit_owner, category_owner, case_owner


def _validate_interactive_templates(
    artifact: dict[str, Any],
    compiled_by_uuid: dict[str, dict[str, Any]],
    issues: list[dict[str, str]],
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    templates = artifact.get("interactive_templates", [])
    templates_by_source_id: dict[int, list[dict[str, Any]]] = {}
    labels: set[str] = set()
    wrapper_templates: list[dict[str, Any]] = []
    if not isinstance(templates, list):
        return templates_by_source_id, {
            "wrapper_templates": wrapper_templates,
            "action_bindings": [],
            "all_action_ids_resolve": False,
            "all_action_content_matches": False,
            "unique_source_ids": False,
            "unique_labels": False,
        }

    unique_source_ids = True
    unique_labels = True
    for template_index, template in enumerate(templates):
        path = f"/artifact/interactive_templates/{template_index}"
        if not _exact_keys(template, _INTERACTIVE_TEMPLATE_KEYS, issues, "P4_E3_INTERACTIVE_TEMPLATE_KEYS_INVALID", path):
            continue
        source_id = template.get("source_id")
        if (
            not isinstance(source_id, int)
            or isinstance(source_id, bool)
            or source_id <= 0
            or source_id > _INTERACTIVE_TEMPLATE_SOURCE_ID_MAX
        ):
            _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_SOURCE_ID_INVALID", f"{path}/source_id", "Interactive template source_id must be a positive 48-bit integer.")
        else:
            matches = templates_by_source_id.setdefault(source_id, [])
            if matches:
                unique_source_ids = False
                _issue(issues, "P4_E3_DUPLICATE_INTERACTIVE_TEMPLATE_SOURCE_ID", f"{path}/source_id", "Interactive template source_id is ambiguous.")
            matches.append(template)
        label = template.get("label")
        if not isinstance(label, str) or not label:
            _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_LABEL_INVALID", f"{path}/label", "Interactive template label must be non-empty.")
        elif label in labels:
            unique_labels = False
            _issue(issues, "P4_E3_DUPLICATE_INTERACTIVE_TEMPLATE_LABEL", f"{path}/label", "Interactive template label is ambiguous.")
        else:
            labels.add(label)
        if not isinstance(template.get("type"), str) or not isinstance(template.get("interactive_content"), dict):
            _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_CONTENT_INVALID", path, "Interactive template type/content is malformed.")
        if not isinstance(template.get("translations"), dict):
            _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_TRANSLATIONS_INVALID", f"{path}/translations", "Interactive template translations must be an object.")
        if not isinstance(template.get("language_id"), int) or isinstance(template.get("language_id"), bool):
            _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_LANGUAGE_INVALID", f"{path}/language_id", "Interactive template language_id must be an integer.")
        if not isinstance(template.get("send_with_title"), bool):
            _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_SEND_WITH_TITLE_INVALID", f"{path}/send_with_title", "Interactive template send_with_title must be boolean.")
        wrapper_templates.append(
            {
                "source_id": source_id,
                "label": label,
                "type": template.get("type"),
                "interactive_content": template.get("interactive_content"),
            }
        )

    action_bindings: list[dict[str, Any]] = []
    all_action_ids_resolve = True
    all_action_content_matches = True
    for node_uuid, node in compiled_by_uuid.items():
        for action_index, action in enumerate(node.get("actions", [])):
            if not isinstance(action, dict) or action.get("type") != "send_interactive_msg":
                continue
            action_id = action.get("id")
            matches = (
                templates_by_source_id.get(action_id, [])
                if isinstance(action_id, int) and not isinstance(action_id, bool)
                else []
            )
            resolved = len(matches) == 1
            content_matches = resolved and _interactive_text_matches(
                action.get("text"), matches[0]["interactive_content"]
            )
            if not resolved:
                all_action_ids_resolve = False
                _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_REFERENCE_INVALID", f"/artifact/flows/0/definition/nodes/{node_uuid}/actions/{action_index}/id", "Interactive action id must resolve to exactly one wrapper source_id.")
            if resolved and not content_matches:
                all_action_content_matches = False
                _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_CONTENT_INVALID", f"/artifact/flows/0/definition/nodes/{node_uuid}/actions/{action_index}/text", "Interactive action content does not belong to its wrapper template.")
            action_bindings.append(
                {
                    "node_uuid": node_uuid,
                    "action_uuid": action.get("uuid"),
                    "action_id": action_id,
                    "resolved": resolved,
                    "resolved_template_source_id": action_id if resolved else None,
                    "template_label": matches[0].get("label") if resolved else None,
                    "content_matches": content_matches,
                }
            )
    return templates_by_source_id, {
        "wrapper_templates": wrapper_templates,
        "action_bindings": action_bindings,
        "all_action_ids_resolve": all_action_ids_resolve,
        "all_action_content_matches": all_action_content_matches,
        "unique_source_ids": unique_source_ids,
        "unique_labels": unique_labels,
    }


def _source_route_report(
    spec: dict[str, Any], result: dict[str, Any], compiled_by_uuid: dict[str, dict[str, Any]], issues: list[dict[str, str]]
) -> dict[str, Any]:
    flow_uuid = spec["flow"]["id"]
    _, groups, choice_values = _physical_layout(spec)
    route_checks: list[dict[str, Any]] = []
    technical_checks: list[dict[str, Any]] = []
    for node in spec.get("nodes", []):
        node_id = node["id"]
        if node["type"] in {"ask_choice", "ask_input"}:
            attempts = [
                node_id,
                *[f"{node_id}:attempt:{attempt}" for attempt in range(2, node["retry"]["max_attempts"] + 1)],
            ]
            waits = [f"{logical_id}:wait" for logical_id in attempts]
            for index, logical_id in enumerate(waits, start=1):
                compiled = compiled_by_uuid.get(_uid(flow_uuid, logical_id, "node"), {})
                exits = {item.get("uuid"): item.get("destination_uuid") for item in compiled.get("exits", []) if isinstance(item, dict)}
                invalid_exit_uuid = _uid(flow_uuid, f"{logical_id}:invalid", "exit")
                no_response_exit_uuid = _uid(flow_uuid, f"{logical_id}:no_response", "exit")
                invalid_destination = exits.get(invalid_exit_uuid)
                no_response_destination = exits.get(no_response_exit_uuid)
                technical_checks.append(
                    {
                        "node_id": node_id,
                        "attempt": index,
                        "route": "invalid",
                        "destination_uuid": invalid_destination,
                        "generated_policy_terminal": bool(
                            invalid_destination
                            and any(
                                invalid_destination == _uid(flow_uuid, item["id"], "node") and _generated_policy_node(item)
                                for item in spec.get("nodes", [])
                            )
                        ),
                        "executable_retry_hop": invalid_destination is not None,
                    }
                )
                technical_checks.append(
                    {
                        "node_id": node_id,
                        "attempt": index,
                        "route": "no_response",
                        "destination_uuid": no_response_destination,
                        "generated_policy_terminal": bool(
                            no_response_destination
                            and any(
                                no_response_destination == _uid(flow_uuid, item["id"], "node") and _generated_policy_node(item)
                                for item in spec.get("nodes", [])
                            )
                        ),
                        "executable_retry_hop": no_response_destination is not None,
                    }
                )
            if node["type"] == "ask_choice":
                authored = {
                    choice["id"]: _uid(flow_uuid, choice_values[choice["id"]], "node")
                    for choice in node["choices"]
                }
                choice_bindings = [
                    {
                        "choice_id": choice["id"],
                        "title": choice["title"],
                        "submitted_value": choice["submitted_value"],
                        "runtime_matcher": choice["title"],
                        "destination_node_id": choice["next_node_id"],
                        "destination_uuid": authored[choice["id"]],
                    }
                    for choice in node["choices"]
                ]
                route_checks.append(
                    {
                        "node_id": node_id,
                        "node_type": node["type"],
                        "authored_routes": authored,
                        "technical_routes": {
                            "invalid": _uid(flow_uuid, waits[0] + ":invalid", "exit"),
                            "no_response": _uid(flow_uuid, waits[0] + ":no_response", "exit"),
                        },
                        "choice_bindings": choice_bindings,
                        "retry": node["retry"],
                        "no_response_timeout_seconds": node["no_response"]["timeout_seconds"],
                    }
                )
            else:
                route_checks.append(
                    {
                        "node_id": node_id,
                        "node_type": node["type"],
                        "authored_routes": {"success": _uid(flow_uuid, node["next_node_id"], "node")},
                        "technical_routes": {
                            "invalid": _uid(flow_uuid, waits[0] + ":invalid", "exit"),
                            **({"no_response": _uid(flow_uuid, waits[0] + ":no_response", "exit")} if node.get("no_response") else {}),
                        },
                        "capture": {
                            "prompt": node["message"]["text"],
                            "result_variable": node["save_as"],
                            "input_type": node["input_type"],
                            "parser": node.get("validation", {}).get("parser") if node.get("validation") else None,
                            "constraints": node.get("validation", {}).get("constraints", {}) if node.get("validation") else {},
                            "retry": node["retry"],
                            "no_response_timeout_seconds": node["no_response"]["timeout_seconds"] if node.get("no_response") else None,
                        },
                    }
                )
        elif node["type"] == "record_request":
            route_checks.append(
                {
                    "node_id": node_id,
                    "node_type": node["type"],
                    "authored_routes": {"success": _uid(flow_uuid, node["success_node_id"], "node")},
                    "technical_routes": {},
                    "persistence": {
                        "mechanism": node["mechanism"],
                        "resource_ref": node["resource_ref"],
                        "fields": node["fields"],
                        "success_node_id": node["success_node_id"],
                        "failure_route_emitted": len(compiled_by_uuid.get(_uid(flow_uuid, node_id, "node"), {}).get("exits", [])) > 1,
                        "failure_handling": "external_glific_action_error",
                    },
                }
            )
    terminal_reasons = []
    for node in spec.get("nodes", []):
        if node.get("type") != "end":
            continue
        compiled = compiled_by_uuid.get(_uid(flow_uuid, node["id"], "node"), {})
        terminal_reasons.append(
            {
                "node_id": node["id"],
                "reason": node["reason"],
                "generated_policy": _generated_policy_node(node),
                "compiled_exit_count": len(compiled.get("exits", [])),
                "compiled_action_types": [action.get("type") for action in compiled.get("actions", [])],
                "null_destination_exit": len(compiled.get("exits", [])) == 1
                and compiled.get("exits", [])[0].get("destination_uuid") is None,
            }
        )
        if (
            len(compiled.get("actions", [])) != 1
            or compiled.get("actions", [{}])[0].get("type") != "set_run_result"
            or len(compiled.get("exits", [])) != 1
            or compiled.get("exits", [{}])[0].get("destination_uuid") is not None
        ):
            _issue(issues, "P4_E3_TERMINAL_EXECUTION_INVALID", f"/nodes/{node['id']}", "End node must execute set_run_result and then a null exit.")
    return {
        "route_checks": route_checks,
        "technical_route_checks": technical_checks,
        "terminal_reasons": terminal_reasons,
        "all_terminal_exits_null": all(item["null_destination_exit"] for item in terminal_reasons),
        "technical_behavior_policy_version": "product4-technical-policy-1.0",
    }


def build_glific_structural_report(result: dict[str, Any], flow_spec: Any | None = None) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
        _issue(issues, "P4_E3_RESULT_KEYS_INVALID", "/", "Compilation result keys are not exact.")
    artifact = result.get("artifact") if isinstance(result, dict) else None
    if not isinstance(artifact, dict):
        _issue(issues, "P4_E3_ARTIFACT_INVALID", "/artifact", "Artifact must be an object.")
        artifact = {}
    canonical_hash = result.get("canonical_hash") if isinstance(result, dict) else None
    expected_hash = hashlib.sha256(_canonical_json(artifact).encode()).hexdigest()
    if canonical_hash != expected_hash:
        _issue(issues, "P4_E3_ARTIFACT_HASH_MISMATCH", "/canonical_hash", "Canonical artifact hash is incorrect.")
    wrapper = _exact_keys(artifact, _ARTIFACT_KEYS, issues, "P4_E3_ARTIFACT_WRAPPER_INVALID", "/artifact")
    flows = artifact.get("flows", [])
    if not isinstance(flows, list) or len(flows) != 1 or not isinstance(flows[0], dict):
        _issue(issues, "P4_E3_FLOW_INVALID", "/artifact/flows", "Exactly one flow wrapper is required.")
        flow_wrapper: dict[str, Any] = {}
    else:
        flow_wrapper = flows[0]
    _exact_keys(flow_wrapper, _FLOW_KEYS, issues, "P4_E3_FLOW_WRAPPER_INVALID", "/artifact/flows/0")
    definition = flow_wrapper.get("definition", {})
    definition_valid = _exact_keys(definition, _DEFINITION_KEYS, issues, "P4_E3_DEFINITION_INVALID", "/artifact/flows/0/definition")
    if isinstance(definition, dict) and definition.get("spec_version") != "13.1.0":
        _issue(issues, "P4_E3_DEFINITION_VERSION_INVALID", "/artifact/flows/0/definition/spec_version", "Definition must use Glific spec 13.1.0.")
    if not isinstance(artifact.get("contact_field"), list) or not isinstance(artifact.get("collections"), list) or not isinstance(artifact.get("interactive_templates"), list):
        _issue(issues, "P4_E3_WRAPPER_COLLECTION_INVALID", "/artifact", "Wrapper collections must be lists.")
    compiled_by_uuid, _, _, _ = _validate_generic_artifact(artifact, issues)
    templates_by_source_id, interactive_template_checks = _validate_interactive_templates(
        artifact, compiled_by_uuid, issues
    )
    node_uuids = set(compiled_by_uuid)
    for node in compiled_by_uuid.values():
        for exit_item in node.get("exits", []):
            destination = exit_item.get("destination_uuid") if isinstance(exit_item, dict) else None
            if destination is not None and destination not in node_uuids:
                _issue(issues, "P4_E3_DANGLING_DESTINATION", "/artifact/flows/0/definition/nodes", "Every non-null destination must resolve to a compiled node.")

    route_report = {
        "route_checks": [],
        "technical_route_checks": [],
        "terminal_reasons": [],
        "all_terminal_exits_null": False,
        "technical_behavior_policy_version": "product4-technical-policy-1.0",
        "interactive_template_checks": interactive_template_checks,
        "contact_field_wrapper_audit": {
            "wrapper_key_present": isinstance(artifact.get("contact_field"), list),
            "entries_required_for_import": False,
            "runtime_set_contact_field_creates_missing_field": True,
            "compiler_emits_empty_entries": artifact.get("contact_field") == [],
            "official_import_behavior": "import_contact_field enumerates the optional entries; native set_contact_field creates missing fields at runtime",
        },
    }
    if flow_spec is not None:
        spec = _model_dump(flow_spec)
        flow_uuid = spec.get("flow", {}).get("id")
        if not isinstance(flow_uuid, str) or not _is_uuid(flow_uuid):
            _issue(issues, "P4_E3_FLOW_ID_INVALID", "/flow/id", "Flow ID must be a UUID.")
        else:
            expected_map = _expected_compilation_map(flow_uuid, spec)
            if result.get("compilation_map") != expected_map:
                _issue(issues, "P4_E3_COMPILATION_MAP_INVALID", "/compilation_map", "Compilation map does not preserve deterministic expanded lineage.")
            physical, groups, choice_values = _physical_layout(spec)
            expected_uuids = {_uid(flow_uuid, logical_id, "node") for logical_id in physical}
            if set(compiled_by_uuid) != expected_uuids:
                _issue(issues, "P4_E3_NODE_MAP_MISMATCH", "/compilation_map", "Flow Spec IDs do not map to the exact compiled UUID set.")
            for node in spec.get("nodes", []):
                node_id = node["id"]
                if node.get("type") in {"ask_choice", "ask_input"}:
                    attempts = [
                        node_id,
                        *[f"{node_id}:attempt:{attempt}" for attempt in range(2, node["retry"]["max_attempts"] + 1)],
                    ]
                    waits = [f"{logical_id}:wait" for logical_id in attempts]
                    retry_nodes = [
                        f"{node_id}:attempt:{attempt}:retry_message"
                        for attempt in range(1, node["retry"]["max_attempts"])
                        if node["retry"]["messages"]
                    ]
                    for index, (prompt_logical, wait_logical) in enumerate(zip(attempts, waits), start=1):
                        is_last = index == len(attempts)
                        retry_destination = (
                            _uid(flow_uuid, retry_nodes[index - 1], "node")
                            if not is_last and retry_nodes
                            else (_uid(flow_uuid, attempts[index], "node") if not is_last else _uid(flow_uuid, node["retry"]["on_exhausted_node_id"], "node"))
                        )
                        targets: list[tuple[str, str]] = []
                        if node["type"] == "ask_choice":
                            targets.extend((choice["id"], _uid(flow_uuid, choice_values[choice["id"]], "node")) for choice in node["choices"])
                            targets.extend((("invalid", retry_destination), ("no_response", _uid(flow_uuid, node["no_response"]["next_node_id"], "node"))))
                        else:
                            targets.extend((("success", _uid(flow_uuid, node["next_node_id"], "node")), ("invalid", retry_destination)))
                            if node.get("no_response"):
                                targets.append(("no_response", _uid(flow_uuid, node["no_response"]["next_node_id"], "node")))
                        expected_routes = _routes(flow_uuid, wait_logical, targets)
                        authored_destinations = {
                            destination
                            for route_key, destination in targets
                            if route_key not in {"invalid", "no_response"}
                        }
                        for technical_key in ("invalid", "no_response"):
                            if (
                                technical_key in expected_routes
                                and expected_routes[technical_key]["destination_uuid"]
                                in authored_destinations
                            ):
                                _issue(issues, "P4_E3_TECHNICAL_ROUTE_ALIAS", f"/nodes/{node_id}/attempt/{index}/{technical_key}", "Technical route aliases an authored destination.")
                        prompt = compiled_by_uuid.get(_uid(flow_uuid, prompt_logical, "node"), {})
                        wait = compiled_by_uuid.get(_uid(flow_uuid, wait_logical, "node"), {})
                        if node["type"] == "ask_choice":
                            action_uuid = _uid(flow_uuid, prompt_logical, "action")
                            expected_action = {
                                "id": _interactive_template_source_id(flow_uuid, node["id"]),
                                "name": f"product2_{node['id']}_attempt_{index}",
                                "text": json.dumps(
                                    _interactive_payload(node),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                "type": "send_interactive_msg",
                                "uuid": action_uuid,
                            }
                        else:
                            expected_action = {
                                "uuid": _uid(flow_uuid, prompt_logical, "action"),
                                "type": "send_msg",
                                "text": node["message"]["text"],
                                "quick_replies": [],
                                "labels": [],
                                "attachments": [],
                            }
                        if prompt.get("actions") != [expected_action]:
                            _issue(issues, "P4_E3_ACTION_MISMATCH", f"/nodes/{node_id}/attempt/{index}/actions", "Prompt action copy or lineage changed.")
                        if node["type"] == "ask_choice":
                            prompt_actions = prompt.get("actions") if isinstance(prompt.get("actions"), list) else []
                            actual_action_id = prompt_actions[0].get("id") if prompt_actions and isinstance(prompt_actions[0], dict) else None
                            expected_template_source_id = _interactive_template_source_id(flow_uuid, node_id)
                            if actual_action_id != expected_template_source_id:
                                _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_OWNER_INVALID", f"/nodes/{node_id}/attempt/{index}/actions/0/id", "Interactive action id is not the source_id for its authored choice template.")
                        expected_prompt_route = _routes(flow_uuid, prompt_logical, [("wait", _uid(flow_uuid, wait_logical, "node"))])
                        if prompt.get("exits") != list(expected_prompt_route.values()) or "router" in prompt:
                            _issue(issues, "P4_E3_PROMPT_WAIT_LINK_INVALID", f"/nodes/{node_id}/attempt/{index}", "Prompt must have one exit to its owned router-only wait node.")
                        if wait.get("actions") != [] or wait.get("exits") != list(expected_routes.values()):
                            _issue(issues, "P4_E3_ROUTE_MISMATCH", f"/nodes/{node_id}/attempt/{index}/wait/exits", "Wait exits do not preserve intended destinations.")
                        expected_router = _expected_router(flow_uuid, node, wait_logical, {key: item["uuid"] for key, item in expected_routes.items()})
                        if wait.get("router") != expected_router:
                            _issue(issues, "P4_E3_ROUTER_MISMATCH", f"/nodes/{node_id}/attempt/{index}/wait/router", "Router does not match official cases/categories/wait timeout.")
                    for index, logical_id in enumerate(retry_nodes, start=1):
                        compiled = compiled_by_uuid.get(_uid(flow_uuid, logical_id, "node"), {})
                        expected_action = {"uuid": _uid(flow_uuid, logical_id, "action"), "type": "send_msg", "text": node["retry"]["messages"][min(index - 1, len(node["retry"]["messages"]) - 1)], "quick_replies": [], "labels": [], "attachments": []}
                        expected_exit = {"uuid": _uid(flow_uuid, f"{logical_id}:next", "exit"), "destination_uuid": _uid(flow_uuid, attempts[index], "node")}
                        if compiled.get("actions") != [expected_action] or compiled.get("exits") != [expected_exit]:
                            _issue(issues, "P4_E3_RETRY_GRAPH_MISMATCH", f"/nodes/{node_id}/retry/{index}", "Retry copy is not an executable Glific node.")
                elif node.get("type") == "send_message":
                    compiled = compiled_by_uuid.get(_uid(flow_uuid, node_id, "node"), {})
                    if compiled.get("actions", [{}])[0].get("text") != node["message"]["text"]:
                        _issue(issues, "P4_E3_ACTION_MISMATCH", f"/nodes/{node_id}/actions", "Message copy changed.")
                    expected = _routes(flow_uuid, node_id, [("next", _uid(flow_uuid, node["next_node_id"], "node"))])
                    if compiled.get("exits") != list(expected.values()):
                        _issue(issues, "P4_E3_ROUTE_MISMATCH", f"/nodes/{node_id}/exits", "Authored next route changed.")
                elif node.get("type") == "record_request":
                    compiled = compiled_by_uuid.get(_uid(flow_uuid, node_id, "node"), {})
                    expected_actions = []
                    for field_name, expression in sorted(node["fields"].items()):
                        value = expression
                        if isinstance(expression, str) and expression.startswith("{{") and expression.endswith("}}"):
                            value = "@results." + expression[2:-2].strip()
                        expected_actions.append({"uuid": _uid(flow_uuid, f"{node_id}:{field_name}", "set_contact_field"), "type": "set_contact_field", "value": value, "field": {"name": field_name, "key": field_name}})
                    expected = _routes(flow_uuid, node_id, [("success", _uid(flow_uuid, node["success_node_id"], "node"))])
                    if compiled.get("actions") != expected_actions or compiled.get("exits") != list(expected.values()):
                        _issue(issues, "P4_E3_PERSISTENCE_MISMATCH", f"/nodes/{node_id}", "Persistence must be native set_contact_field with one authored success exit.")
                elif node.get("type") == "end":
                    compiled = compiled_by_uuid.get(_uid(flow_uuid, node_id, "node"), {})
                    expected_action = {
                        "uuid": _uid(flow_uuid, node_id, "set_run_result"),
                        "type": "set_run_result",
                        "name": "terminal_reason",
                        "value": node["reason"],
                        "category": node["reason"],
                    }
                    expected_exit = {
                        "uuid": _uid(flow_uuid, f"{node_id}:terminal", "exit"),
                        "destination_uuid": None,
                    }
                    if compiled.get("actions") != [expected_action] or compiled.get("exits") != [expected_exit] or "router" in compiled:
                        _issue(issues, "P4_E3_TERMINAL_EXECUTION_INVALID", f"/nodes/{node_id}", "Terminal must execute set_run_result and then a null exit.")
            for node in spec.get("nodes", []):
                if node.get("type") != "ask_choice":
                    continue
                for choice in node["choices"]:
                    logical_id = choice_values[choice["id"]]
                    compiled = compiled_by_uuid.get(_uid(flow_uuid, logical_id, "node"), {})
                    expected_action = {"uuid": _uid(flow_uuid, f"{node['id']}:{choice['id']}", "set_run_result"), "type": "set_run_result", "name": node["save_as"], "value": choice["submitted_value"], "category": choice["title"]}
                    expected_exit = {"uuid": _uid(flow_uuid, f"{logical_id}:next", "exit"), "destination_uuid": _uid(flow_uuid, choice["next_node_id"], "node")}
                    if compiled.get("actions") != [expected_action] or compiled.get("exits") != [expected_exit]:
                        _issue(issues, "P4_E3_STABLE_VALUE_MISMATCH", f"/compilation_map/{choice['id']}", "Choice stable value is not written by set_run_result.")
            expected_templates: dict[int, dict[str, Any]] = {}
            for node in spec.get("nodes", []):
                if node.get("type") != "ask_choice":
                    continue
                source_id = _interactive_template_source_id(flow_uuid, node["id"])
                expected_templates[source_id] = {
                    "source_id": source_id,
                    "label": _interactive_template_label(flow_uuid, node["id"]),
                    "type": node["presentation"],
                    "interactive_content": _interactive_payload(node),
                    "translations": {},
                    "language_id": 1,
                    "send_with_title": False,
                }
            if set(templates_by_source_id) != set(expected_templates):
                _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_SET_INVALID", "/artifact/interactive_templates", "Wrapper interactive templates do not match authored choice nodes.")
            for source_id, expected_template in expected_templates.items():
                matches = templates_by_source_id.get(source_id, [])
                if len(matches) != 1 or matches[0] != expected_template:
                    _issue(issues, "P4_E3_INTERACTIVE_TEMPLATE_OWNER_INVALID", f"/artifact/interactive_templates/{source_id}", "Interactive template content/label is not bound to its authored choice node.")
            route_report = _source_route_report(spec, result, compiled_by_uuid, issues)
            route_report["interactive_template_checks"] = interactive_template_checks
            route_report["contact_field_wrapper_audit"] = {
                "wrapper_key_present": isinstance(artifact.get("contact_field"), list),
                "entries_required_for_import": False,
                "runtime_set_contact_field_creates_missing_field": True,
                "compiler_emits_empty_entries": artifact.get("contact_field") == [],
                "official_import_behavior": "import_contact_field enumerates the optional entries; native set_contact_field creates missing fields at runtime",
            }

    issue_codes = {issue["code"] for issue in issues}
    checks = {
        "result_keys": isinstance(result, dict) and set(result) == _RESULT_KEYS,
        "canonical_hash": canonical_hash == expected_hash,
        "wrapper": wrapper,
        "definition": definition_valid and definition.get("spec_version") == "13.1.0",
        "uuid_uniqueness": not any("DUPLICATE" in code or code.endswith("UUID_INVALID") for code in issue_codes),
        "router_ownership": not any("OWNERSHIP" in code for code in issue_codes),
        "destinations": "P4_E3_DANGLING_DESTINATION" not in issue_codes,
        "compilation_map": not any(code.startswith("P4_E3_COMPILATION_MAP") for code in issue_codes),
        "interactive_templates": interactive_template_checks["all_action_ids_resolve"]
        and interactive_template_checks["all_action_content_matches"]
        and interactive_template_checks["unique_source_ids"]
        and interactive_template_checks["unique_labels"]
        and not any(
            code.startswith(
                ("P4_E3_INTERACTIVE_TEMPLATE", "P4_E3_DUPLICATE_INTERACTIVE_TEMPLATE")
            )
            for code in issue_codes
        ),
        "official_contract": not any(
            code.startswith(
                (
                    "P4_E3_ACTION_",
                    "P4_E3_PROMPT_ACTION_ROUTER",
                    "P4_E3_UNSUPPORTED_NODE_SHAPE",
                    "P4_E3_ROUTER_",
                    "P4_E3_WAIT_",
                    "P4_E3_CASE_",
                    "P4_E3_INTERACTIVE_TEMPLATE",
                    "P4_E3_DUPLICATE_INTERACTIVE_TEMPLATE",
                )
            )
            for code in issue_codes
        ),
        "dispatch_semantics": not any(
            code in {
                "P4_E3_PROMPT_ACTION_ROUTER_INVALID",
                "P4_E3_UNSUPPORTED_NODE_SHAPE",
                "P4_E3_ACTION_EXIT_COUNT_INVALID",
                "P4_E3_PROMPT_WAIT_LINK_INVALID",
                "P4_E3_TERMINAL_EXECUTION_INVALID",
            }
            for code in issue_codes
        ),
        "routes": not any(code in {"P4_E3_ROUTE_MISMATCH", "P4_E3_ROUTER_MISMATCH", "P4_E3_PERSISTENCE_MISMATCH", "P4_E3_STABLE_VALUE_MISMATCH", "P4_E3_TECHNICAL_ROUTE_ALIAS", "P4_E3_TERMINAL_EXIT_INVALID", "P4_E3_TERMINAL_EXECUTION_INVALID", "P4_E3_PROMPT_WAIT_LINK_INVALID"} for code in issue_codes),
    }
    return {
        "schema_version": _REPORT_SCHEMA,
        "passed": not issues,
        "issues": issues,
        "checks": checks,
        "counts": {
            "compiled_nodes": len(compiled_by_uuid),
            "compiled_actions": sum(len(node.get("actions", [])) for node in compiled_by_uuid.values()),
            "compiled_exits": sum(len(node.get("exits", [])) for node in compiled_by_uuid.values()),
            "compiled_categories": sum(len(node.get("router", {}).get("categories", [])) for node in compiled_by_uuid.values()),
            "compiled_cases": sum(len(node.get("router", {}).get("cases", [])) for node in compiled_by_uuid.values()),
        },
        "route_report": route_report,
    }


def build_cross_flow_template_registry_report(
    compiled_flows: list[tuple[dict[str, Any], Any]],
) -> dict[str, Any]:
    """Model Glific's global source-id lookup across independently imported flows."""

    registry: dict[int, list[dict[str, Any]]] = {}
    label_registry: dict[str, list[dict[str, Any]]] = {}
    flow_source_ids: dict[str, list[int]] = {}
    flow_template_labels: dict[str, list[str]] = {}
    expected_templates: list[dict[str, Any]] = []
    legacy_registry: dict[int, list[dict[str, Any]]] = {}
    action_bindings: list[dict[str, Any]] = []
    retry_reuse: list[dict[str, Any]] = []

    for artifact, flow_spec in compiled_flows:
        definition = artifact["flows"][0]["definition"]
        flow_uuid = definition["uuid"]
        payload = _model_dump(flow_spec)
        choice_nodes = [
            node for node in payload.get("nodes", []) if node.get("type") == "ask_choice"
        ]
        expected_ids = {
            node["id"]: _interactive_template_source_id(flow_uuid, node["id"])
            for node in choice_nodes
        }
        flow_source_ids[flow_uuid] = sorted(expected_ids.values())
        templates = artifact.get("interactive_templates", [])
        flow_template_labels[flow_uuid] = sorted(
            template.get("label")
            for template in templates
            if isinstance(template.get("label"), str)
        )
        for template in templates:
            source_id = template.get("source_id")
            label = template.get("label")
            owner = {
                "flow_uuid": flow_uuid,
                "source_id": source_id,
                "label": label,
                "interactive_content": template.get("interactive_content"),
            }
            registry.setdefault(source_id, []).append(owner)
            label_registry.setdefault(label, []).append(owner)
        for node in choice_nodes:
            node_id = node["id"]
            expected_source_id = expected_ids[node_id]
            expected_templates.append(
                {
                    "flow_uuid": flow_uuid,
                    "node_id": node_id,
                    "source_id": expected_source_id,
                    "label": _interactive_template_label(flow_uuid, node_id),
                    "interactive_content": _interactive_payload(node),
                }
            )
            legacy_source_id = int(hashlib.sha256(node_id.encode()).hexdigest()[:10], 16)
            legacy_registry.setdefault(legacy_source_id, []).append(
                {"flow_uuid": flow_uuid, "node_id": node_id}
            )
            compiled_actions = [
                action
                for compiled_node in definition.get("nodes", [])
                for action in compiled_node.get("actions", [])
                if action.get("type") == "send_interactive_msg"
                and action.get("name", "").startswith(f"product2_{node_id}_")
            ]
            retry_reuse.append(
                {
                    "flow_uuid": flow_uuid,
                    "node_id": node_id,
                    "expected_attempt_count": node["retry"]["max_attempts"],
                    "actual_attempt_count": len(compiled_actions),
                    "source_ids": sorted({action.get("id") for action in compiled_actions}),
                    "expected_source_id": expected_source_id,
                    "action_uuids_unique": len(
                        {action.get("uuid") for action in compiled_actions}
                    )
                    == len(compiled_actions),
                }
            )

    for artifact, _flow_spec in compiled_flows:
        definition = artifact["flows"][0]["definition"]
        flow_uuid = definition["uuid"]
        for node in definition.get("nodes", []):
            for action in node.get("actions", []):
                if action.get("type") != "send_interactive_msg":
                    continue
                matches = registry.get(action.get("id"), [])
                resolved = len(matches) == 1
                own_template = resolved and matches[0]["flow_uuid"] == flow_uuid
                content_matches = resolved and _interactive_text_matches(
                    action.get("text"), matches[0]["interactive_content"]
                )
                action_bindings.append(
                    {
                        "flow_uuid": flow_uuid,
                        "action_uuid": action.get("uuid"),
                        "action_id": action.get("id"),
                        "global_registry_matches": len(matches),
                        "resolves_to_own_flow_template": own_template,
                        "content_matches": content_matches,
                    }
                )

    source_id_sets_disjoint = sum(map(len, flow_source_ids.values())) == len(
        {source_id for source_ids in flow_source_ids.values() for source_id in source_ids}
    )
    label_sets_disjoint = sum(map(len, flow_template_labels.values())) == len(
        {label for labels in flow_template_labels.values() for label in labels}
    )
    wrapper_owners = [
        {
            **expected,
            "matching_global_entries": [
                owner
                for owner in registry.get(expected["source_id"], [])
                if owner["flow_uuid"] == expected["flow_uuid"]
                and owner["label"] == expected["label"]
                and owner["interactive_content"] == expected["interactive_content"]
            ],
        }
        for expected in expected_templates
    ]
    import_lookup = [
        {
            "flow_uuid": expected["flow_uuid"],
            "node_id": expected["node_id"],
            "label": expected["label"],
            "matching_global_entries": [
                owner
                for owner in label_registry.get(expected["label"], [])
                if owner["flow_uuid"] == expected["flow_uuid"]
                and owner["source_id"] == expected["source_id"]
                and owner["interactive_content"] == expected["interactive_content"]
            ],
        }
        for expected in expected_templates
    ]
    legacy_collisions = [
        {"source_id": source_id, "owners": owners}
        for source_id, owners in sorted(legacy_registry.items())
        if len(owners) > 1
    ]
    retry_reuse_passed = all(
        item["actual_attempt_count"] == item["expected_attempt_count"]
        and item["source_ids"] == [item["expected_source_id"]]
        and item["action_uuids_unique"]
        for item in retry_reuse
    )
    return {
        "schema_version": "product4-p49-cross-flow-template-registry-2.0",
        "identity_basis": {
            "source_id_algorithm": "sha256(flow_uuid + NUL + authored_choice_node_id), first 8 bytes modulo (2^48 - 1) plus 1",
            "source_id_max": _INTERACTIVE_TEMPLATE_SOURCE_ID_MAX,
            "label_algorithm": "product2_{flow_uuid}_{authored_choice_node_id}",
            "global_import_lookup": "Glific imports interactive templates by label, then resolves action ids through the per-import source_id mapping",
        },
        "source_id_max": _INTERACTIVE_TEMPLATE_SOURCE_ID_MAX,
        "flow_source_ids": flow_source_ids,
        "source_id_sets_disjoint": source_id_sets_disjoint,
        "flow_template_labels": flow_template_labels,
        "label_sets_disjoint": label_sets_disjoint,
        "global_source_id_registry": [
            {"source_id": source_id, "owners": owners}
            for source_id, owners in sorted(registry.items())
        ],
        "wrapper_owners": wrapper_owners,
        "all_wrappers_resolve_to_own_flow": all(
            len(item["matching_global_entries"]) == 1 for item in wrapper_owners
        ),
        "global_label_registry": [
            {"label": label, "owners": owners}
            for label, owners in sorted(label_registry.items())
        ],
        "import_lookup": import_lookup,
        "all_import_lookups_resolve_to_own_flow": all(
            len(item["matching_global_entries"]) == 1 for item in import_lookup
        ),
        "action_bindings": action_bindings,
        "all_actions_resolve_to_own_flow_content": all(
            item["resolves_to_own_flow_template"] and item["content_matches"]
            for item in action_bindings
        ),
        "retry_source_id_reuse": retry_reuse,
        "retry_source_id_reuse_passed": retry_reuse_passed,
        "legacy_node_only_collision_proof": {
            "legacy_collision_count": len(legacy_collisions),
            "collisions": legacy_collisions,
            "f002_cross_flow_collision_detected": any(
                len(item["owners"]) > 1
                and {owner["node_id"] for owner in item["owners"]} == {"F002"}
                for item in legacy_collisions
            ),
        },
        "passed": (
            source_id_sets_disjoint
            and label_sets_disjoint
            and all(len(item["matching_global_entries"]) == 1 for item in wrapper_owners)
            and all(len(item["matching_global_entries"]) == 1 for item in import_lookup)
            and all(
                item["resolves_to_own_flow_template"] and item["content_matches"]
                for item in action_bindings
            )
            and retry_reuse_passed
        ),
    }


def validate_glific_structure(result: dict[str, Any], flow_spec: Any | None = None) -> dict[str, Any]:
    report = build_glific_structural_report(result, flow_spec)
    if not report["passed"]:
        first = report["issues"][0]
        raise ValueError(f"{first['code']}:{first['path']}")
    return report


__all__ = [
    "build_cross_flow_template_registry_report",
    "build_glific_structural_report",
    "compile_glific",
    "validate_glific_structure",
]
