"""Small source-derived model of Glific Node.execute and Exit.execute.

This module deliberately does not call Product 2's compiler or validator. It
only consumes the emitted artifact and follows the pinned Glific dispatch
rules: action nodes execute their first exit, router-only nodes dispatch a
message or park on their wait, and a null exit resets the flow context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

NO_RESPONSE = object()
_RESERVED_ROUTER_BODIES = {"completed", "expired", "success", "failure"}


class RuntimeDispatchError(RuntimeError):
    pass


def _uid(flow_uuid: str, logical_id: str, role: str) -> str:
    return str(uuid5(UUID(flow_uuid), f"{logical_id}:{role}"))


def _json_spec(spec: Any) -> dict[str, Any]:
    dump = getattr(spec, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return spec


@dataclass(frozen=True)
class WaitBinding:
    source_node_id: str
    attempt: int
    node_uuid: str


class GlificRuntimeDispatch:
    """Interpret one Glific JSON artifact using official dispatch precedence."""

    def __init__(self, artifact: dict[str, Any]) -> None:
        flows = artifact.get("flows")
        flow = flows[0] if isinstance(flows, list) and flows else {}
        definition = flow.get("definition", {}) if isinstance(flow, dict) else {}
        nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
        self.definition = definition
        self.nodes = {
            node["uuid"]: node
            for node in nodes
            if isinstance(node, dict) and isinstance(node.get("uuid"), str)
        }
        self.ui_nodes = definition.get("_ui", {}).get("nodes", {})
        self.current_uuid: str | None = None
        self.terminal = False
        self.context_reset_count = 0
        self.trace: list[dict[str, Any]] = []
        self.outbound: list[dict[str, Any]] = []
        self.results: dict[str, Any] = {}
        self.result_history: list[dict[str, Any]] = []
        self.contact_fields: dict[str, Any] = {}
        self.terminal_record: dict[str, Any] | None = None
        self._step = 0

    def _record(self, event: str, **fields: Any) -> None:
        self._step += 1
        self.trace.append({"step": self._step, "event": event, **fields})

    def start_node_uuid(self) -> str:
        if not isinstance(self.ui_nodes, dict) or not self.ui_nodes:
            raise RuntimeDispatchError("START_NODE_MISSING")
        candidates = []
        for node_uuid, ui in self.ui_nodes.items():
            position = ui.get("position", {}) if isinstance(ui, dict) else {}
            candidates.append(
                (
                    position.get("top", 0),
                    position.get("left", 0),
                    node_uuid,
                )
            )
        return min(candidates)[2]

    def start(self) -> None:
        node_uuid = self.start_node_uuid()
        self._record("start_selected", node_uuid=node_uuid, selection="_ui.top_left")
        self._execute_node(node_uuid)

    def send_message(self, text: str) -> None:
        if self.current_uuid is None or self.terminal:
            raise RuntimeDispatchError("MESSAGE_WITHOUT_WAIT")
        self._record("message_received", node_uuid=self.current_uuid, text=text)
        self._execute_node(self.current_uuid, message=text)

    def timeout(self) -> None:
        if self.current_uuid is None or self.terminal:
            raise RuntimeDispatchError("TIMEOUT_WITHOUT_WAIT")
        self._record("timeout_fired", node_uuid=self.current_uuid)
        self._execute_node(self.current_uuid, message=NO_RESPONSE)

    def _execute_node(self, node_uuid: str, message: Any = None) -> None:
        if self.terminal:
            return
        node = self.nodes.get(node_uuid)
        if node is None:
            raise RuntimeDispatchError(f"NODE_MISSING:{node_uuid}")
        actions = node.get("actions", [])
        has_router = isinstance(node.get("router"), dict)
        self.current_uuid = node_uuid
        self._record(
            "node_execute",
            node_uuid=node_uuid,
            dispatch="action_and_router"
            if actions and has_router
            else "actions"
            if actions
            else "router"
            if has_router
            else "unsupported",
            message_kind="none"
            if message is None
            else "no_response"
            if message is NO_RESPONSE
            else "text",
        )
        if actions and has_router:
            # This is the exact pinned Node.execute precedence that caused the
            # rejected P49 shape. Ordinary replies re-execute the action; only
            # reserved bodies are handed to the router.
            if isinstance(message, str) and message.casefold() in _RESERVED_ROUTER_BODIES:
                self._record("action_router_reserved_router_dispatch", node_uuid=node_uuid)
                self._dispatch_router(node, message)
            else:
                self._record("action_router_reexecuted_action", node_uuid=node_uuid)
                self._execute_actions(node)
            return
        if actions:
            self._execute_actions(node)
            exits = node.get("exits", [])
            if not isinstance(exits, list) or not exits:
                raise RuntimeDispatchError(f"ACTION_EXIT_MISSING:{node_uuid}")
            self._execute_exit(exits[0])
            return
        if has_router:
            if message is None:
                self._park_on_wait(node)
            else:
                self._dispatch_router(node, message)
            return
        self._record("unsupported_node_type", node_uuid=node_uuid)
        raise RuntimeDispatchError(f"UNSUPPORTED_NODE_TYPE:{node_uuid}")

    def _execute_actions(self, node: dict[str, Any]) -> None:
        for action in node.get("actions", []):
            if not isinstance(action, dict):
                raise RuntimeDispatchError("ACTION_INVALID")
            action_type = action.get("type")
            self._record(
                "action_execute",
                node_uuid=node["uuid"],
                action_uuid=action.get("uuid"),
                action_type=action_type,
            )
            if action_type in {"send_msg", "send_interactive_msg"}:
                outbound = {
                    "node_uuid": node["uuid"],
                    "action_uuid": action.get("uuid"),
                    "type": action_type,
                    "text": action.get("text"),
                }
                if action_type == "send_interactive_msg":
                    outbound["source_id"] = action.get("id")
                self.outbound.append(outbound)
                self._record("outbound_message", **outbound)
            elif action_type == "set_run_result":
                value = action.get("value")
                record = {
                    "name": action.get("name"),
                    "value": value,
                    "category": action.get("category"),
                    "node_uuid": node["uuid"],
                    "action_uuid": action.get("uuid"),
                }
                self.results[str(action.get("name"))] = value
                self.result_history.append(record)
                self._record("run_result_set", **record)
                if action.get("name") == "terminal_reason":
                    self.terminal_record = {
                        "reason": value,
                        "category": action.get("category"),
                        "node_uuid": node["uuid"],
                        "action_uuid": action.get("uuid"),
                    }
            elif action_type == "set_contact_field":
                field = action.get("field", {})
                field_name = field.get("name") if isinstance(field, dict) else None
                if not isinstance(field_name, str) or not field_name:
                    raise RuntimeDispatchError("CONTACT_FIELD_NAME_MISSING")
                value = self._resolve_value(action.get("value"))
                self.contact_fields[field_name] = value
                self._record(
                    "contact_field_set",
                    node_uuid=node["uuid"],
                    action_uuid=action.get("uuid"),
                    field=field_name,
                    value=value,
                )
            else:
                raise RuntimeDispatchError(f"ACTION_UNSUPPORTED:{action_type}")

    def _resolve_value(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("@results."):
            return self.results.get(value.removeprefix("@results."))
        return value

    def _execute_exit(self, exit_item: dict[str, Any]) -> None:
        destination = exit_item.get("destination_uuid")
        self._record(
            "exit_execute",
            exit_uuid=exit_item.get("uuid"),
            destination_uuid=destination,
        )
        if destination is None:
            self.context_reset_count += 1
            self.current_uuid = None
            self.terminal = True
            self._record(
                "context_reset",
                reason=self.terminal_record,
                active_results_cleared=True,
            )
            self.results = {}
            return
        if destination not in self.nodes:
            raise RuntimeDispatchError(f"EXIT_DESTINATION_MISSING:{destination}")
        self._execute_node(destination)

    def _park_on_wait(self, node: dict[str, Any]) -> None:
        wait = node.get("router", {}).get("wait", {})
        self.current_uuid = node["uuid"]
        self._record(
            "wait_parked",
            node_uuid=node["uuid"],
            wait_type=wait.get("type"),
            timeout=wait.get("timeout"),
        )

    def _dispatch_router(self, node: dict[str, Any], message: Any) -> None:
        router = node.get("router")
        if not isinstance(router, dict):
            raise RuntimeDispatchError("ROUTER_MISSING")
        categories = {
            item.get("uuid"): item
            for item in router.get("categories", [])
            if isinstance(item, dict)
        }
        if message is NO_RESPONSE:
            timeout = router.get("wait", {}).get("timeout", {})
            category_uuid = timeout.get("category_uuid")
        else:
            category_uuid = self._match_case(router, str(message))
        category = categories.get(category_uuid)
        if not isinstance(category, dict):
            raise RuntimeDispatchError(f"ROUTER_CATEGORY_MISSING:{category_uuid}")
        if message is not NO_RESPONSE:
            result_name = router.get("result_name")
            if isinstance(result_name, str) and result_name:
                self.results[result_name] = str(message)
                self._record(
                    "router_result_saved",
                    node_uuid=node["uuid"],
                    result_name=result_name,
                    value=str(message),
                )
        self._record(
            "router_dispatch",
            node_uuid=node["uuid"],
            category_uuid=category_uuid,
            category_name=category.get("name"),
            message=None if message is NO_RESPONSE else str(message),
        )
        exits = {
            item.get("uuid"): item
            for item in node.get("exits", [])
            if isinstance(item, dict)
        }
        exit_item = exits.get(category.get("exit_uuid"))
        if not isinstance(exit_item, dict):
            raise RuntimeDispatchError(f"ROUTER_EXIT_MISSING:{category.get('exit_uuid')}")
        self._execute_exit(exit_item)

    def _match_case(self, router: dict[str, Any], text: str) -> str | None:
        for case in router.get("cases", []):
            if isinstance(case, dict) and self._case_matches(case, text):
                return case.get("category_uuid")
        return router.get("default_category_uuid")

    @staticmethod
    def _case_matches(case: dict[str, Any], text: str) -> bool:
        case_type = case.get("type")
        arguments = case.get("arguments", [])
        if case_type in {"has_only_phrase", "has_only_text"}:
            return bool(arguments) and text.casefold() == str(arguments[0]).casefold()
        if case_type in {"has_phrase", "has_beginning"}:
            return bool(arguments) and str(arguments[0]).casefold() in text.casefold()
        if case_type == "has_pattern":
            return bool(arguments) and re.fullmatch(str(arguments[0]), text) is not None
        if case_type == "has_email":
            return re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text) is not None
        if case_type == "has_phone":
            digits = re.sub(r"\D", "", text)
            return len(digits) >= 7 and bool(re.fullmatch(r"[+()\-\s0-9]+", text))
        if case_type == "has_number":
            try:
                float(text)
                return True
            except ValueError:
                return False
        if case_type == "has_any_word":
            words = [item.strip().casefold() for item in str(arguments[0]).split(",")]
            return any(word and word in text.casefold().split() for word in words)
        return False

    def export(self) -> dict[str, Any]:
        return {
            "terminal": self.terminal,
            "current_node_uuid": self.current_uuid,
            "context_reset_count": self.context_reset_count,
            "outbound": self.outbound,
            "result_history": self.result_history,
            "contact_fields": self.contact_fields,
            "terminal_record": self.terminal_record,
            "trace": self.trace,
        }


def _bindings(spec: Any) -> tuple[dict[str, dict[str, Any]], dict[str, WaitBinding]]:
    payload = _json_spec(spec)
    flow_uuid = payload["flow"]["id"]
    nodes = {node["id"]: node for node in payload.get("nodes", [])}
    waits: dict[str, WaitBinding] = {}
    for node in payload.get("nodes", []):
        if node.get("type") not in {"ask_choice", "ask_input"}:
            continue
        for attempt in range(1, node["retry"]["max_attempts"] + 1):
            prompt = node["id"] if attempt == 1 else f"{node['id']}:attempt:{attempt}"
            wait_logical = f"{prompt}:wait"
            waits[_uid(flow_uuid, wait_logical, "node")] = WaitBinding(node["id"], attempt, _uid(flow_uuid, wait_logical, "node"))
    return nodes, waits


def _valid_response(node: dict[str, Any], choice_index: int = 0) -> str:
    if node["type"] == "ask_choice":
        return node["choices"][choice_index % len(node["choices"])] ["title"]
    input_type = node.get("input_type")
    return {
        "text": "a valid response",
        "number": "42",
        "email": "person@example.com",
        "phone": "+15551234567",
        "time": "10:30 AM",
    }.get(input_type, "a valid response")


def _invalid_response(node: dict[str, Any]) -> str | None:
    if node["type"] == "ask_choice":
        return "not one of the available choices"
    validation = node.get("validation") or {}
    parser = validation.get("parser")
    if parser == "email":
        return "not-an-email"
    if parser == "phone":
        return "not-a-phone"
    if parser == "integer":
        return "not-a-number"
    if parser in {"plain_text"} and validation.get("constraints"):
        return "not-a-valid-response"
    return None


def _can_reach(nodes: dict[str, dict[str, Any]], start: str, target: str, seen: set[str] | None = None) -> bool:
    if start == target:
        return True
    seen = seen or set()
    if start in seen or start not in nodes:
        return False
    seen.add(start)
    node = nodes[start]
    targets: list[str] = []
    if node.get("type") == "ask_choice":
        targets.extend(choice["next_node_id"] for choice in node.get("choices", []))
        targets.extend([node["retry"]["on_exhausted_node_id"], node["no_response"]["next_node_id"]])
    elif node.get("type") == "ask_input":
        targets.extend([node["next_node_id"], node["retry"]["on_exhausted_node_id"]])
        if node.get("no_response"):
            targets.append(node["no_response"]["next_node_id"])
    else:
        for key in ("next_node_id", "success_node_id", "default_node_id"):
            if node.get(key):
                targets.append(node[key])
    return any(_can_reach(nodes, target_id, target, seen.copy()) for target_id in targets)


def _reachable_source_node_ids(
    nodes: dict[str, dict[str, Any]], payload: dict[str, Any]
) -> set[str]:
    entry_node_id = payload["flow"]["entry_node_id"]
    return {
        node_id
        for node_id in nodes
        if _can_reach(nodes, entry_node_id, node_id)
    }


def _wait_logical(node_id: str, attempt: int) -> str:
    prompt = node_id if attempt == 1 else f"{node_id}:attempt:{attempt}"
    return f"{prompt}:wait"


def _wait_uuid(flow_uuid: str, node_id: str, attempt: int) -> str:
    return _uid(flow_uuid, _wait_logical(node_id, attempt), "node")


def _terminal_trace_passed(runtime: dict[str, Any]) -> bool:
    return bool(
        runtime.get("terminal")
        and runtime.get("context_reset_count") == 1
        and runtime.get("terminal_record")
        and any(
            item.get("event") == "exit_execute"
            and item.get("destination_uuid") is None
            for item in runtime.get("trace", [])
        )
    )


def _drive_to_wait(
    artifact: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    waits: dict[str, WaitBinding],
    target_node_id: str,
    target_attempt: int = 1,
) -> GlificRuntimeDispatch:
    """Navigate with valid replies until one exact authored wait is parked."""

    runtime = GlificRuntimeDispatch(artifact)
    runtime.start()
    for _ in range(100):
        if runtime.terminal:
            raise RuntimeDispatchError(f"TARGET_WAIT_TERMINATED:{target_node_id}:{target_attempt}")
        binding = waits.get(runtime.current_uuid or "")
        if binding is None:
            raise RuntimeDispatchError(f"WAIT_BINDING_MISSING:{runtime.current_uuid}")
        node = nodes[binding.source_node_id]
        if binding.source_node_id == target_node_id:
            if binding.attempt == target_attempt:
                return runtime
            if binding.attempt < target_attempt:
                invalid = _invalid_response(node)
                if invalid is None:
                    raise RuntimeDispatchError(
                        f"RETRY_ATTEMPT_UNREACHABLE:{target_node_id}:{target_attempt}"
                    )
                runtime.send_message(invalid)
                continue
            raise RuntimeDispatchError(f"TARGET_WAIT_PASSED:{target_node_id}:{target_attempt}")
        if node["type"] == "ask_choice":
            selected = next(
                (
                    choice
                    for choice in node["choices"]
                    if _can_reach(nodes, choice["next_node_id"], target_node_id)
                ),
                None,
            )
            if selected is None:
                raise RuntimeDispatchError(
                    f"TARGET_WAIT_PATH_MISSING:{binding.source_node_id}:{target_node_id}"
                )
            runtime.send_message(selected["title"])
        else:
            runtime.send_message(_valid_response(node))
    raise RuntimeDispatchError(f"TARGET_WAIT_STEP_LIMIT:{target_node_id}:{target_attempt}")


def _continue_to_termination(
    runtime: GlificRuntimeDispatch,
    nodes: dict[str, dict[str, Any]],
    waits: dict[str, WaitBinding],
) -> dict[str, Any]:
    """Continue with first valid outcomes until Exit.execute(null) resets context."""

    for _ in range(100):
        if runtime.terminal:
            return runtime.export()
        binding = waits.get(runtime.current_uuid or "")
        if binding is None:
            raise RuntimeDispatchError(f"WAIT_BINDING_MISSING:{runtime.current_uuid}")
        node = nodes[binding.source_node_id]
        if node["type"] == "ask_choice":
            runtime.send_message(node["choices"][0]["title"])
        else:
            runtime.send_message(_valid_response(node))
    raise RuntimeDispatchError("TERMINATION_STEP_LIMIT")


def _reachable_asks(
    payload: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    reachable: set[str],
    node_type: str,
) -> list[dict[str, Any]]:
    return [
        node
        for node in payload.get("nodes", [])
        if node.get("type") == node_type and node["id"] in reachable
    ]


def _choice_outcome_coverage(
    artifact: dict[str, Any],
    payload: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    waits: dict[str, WaitBinding],
    reachable: set[str],
) -> dict[str, Any]:
    flow_uuid = payload["flow"]["id"]
    traces: list[dict[str, Any]] = []
    for node in _reachable_asks(payload, nodes, reachable, "ask_choice"):
        for choice in node["choices"]:
            runtime = _drive_to_wait(artifact, nodes, waits, node["id"])
            target_wait_uuid = _wait_uuid(flow_uuid, node["id"], 1)
            runtime.send_message(choice["title"])
            trace = _continue_to_termination(runtime, nodes, waits)
            expected_category_uuid = _uid(
                flow_uuid,
                f"{node['id']}:wait:{choice['id']}",
                "category",
            )
            expected_stable_node_uuid = _uid(
                flow_uuid, f"{node['id']}:{choice['id']}:value", "node"
            )
            expected_destination_uuid = _uid(flow_uuid, choice["next_node_id"], "node")
            router_match = any(
                item.get("event") == "router_dispatch"
                and item.get("node_uuid") == target_wait_uuid
                and item.get("category_uuid") == expected_category_uuid
                and item.get("category_name") == choice["title"]
                for item in trace["trace"]
            )
            stable_value = any(
                item.get("event") == "run_result_set"
                and item.get("node_uuid") == expected_stable_node_uuid
                and item.get("name") == node["save_as"]
                and item.get("value") == choice["submitted_value"]
                for item in trace["trace"]
            )
            destination_entered = any(
                item.get("event") == "node_execute"
                and item.get("node_uuid") == expected_destination_uuid
                for item in trace["trace"]
            )
            proof = {
                "router_matched_expected_category": router_match,
                "stable_submitted_value_action_executed": stable_value,
                "expected_authored_destination_entered": destination_entered,
                "terminated_via_null_exit": _terminal_trace_passed(trace),
            }
            traces.append(
                {
                    "scope": "every_reachable_authored_choice_outcome",
                    "node_id": node["id"],
                    "choice_id": choice["id"],
                    "title": choice["title"],
                    "submitted_value": choice["submitted_value"],
                    "expected_category_uuid": expected_category_uuid,
                    "expected_stable_value_node_uuid": expected_stable_node_uuid,
                    "expected_destination_uuid": expected_destination_uuid,
                    "proof": proof,
                    "passed": all(proof.values()),
                    "trace": trace,
                }
            )
    return {
        "scope": "every reachable authored ask_choice outcome; one terminating trace per choice",
        "expected_count": len(traces),
        "covered_count": sum(item["passed"] for item in traces),
        "traces": traces,
        "passed": bool(traces or not _reachable_asks(payload, nodes, reachable, "ask_choice"))
        and all(item["passed"] for item in traces),
    }


def _input_validation_coverage(
    artifact: dict[str, Any],
    payload: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    waits: dict[str, WaitBinding],
    reachable: set[str],
) -> dict[str, Any]:
    flow_uuid = payload["flow"]["id"]
    artifact_nodes = GlificRuntimeDispatch(artifact).nodes
    traces: list[dict[str, Any]] = []
    for node in _reachable_asks(payload, nodes, reachable, "ask_input"):
        runtime = _drive_to_wait(artifact, nodes, waits, node["id"])
        valid_sample = _valid_response(node)
        target_wait_uuid = _wait_uuid(flow_uuid, node["id"], 1)
        runtime.send_message(valid_sample)
        trace = _continue_to_termination(runtime, nodes, waits)
        wait_node = artifact_nodes[target_wait_uuid]
        accepted_category_uuids = {
            category["uuid"]
            for category in wait_node["router"]["categories"]
            if category.get("name") == "Accepted"
        }
        router_accepted = any(
            item.get("event") == "router_dispatch"
            and item.get("node_uuid") == target_wait_uuid
            and item.get("category_uuid") in accepted_category_uuids
            and item.get("category_name") == "Accepted"
            for item in trace["trace"]
        )
        result_saved = any(
            item.get("event") == "router_result_saved"
            and item.get("node_uuid") == target_wait_uuid
            and item.get("result_name") == node["save_as"]
            and item.get("value") == valid_sample
            for item in trace["trace"]
        )
        expected_destination_uuid = _uid(flow_uuid, node["next_node_id"], "node")
        destination_entered = any(
            item.get("event") == "node_execute"
            and item.get("node_uuid") == expected_destination_uuid
            for item in trace["trace"]
        )
        proof = {
            "router_dispatched_accepted_category": router_accepted,
            "result_saved_under_result_name": result_saved,
            "declared_next_node_entered": destination_entered,
            "continued_to_null_exit_termination": _terminal_trace_passed(trace),
        }
        traces.append(
            {
                "scope": "every reachable authored ask_input node; one valid parser-appropriate trace",
                "node_id": node["id"],
                "input_type": node["input_type"],
                "parser": node["validation"]["parser"] if node.get("validation") else "plain_text",
                "valid_sample": valid_sample,
                "result_name": node["save_as"],
                "expected_destination_uuid": expected_destination_uuid,
                "accepted_category_uuids": sorted(accepted_category_uuids),
                "proof": proof,
                "passed": all(proof.values()),
                "trace": trace,
            }
        )
    return {
        "scope": "every reachable authored ask_input node; parser/type valid response and terminating continuation",
        "expected_count": len(traces),
        "covered_count": sum(item["passed"] for item in traces),
        "traces": traces,
        "passed": bool(traces or not _reachable_asks(payload, nodes, reachable, "ask_input"))
        and all(item["passed"] for item in traces),
    }


def _invalid_exhaustion_coverage(
    artifact: dict[str, Any],
    payload: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    waits: dict[str, WaitBinding],
    reachable: set[str],
) -> dict[str, Any]:
    flow_uuid = payload["flow"]["id"]
    ask_nodes = _reachable_asks(payload, nodes, reachable, "ask_choice") + _reachable_asks(
        payload, nodes, reachable, "ask_input"
    )
    records: list[dict[str, Any]] = []
    for node in ask_nodes:
        invalid_sample = _invalid_response(node)
        if invalid_sample is None:
            records.append(
                {
                    "scope": "reachable authored wait; not applicable when official default accepts all input",
                    "node_id": node["id"],
                    "applicable": False,
                    "status": "not_applicable",
                    "reason": "No authored invalid matcher exists for this wait.",
                    "passed": True,
                }
            )
            continue
        runtime = _drive_to_wait(artifact, nodes, waits, node["id"])
        dispatches: list[dict[str, Any]] = []
        for attempt in range(1, node["retry"]["max_attempts"] + 1):
            wait_uuid = _wait_uuid(flow_uuid, node["id"], attempt)
            runtime.send_message(invalid_sample)
            dispatches.append(
                {
                    "attempt": attempt,
                    "wait_uuid": wait_uuid,
                    "matched_other": any(
                        item.get("event") == "router_dispatch"
                        and item.get("node_uuid") == wait_uuid
                        and item.get("category_name") == "Other"
                        for item in runtime.trace
                    ),
                }
            )
        trace = runtime.export()
        expected_destination_uuid = _uid(
            flow_uuid, node["retry"]["on_exhausted_node_id"], "node"
        )
        proof = {
            "attempts_1_2_3_dispatched_other": len(dispatches) == node["retry"]["max_attempts"]
            and all(item["matched_other"] for item in dispatches),
            "retry_exhaustion_destination_entered": any(
                item.get("event") == "node_execute"
                and item.get("node_uuid") == expected_destination_uuid
                for item in trace["trace"]
            ),
            "terminated_via_null_exit": _terminal_trace_passed(trace),
        }
        records.append(
            {
                "scope": "every retry attempt for each reachable authored wait with an official invalid matcher",
                "node_id": node["id"],
                "applicable": True,
                "invalid_sample": invalid_sample,
                "expected_attempt_count": node["retry"]["max_attempts"],
                "dispatches": dispatches,
                "expected_exhaustion_destination_uuid": expected_destination_uuid,
                "proof": proof,
                "passed": all(proof.values()),
                "trace": trace,
            }
        )
    return {
        "scope": "all reachable authored waits; exhaustive for applicable invalid matchers, explicit N/A otherwise",
        "records": records,
        "passed": all(item["passed"] for item in records),
    }


def _timeout_coverage(
    artifact: dict[str, Any],
    payload: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    waits: dict[str, WaitBinding],
    reachable: set[str],
) -> dict[str, Any]:
    flow_uuid = payload["flow"]["id"]
    ask_nodes = _reachable_asks(payload, nodes, reachable, "ask_choice") + _reachable_asks(
        payload, nodes, reachable, "ask_input"
    )
    records: list[dict[str, Any]] = []
    for node in ask_nodes:
        no_response = node.get("no_response")
        if not no_response:
            records.append(
                {
                    "scope": "reachable authored wait; not applicable without a declared no_response route",
                    "node_id": node["id"],
                    "applicable": False,
                    "status": "not_applicable",
                    "passed": True,
                }
            )
            continue
        invalid_sample = _invalid_response(node)
        attempts = (
            range(1, node["retry"]["max_attempts"] + 1)
            if invalid_sample
            else range(1, 2)
        )
        for attempt in attempts:
            runtime = _drive_to_wait(artifact, nodes, waits, node["id"], attempt)
            wait_uuid = _wait_uuid(flow_uuid, node["id"], attempt)
            runtime.timeout()
            trace = runtime.export()
            expected_category_uuid = _uid(
                flow_uuid,
                _wait_logical(node["id"], attempt),
                "no_response_category",
            )
            expected_destination_uuid = _uid(flow_uuid, no_response["next_node_id"], "node")
            proof = {
                "timeout_dispatched_no_response_category": any(
                    item.get("event") == "router_dispatch"
                    and item.get("node_uuid") == wait_uuid
                    and item.get("category_uuid") == expected_category_uuid
                    and item.get("category_name") == "No Response"
                    for item in trace["trace"]
                ),
                "technical_destination_entered": any(
                    item.get("event") == "node_execute"
                    and item.get("node_uuid") == expected_destination_uuid
                    for item in trace["trace"]
                ),
                "terminated_via_null_exit": _terminal_trace_passed(trace),
            }
            records.append(
                {
                    "scope": "each reachable authored wait attempt; all retry attempts where invalid navigation is reachable",
                    "node_id": node["id"],
                    "attempt": attempt,
                    "applicable": True,
                    "timeout_seconds": no_response["timeout_seconds"],
                    "expected_category_uuid": expected_category_uuid,
                    "expected_destination_uuid": expected_destination_uuid,
                    "proof": proof,
                    "passed": all(proof.values()),
                    "trace": trace,
                }
            )
    return {
        "scope": "all reachable authored waits with no_response; all retry attempts where invalid navigation is reachable",
        "records": records,
        "passed": all(item["passed"] for item in records),
    }


def run_case_scenarios(artifact: dict[str, Any], spec: Any) -> dict[str, Any]:
    """Run exhaustive authored coverage plus explicitly scoped representative paths."""

    nodes, waits = _bindings(spec)
    payload = _json_spec(spec)
    reachable = _reachable_source_node_ids(nodes, payload)

    def drive_representative(preferred_target: str | None = None) -> dict[str, Any]:
        runtime = GlificRuntimeDispatch(artifact)
        runtime.start()
        for _ in range(100):
            if runtime.terminal:
                return runtime.export()
            binding = waits.get(runtime.current_uuid or "")
            if binding is None:
                raise RuntimeDispatchError(f"WAIT_BINDING_MISSING:{runtime.current_uuid}")
            node = nodes[binding.source_node_id]
            if node["type"] == "ask_choice":
                selected = next(
                    (
                        choice
                        for choice in node["choices"]
                        if preferred_target
                        and _can_reach(nodes, choice["next_node_id"], preferred_target)
                    ),
                    node["choices"][0],
                )
                runtime.send_message(selected["title"])
            else:
                runtime.send_message(_valid_response(node))
        raise RuntimeDispatchError("REPRESENTATIVE_STEP_LIMIT")

    choice_nodes = _reachable_asks(payload, nodes, reachable, "ask_choice")
    input_nodes = _reachable_asks(payload, nodes, reachable, "ask_input")
    record_nodes = [
        node["id"]
        for node in payload.get("nodes", [])
        if node.get("type") == "record_request"
    ]
    representative_valid = drive_representative()
    representative_persistence = drive_representative(record_nodes[0] if record_nodes else None)
    representative_no_response = GlificRuntimeDispatch(artifact)
    representative_no_response.start()
    if waits.get(representative_no_response.current_uuid or "") is None:
        raise RuntimeDispatchError("REPRESENTATIVE_NO_RESPONSE_WAIT_MISSING")
    representative_no_response.timeout()

    choice_coverage = _choice_outcome_coverage(artifact, payload, nodes, waits, reachable)
    input_coverage = _input_validation_coverage(artifact, payload, nodes, waits, reachable)
    invalid_coverage = _invalid_exhaustion_coverage(artifact, payload, nodes, waits, reachable)
    timeout_coverage = _timeout_coverage(artifact, payload, nodes, waits, reachable)

    return {
        "start_node_selected_from_ui_top_left": bool(
            representative_valid.get("trace")
            and representative_valid["trace"][0].get("event") == "start_selected"
            and representative_valid["trace"][0].get("selection") == "_ui.top_left"
        ),
        "reachable_scope": {
            "reachable_source_node_ids": sorted(reachable),
            "reachable_ask_choice_node_ids": [node["id"] for node in choice_nodes],
            "reachable_ask_input_node_ids": [node["id"] for node in input_nodes],
        },
        "representative_paths": {
            "scope": "one representative valid path, one persistence path, and one first-wait timeout",
            "valid": representative_valid,
            "valid_persistence": representative_persistence,
            "no_response": representative_no_response.export(),
        },
        "choice_outcome_coverage": choice_coverage,
        "input_validation_coverage": input_coverage,
        "invalid_exhaustion_coverage": invalid_coverage,
        "timeout_coverage": timeout_coverage,
    }


def run_rejected_shape_regressions() -> dict[str, Any]:
    """Exercise the exact action+router and actionless/routerless regressions."""

    flow_uuid = "00000000-0000-0000-0000-000000000001"
    action_router_uuid = _uid(flow_uuid, "rejected-action-router", "node")
    empty_terminal_uuid = _uid(flow_uuid, "rejected-empty-terminal", "node")
    action = {
        "uuid": _uid(flow_uuid, "rejected-action-router", "action"),
        "type": "send_msg",
        "text": "prompt",
        "quick_replies": [],
        "labels": [],
        "attachments": [],
    }
    router_exit = {"uuid": _uid(flow_uuid, "rejected-action-router:choice", "exit"), "destination_uuid": None}
    router = {
        "type": "switch",
        "operand": "@input.text",
        "result_name": "choice",
        "wait": {"type": "msg"},
        "cases": [],
        "categories": [
            {"uuid": _uid(flow_uuid, "rejected-action-router:other", "category"), "name": "Other", "exit_uuid": router_exit["uuid"]}
        ],
        "default_category_uuid": _uid(flow_uuid, "rejected-action-router:other", "category"),
    }
    artifact = {
        "flows": [{
            "definition": {
                "_ui": {"nodes": {action_router_uuid: {"type": "execute_actions", "position": {"top": 0, "left": 0}}}},
                "nodes": [{"uuid": action_router_uuid, "actions": [action], "exits": [router_exit], "router": router}],
            },
            "keywords": [],
        }],
        "contact_field": [],
        "collections": [],
        "interactive_templates": [],
    }
    action_router_runtime = GlificRuntimeDispatch(artifact)
    action_router_runtime.start()
    action_router_runtime.send_message("Callback")
    action_router_events = [item["event"] for item in action_router_runtime.trace]

    empty_artifact = {
        "flows": [{
            "definition": {
                "_ui": {"nodes": {empty_terminal_uuid: {"type": "execute_actions", "position": {"top": 0, "left": 0}}}},
                "nodes": [{"uuid": empty_terminal_uuid, "actions": [], "exits": []}],
            },
            "keywords": [],
        }],
        "contact_field": [],
        "collections": [],
        "interactive_templates": [],
    }
    empty_error = None
    try:
        GlificRuntimeDispatch(empty_artifact).start()
    except RuntimeDispatchError as exc:
        empty_error = str(exc)

    return {
        "action_plus_router": {
            "normal_reply_reexecutes_action": "action_router_reexecuted_action" in action_router_events,
            "normal_reply_does_not_dispatch_router": "router_dispatch" not in action_router_events,
            "trace": action_router_runtime.export(),
        },
        "empty_terminal": {
            "rejected_as_unsupported": empty_error == f"UNSUPPORTED_NODE_TYPE:{empty_terminal_uuid}",
            "error": empty_error,
        },
    }


__all__ = [
    "GlificRuntimeDispatch",
    "RuntimeDispatchError",
    "run_case_scenarios",
    "run_rejected_shape_regressions",
]
