from __future__ import annotations

import json
import hashlib
import re
import ssl
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any

import certifi

from product4.capabilities.registry import (
    AcquisitionPolicy,
    REGISTRY,
    bind_semantic_facts,
    require_capability,
    registry_payload,
    required_semantic_fact_gaps,
    semantic_fact_field_map,
    validate_registry_field_value,
    workbench_field_policy,
)
from product4.contracts.atomic_meaning import MeaningPlan, MeaningPlanStep, MeaningStepStatus
from product4.contracts.trigger import FlowTriggerIntent, TriggerKeywordIntent


MODEL = "gpt-5.4"
ENDPOINT = "https://api.openai.com/v1/chat/completions"
FACT_KINDS = [
    "user_facing_text", "visible_choice", "selection_cardinality",
    "response_format", "provided_variable_name", "validation_rule",
    "requiredness", "terminal_outcome", "trigger_value",
    "communication_channel", "persistence_destination",
    "duration_or_schedule", "external_endpoint", "provided_identifier",
]


NULLABLE_NUMBER = {"anyOf": [{"type": "number"}, {"type": "null"}]}
NULLABLE_INTEGER = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
NULLABLE_STRINGS = {
    "anyOf": [
        {"type": "array", "items": {"type": "string"}},
        {"type": "null"},
    ]
}


GRAPH_SCHEMA = {
    "type": "object",
    "properties": {
        "entry_event": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "source_span": {"type": "string"},
                "target_node_id": {"type": "string"},
                "details": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "kind": {"type": "string", "enum": FACT_KINDS},
                            "values": {"type": "array", "items": {"type": "string"}},
                            "source_span": {"type": "string"},
                        },
                        "required": ["id", "kind", "values", "source_span"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["description", "source_span", "target_node_id", "details"],
            "additionalProperties": False,
        },
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "behavior": {"type": "string"},
                    "source_span": {"type": "string"},
                    "user_text_status": {
                        "type": "string",
                        "enum": ["supplied", "absent"],
                    },
                    "details": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "kind": {"type": "string", "enum": FACT_KINDS},
                                "values": {"type": "array", "items": {"type": "string"}},
                                "validation": {
                                    "anyOf": [
                                        {
                                            "type": "object",
                                            "properties": {
                                                "minimum": NULLABLE_NUMBER,
                                                "maximum": NULLABLE_NUMBER,
                                                "min_length": NULLABLE_INTEGER,
                                                "max_length": NULLABLE_INTEGER,
                                                "pattern": NULLABLE_STRING,
                                                "allowed_values": NULLABLE_STRINGS,
                                            },
                                            "required": [
                                                "minimum", "maximum", "min_length", "max_length",
                                                "pattern", "allowed_values",
                                            ],
                                            "additionalProperties": False,
                                        },
                                        {"type": "null"},
                                    ]
                                },
                                "source_span": {"type": "string"},
                            },
                            "required": ["id", "kind", "values", "validation", "source_span"],
                            "additionalProperties": False,
                        },
                    },
                    "completion": {"type": "string", "enum": ["connected", "terminal", "unresolved"]},
                },
                "required": [
                    "id", "behavior", "source_span", "user_text_status",
                    "details", "completion",
                ],
                "additionalProperties": False,
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "from_node_id": {"type": "string"},
                    "to_node_id": {"type": "string"},
                    "condition": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "source_span": {"type": "string"},
                },
                "required": ["id", "from_node_id", "to_node_id", "condition", "source_span"],
                "additionalProperties": False,
            },
        },
        "self_audit": {
            "type": "object",
            "properties": {
                "all_source_covered": {"type": "boolean"},
                "all_nodes_connected": {"type": "boolean"},
                "all_paths_terminate": {"type": "boolean"},
                "uncovered_source_spans": {"type": "array", "items": {"type": "string"}},
                "dangling_node_ids": {"type": "array", "items": {"type": "string"}},
                "unresolved_observations": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "all_source_covered", "all_nodes_connected", "all_paths_terminate",
                "uncovered_source_spans", "dangling_node_ids", "unresolved_observations",
            ],
            "additionalProperties": False,
        },
    },
    "required": ["entry_event", "nodes", "relationships", "self_audit"],
    "additionalProperties": False,
}


def provider_call(api_key: str, project: str, *, name: str, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    body = {
        "model": MODEL,
        "reasoning_effort": "high",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if project:
        headers["OpenAI-Project"] = project
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(
            request,
            timeout=300,
            context=ssl.create_default_context(cafile=certifi.where()),
        ) as response:
            envelope = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        request_id = exc.headers.get("x-request-id") if exc.headers else None
        raise RuntimeError(f"P4_ATOMIC_PROVIDER_HTTP_{exc.code}:{request_id or '-'}") from exc
    elapsed = round(time.monotonic() - started, 2)
    choice = envelope["choices"][0]["message"]
    if choice.get("refusal"):
        raise RuntimeError("P4_ATOMIC_PROVIDER_REFUSAL")
    return json.loads(choice["content"]), {
        "seconds": elapsed,
        "usage": envelope.get("usage", {}),
        "request_id": envelope.get("id"),
    }


GRAPH_SYSTEM = """You are LLM0.5, a capability-independent semantic decomposer.
Convert prose into executable atomic behaviors and explicit relationships, without using product capability names.

Rules:
1. One node contains one observable behavior. A question with fixed visible answers is one behavior; capture after an open question belongs in the same behavior. Sending a message and ending the flow are always two separate nodes, even when joined by "and" in one sentence.
2. Put sequencing and branches only in relationships. Conditions must use the exact visible answer label from the prose.
3. Every source_span must be an exact contiguous substring of the prose. Never paraphrase a source_span.
4. Extract every explicit runtime fact into details: exact displayed text, visible choices, selection cardinality, physical response format, explicitly supplied variable names, validation, terminal outcomes, triggers and channels. A subject such as full name, email address or phone number is not a variable name. Use provided_variable_name only when the prose explicitly instructs the system to save or store a response under a named variable.
5. Set user_text_status=supplied exactly when the node has a source-grounded user_facing_text detail. Otherwise set it to absent. This records observation only; it does not decide whether text is required.
6. Never invent prompts, answers, variables, constraints, endpoints or outcomes. Omit absent facts.
7. Nodes are capability-neutral, but response_format values should be literal physical forms such as text, number, email or phone when the prose says them.
8. Include a real entry event and point it to the first behavior.
9. Draft the graph, walk every path from entry, audit source coverage/connectivity/termination, correct your draft, and return only the corrected result.
10. Do not create skip/no-op nodes. A branch that skips an optional action connects directly to the shared continuation.

Examples from unrelated domains:
- Warehouse prose: "Scan the parcel code, then choose Fragile or Standard. Fragile goes to manual packing; both go to dispatch." Correct: scan node, choice node, manual-pack node, dispatch node; Standard connects directly to dispatch. Mistake: a synthetic "skip packing" node.
- Library prose: "Show 'Renewal received' and finish as renewed." Correct: one message node carrying user_facing_text='Renewal received', followed by a separate terminal node carrying terminal_outcome='renewed'. Mistake: combining the message and termination in one node.
- Safety prose: "Ask 'How many people?' and capture a number from 1 to 20." Correct facts: prompt, response_format=number, validation minimum=1 maximum=20, user_text_status=supplied.
- Intake prose: "Collect the person's email address." Correct: one capture behavior with response_format=email and user_text_status=absent. Mistake: inventing a visible prompt.
- Identifier prose: "Collect the person's email address." Correct: no provided_variable_name fact. "Collect the email and save it as contact_email." Correct: provided_variable_name=contact_email.
- Routing prose: "If approved, continue to verification." Correct: relationship only; no synthetic user text.
- Delivery prose: "Choose Home or Office. For Office ask for a desk number. Then confirm." Correct: the confirm node has incoming relationships from Home and from desk number; it is not placed before desk number.
- Course prose: "If Pass issue a certificate; if Fail show retry guidance; both end." Correct: both branch behaviors have terminal continuations. Mistake: leaving Fail dangling.
- Maintenance prose: "When ticket MX9 arrives by email, notify the supervisor." Correct entry facts: trigger_value=MX9 and communication_channel=email. Mistake: turning the trigger into a normal action node.
"""


def binder_schema() -> dict[str, Any]:
    capabilities = list(REGISTRY)
    return {
        "type": "object",
        "properties": {
            "bindings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "semantic_node_id": {"type": "string"},
                        "status": {"type": "string", "enum": ["bound", "unsupported"]},
                        "capability_id": {"anyOf": [{"type": "string", "enum": capabilities}, {"type": "null"}]},
                        "rationale": {"type": "string"},
                    },
                    "required": ["semantic_node_id", "status", "capability_id", "rationale"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["bindings"],
        "additionalProperties": False,
    }


BINDER_SYSTEM = """You are a restricted capability selector.
For each semantic node, select exactly one capability from the supplied real registry or mark it unsupported.
Use only the node behavior and details. Do not alter, merge, split, reorder or repair nodes. Do not map fields; registry-owned deterministic code does that after selection.
Return exactly one binding per semantic node and no extra bindings.
"""


def exact_graph_errors(graph: dict[str, Any], prose: str) -> list[str]:
    errors: list[str] = []
    nodes = graph["nodes"]
    ids = [node["id"] for node in nodes]
    if len(ids) != len(set(ids)):
        errors.append("duplicate_node_id")
    known = set(ids)
    if graph["entry_event"]["target_node_id"] not in known:
        errors.append("invalid_entry_target")
    spans = [graph["entry_event"]["source_span"]]
    spans.extend(detail["source_span"] for detail in graph["entry_event"]["details"])
    for node in nodes:
        spans.append(node["source_span"])
        spans.extend(detail["source_span"] for detail in node["details"])
        text_details = [
            detail for detail in node["details"]
            if detail["kind"] == "user_facing_text"
        ]
        expected_text_status = "supplied" if text_details else "absent"
        if node.get("user_text_status") != expected_text_status:
            errors.append(f"user_text_status_mismatch:{node['id']}")
    for edge in graph["relationships"]:
        spans.append(edge["source_span"])
        if edge["from_node_id"] not in known or edge["to_node_id"] not in known:
            errors.append(f"invalid_edge:{edge['id']}")
    source = prose
    errors.extend(f"ungrounded_span:{span}" for span in spans if span not in source)
    return errors


def topological_order(graph: dict[str, Any]) -> list[str]:
    ids = [node["id"] for node in graph["nodes"]]
    position = {node_id: index for index, node_id in enumerate(ids)}
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {node_id: 0 for node_id in ids}
    for edge in graph["relationships"]:
        outgoing[edge["from_node_id"]].append(edge["to_node_id"])
        indegree[edge["to_node_id"]] += 1
    ready = [node_id for node_id in ids if indegree[node_id] == 0]
    ready.sort(key=position.get)
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=position.get)
    if len(result) != len(ids):
        raise RuntimeError("P4_ATOMIC_GRAPH_CYCLE")
    return result


def branch_priority_topological_order(
    graph: dict[str, Any],
    bindings: dict[str, str],
    configs: dict[str, dict[str, Any]],
) -> list[str]:
    """Respect dependencies while preferring the registry option order."""

    ids = [node["id"] for node in graph["nodes"]]
    position = {node_id: index for index, node_id in enumerate(ids)}
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    indegree = {node_id: 0 for node_id in ids}
    ranks: dict[str, set[tuple[int, ...]]] = defaultdict(set)
    ranks[graph["entry_event"]["target_node_id"]].add(())
    for edge in graph["relationships"]:
        outgoing[edge["from_node_id"]].append(edge)
        indegree[edge["to_node_id"]] += 1

    def ready_key(node_id: str) -> tuple[tuple[int, ...], int]:
        return (min(ranks[node_id]) if ranks[node_id] else (10**6,), position[node_id])

    ready = [node_id for node_id in ids if indegree[node_id] == 0]
    ready.sort(key=ready_key)
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        current_ranks = ranks[current]
        if not current_ranks:
            raise RuntimeError(f"P4_ATOMIC_NODE_UNREACHABLE:{current}")
        option_ranks: dict[str, int] = {}
        if bindings[current] == "fixed_choice":
            option_ranks = {
                str(option["label"]).casefold(): index
                for index, option in enumerate(configs[current].get("options", []))
            }
        explicit_conditions = {
            str(edge["condition"]).casefold()
            for edge in outgoing[current]
            if edge.get("condition") is not None
        }
        for edge in outgoing[current]:
            target = edge["to_node_id"]
            if option_ranks:
                condition = edge.get("condition")
                selected = (
                    [str(condition).casefold()]
                    if condition is not None
                    else [label for label in option_ranks if label not in explicit_conditions]
                )
                if not selected or any(label not in option_ranks for label in selected):
                    raise RuntimeError(f"P4_ATOMIC_CHOICE_EDGE_INVALID:{current}:{condition}")
                ranks[target].update(
                    (*rank, option_ranks[label])
                    for rank in current_ranks
                    for label in selected
                )
            else:
                ranks[target].update(current_ranks)
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
        ready.sort(key=ready_key)
    if len(result) != len(ids):
        raise RuntimeError("P4_ATOMIC_GRAPH_CYCLE")
    return result


def stable_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "answer"


def stable_gap_id(node_id: str, capability: str, field_path: str) -> str:
    digest = hashlib.sha256(
        f"{node_id}\0{capability}\0{field_path}".encode("utf-8")
    ).hexdigest()[:12].upper()
    return f"GAP-{digest}"


def _node_facts(node: dict[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for detail in node["details"]:
        normalized = dict(detail)
        if detail["kind"] == "validation_rule" and detail["validation"] is not None:
            normalized["values"] = [
                {key: value for key, value in detail["validation"].items() if value is not None}
            ]
        facts.append(normalized)
    return facts


def _gap_context(
    graph: dict[str, Any],
    node: dict[str, Any],
    capability: str,
    field_path: str,
    known_values: dict[str, Any],
) -> dict[str, Any]:
    nodes = {item["id"]: item for item in graph["nodes"]}
    incoming = [
        {
            "behavior": nodes[edge["from_node_id"]]["behavior"],
            "condition": edge.get("condition"),
        }
        for edge in graph["relationships"]
        if edge["to_node_id"] == node["id"]
    ]
    outgoing = [
        {
            "behavior": nodes[edge["to_node_id"]]["behavior"],
            "condition": edge.get("condition"),
        }
        for edge in graph["relationships"]
        if edge["from_node_id"] == node["id"]
    ]
    field = next(item for item in require_capability(capability).fields if item.path == field_path)
    return {
        "gap_id": stable_gap_id(node["id"], capability, field_path),
        "semantic_node_id": node["id"],
        "capability_id": capability,
        "field_path": field_path,
        "behavior": node["behavior"],
        "source_span": node["source_span"],
        "previous_behaviors": incoming,
        "next_behaviors": outgoing,
        "known_configuration": known_values,
        "registry_question": field.question,
        "answer_type": field.answer_type,
        "permitted_options": list(field.options),
        "accepted_fact_kinds": [item.value for item in field.accepted_fact_kinds],
    }


def bind_configurations(
    graph: dict[str, Any],
    bindings: dict[str, str],
    clarification_answers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind facts and answers using registry-owned acquisition requirements."""

    answers = clarification_answers or {}
    nodes = {node["id"]: node for node in graph["nodes"]}
    source_order = topological_order(graph)
    configs: dict[str, dict[str, Any]] = {}
    acquisition_sources: dict[str, dict[str, str]] = {}
    gaps: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for node_id in source_order:
        node = nodes[node_id]
        capability = bindings[node_id]
        facts = _node_facts(node)
        values = bind_semantic_facts(capability, facts)
        fact_routes = semantic_fact_field_map(capability)
        provenance = {
            fact_routes[detail["kind"]].path: "confirmed_prose"
            for detail in facts
            if detail["kind"] in fact_routes
        }
        generated_fields = {
            field.path
            for field in require_capability(capability).fields
            if workbench_field_policy(capability, field.path) is AcquisitionPolicy.GENERATED
        }
        for field_path in generated_fields:
            values.pop(field_path, None)
            provenance.pop(field_path, None)

        if capability == "capture_user_input":
            if "required" not in values:
                values["required"] = True
                provenance["required"] = "defaulted:meaning-workbench"
            if "validation" not in values:
                values["validation"] = {}
                provenance["validation"] = "defaulted:meaning-workbench"
            if "save_as" in generated_fields:
                base = stable_slug(node["behavior"])[:48]
                save_as = base
                suffix = 2
                while save_as in used_names:
                    save_as = f"{base}_{suffix}"
                    suffix += 1
                values["save_as"] = save_as
                provenance["save_as"] = "generated:meaning-workbench"
            used_names.add(str(values["save_as"]))
        elif capability == "send_text_message" and "locale" not in values:
            values["locale"] = "en"
            provenance["locale"] = "defaulted:meaning-workbench"

        missing_fields = required_semantic_fact_gaps(capability, values)
        for field_path in missing_fields:
            gap_id = stable_gap_id(node_id, capability, field_path)
            if gap_id not in answers:
                gaps.append(_gap_context(graph, node, capability, field_path, values))
                continue
            field = next(
                item for item in require_capability(capability).fields
                if item.path == field_path
            )
            values[field_path] = validate_registry_field_value(field, answers[gap_id])
            provenance[field_path] = f"clarification_answer:{gap_id}"

        configs[node_id] = values
        acquisition_sources[node_id] = provenance

    unknown_answers = sorted(set(answers) - {
        stable_gap_id(node_id, bindings[node_id], field_path)
        for node_id, values in configs.items()
        for field_path in required_semantic_fact_gaps(bindings[node_id], {
            key: value for key, value in values.items()
            if not acquisition_sources[node_id].get(key, "").startswith("clarification_answer:")
        })
    })
    if unknown_answers:
        raise ValueError("P4_ATOMIC_UNKNOWN_CLARIFICATION_ANSWERS:" + ",".join(unknown_answers))

    return {
        "configs": configs,
        "acquisition_sources": acquisition_sources,
        "gaps": gaps,
    }


QUESTION_BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gap_id": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["gap_id", "question"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


QUESTION_SYSTEM = """You phrase configuration questions for a non-technical user.
You receive the complete capability-neutral flow and one complete list of registry-confirmed missing fields.
Return exactly one clear, contextual question for every supplied gap_id, in the same order.
Use the behavior, neighboring behaviors, known options and response format to make each question understandable.
Do not add gaps, answer a question, suggest copy, change logic, expose capability IDs or mention internal field names.
"""


def phrase_configuration_questions(
    api_key: str,
    project: str,
    graph: dict[str, Any],
    gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not gaps:
        return [], {"seconds": 0, "usage": {}, "request_id": None}
    raw, call = provider_call(
        api_key,
        project,
        name="atomic_configuration_question_batch",
        system=QUESTION_SYSTEM,
        user=json.dumps({"atomic_flow": graph, "configuration_gaps": gaps}, ensure_ascii=False),
        schema=QUESTION_BATCH_SCHEMA,
    )
    questions = raw["questions"]
    expected = [item["gap_id"] for item in gaps]
    actual = [item["gap_id"] for item in questions]
    if actual != expected or len(actual) != len(set(actual)):
        raise RuntimeError("P4_ATOMIC_CONFIGURATION_QUESTION_COVERAGE_INVALID")
    return questions, call


def compile_plan(
    graph: dict[str, Any],
    bindings: dict[str, str],
    clarification_answers: dict[str, Any] | None = None,
    *,
    prose: str,
    plan_id: str = "MP-atomic-long-experiment",
) -> tuple[MeaningPlan, dict[str, Any]]:
    nodes = {node["id"]: node for node in graph["nodes"]}
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in graph["relationships"]:
        outgoing[edge["from_node_id"]].append(edge)

    configuration = bind_configurations(graph, bindings, clarification_answers)
    configs = configuration["configs"]
    if configuration["gaps"]:
        raise RuntimeError("P4_ATOMIC_CONFIGURATION_GAPS_UNANSWERED")

    order = branch_priority_topological_order(graph, bindings, configs)
    incoming_paths: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    incoming_paths[graph["entry_event"]["target_node_id"]].add(())
    for node_id in order:
        capability = bindings[node_id]
        values = configs[node_id]
        paths = incoming_paths[node_id]
        if not paths:
            raise RuntimeError(f"P4_ATOMIC_NODE_UNREACHABLE:{node_id}")
        if capability == "fixed_choice":
            option_map = {
                str(option["label"]).casefold(): str(option["value"])
                for option in values.get("options", [])
            }
            explicit_conditions = {
                str(edge["condition"]).casefold()
                for edge in outgoing[node_id]
                if edge.get("condition") is not None
            }
            for edge in outgoing[node_id]:
                condition = edge.get("condition")
                selected = (
                    [str(condition).casefold()]
                    if condition is not None
                    else [label for label in option_map if label not in explicit_conditions]
                )
                if not selected or any(label not in option_map for label in selected):
                    raise RuntimeError(f"P4_ATOMIC_CHOICE_EDGE_INVALID:{node_id}:{condition}")
                incoming_paths[edge["to_node_id"]].update(
                    (*path, option_map[label])
                    for path in paths
                    for label in selected
                )
        else:
            for edge in outgoing[node_id]:
                if edge.get("condition") is not None:
                    raise RuntimeError(f"P4_ATOMIC_NON_CHOICE_CONDITION:{node_id}")
                incoming_paths[edge["to_node_id"]].update(paths)

    trigger_facts = {detail["kind"]: detail for detail in graph["entry_event"]["details"]}
    trigger_values = trigger_facts.get("trigger_value", {}).get("values", [])
    trigger = FlowTriggerIntent(
        status="explicit" if trigger_values else "none",
        keywords=[
            TriggerKeywordIntent(
                value=value,
                source_excerpt=trigger_facts["trigger_value"]["source_span"],
            )
            for value in trigger_values
        ],
    )
    source = prose
    steps = []
    for ordinal, node_id in enumerate(order, 1):
        node = nodes[node_id]
        paths = tuple(sorted(incoming_paths[node_id]))
        steps.append(MeaningPlanStep(
            id=node_id,
            ordinal=ordinal,
            creation_ordinal=ordinal,
            capability=bindings[node_id],
            branch_path=paths[0],
            branch_paths=paths,
            semantic_subject=node["behavior"],
            source_instruction=source,
            source_excerpt=node["source_span"],
            supplied_values=configs[node_id],
            acquisition_sources=configuration["acquisition_sources"][node_id],
            status=MeaningStepStatus.VERIFIED,
        ))
    plan = MeaningPlan(
        id=plan_id,
        steps=steps,
        trigger_intent=trigger,
        status=MeaningStepStatus.VERIFIED,
    )
    return plan, {
        "fact_gaps": {},
        "configs": configs,
        "acquisition_sources": configuration["acquisition_sources"],
        "order": order,
    }


def _validated_binding_map(graph: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, str]:
    node_ids = {node["id"] for node in graph["nodes"]}
    binding_ids = [row["semantic_node_id"] for row in rows]
    if len(binding_ids) != len(set(binding_ids)) or set(binding_ids) != node_ids:
        raise RuntimeError("P4_ATOMIC_BINDING_COVERAGE_INVALID")
    unsupported = [
        row for row in rows
        if row["status"] != "bound" or row["capability_id"] is None
    ]
    if unsupported:
        raise RuntimeError(
            "P4_ATOMIC_UNSUPPORTED_NODES:"
            + ",".join(str(row["semantic_node_id"]) for row in unsupported)
        )
    return {
        str(row["semantic_node_id"]): str(row["capability_id"])
        for row in rows
    }


def create_atomic_checkpoint(
    prose: str,
    *,
    api_key: str,
    project: str,
    session_id: str,
) -> tuple[dict[str, Any], MeaningPlan | None]:
    """Decompose, bind and either pause once or return a verified plan."""

    graph, graph_call = provider_call(
        api_key,
        project,
        name="atomic_semantic_graph",
        system=GRAPH_SYSTEM,
        user=prose,
        schema=GRAPH_SCHEMA,
    )
    graph_errors = exact_graph_errors(graph, prose)
    if graph_errors:
        raise RuntimeError("P4_ATOMIC_GRAPH_INVALID:" + ",".join(graph_errors))
    bound, binder_call = provider_call(
        api_key,
        project,
        name="atomic_capability_bindings",
        system=BINDER_SYSTEM,
        user=json.dumps({"registry": registry_payload(), "graph": graph}, ensure_ascii=False),
        schema=binder_schema(),
    )
    rows = bound["bindings"]
    binding_map = _validated_binding_map(graph, rows)
    configuration = bind_configurations(graph, binding_map)
    checkpoint: dict[str, Any] = {
        "schema_version": "product4-atomic-workbench-1.0",
        "status": "awaiting_configuration" if configuration["gaps"] else "ready",
        "prose": prose,
        "graph": graph,
        "bindings": rows,
        "configuration_gaps": configuration["gaps"],
        "questions": [],
        "answers": {},
        "calls": {"graph": graph_call, "binder": binder_call},
    }
    if configuration["gaps"]:
        questions, question_call = phrase_configuration_questions(
            api_key,
            project,
            graph,
            configuration["gaps"],
        )
        checkpoint["questions"] = questions
        checkpoint["calls"]["questions"] = question_call
        return checkpoint, None
    plan, compilation = compile_plan(
        graph,
        binding_map,
        prose=prose,
        plan_id=f"MP-{session_id}",
    )
    checkpoint["meaning_plan"] = plan.model_dump(mode="json")
    checkpoint["compilation"] = compilation
    return checkpoint, plan


def complete_atomic_checkpoint(
    checkpoint: dict[str, Any],
    answers: dict[str, Any],
    *,
    session_id: str,
) -> tuple[dict[str, Any], MeaningPlan]:
    if checkpoint.get("status") != "awaiting_configuration":
        raise ValueError("P4_ATOMIC_CHECKPOINT_NOT_AWAITING_CONFIGURATION")
    questions = checkpoint.get("questions") or []
    expected = [str(item["gap_id"]) for item in questions]
    if set(answers) != set(expected) or len(answers) != len(expected):
        raise ValueError("P4_ATOMIC_ANSWER_BATCH_INCOMPLETE")
    graph = checkpoint["graph"]
    binding_map = _validated_binding_map(graph, checkpoint["bindings"])
    plan, compilation = compile_plan(
        graph,
        binding_map,
        answers,
        prose=str(checkpoint["prose"]),
        plan_id=f"MP-{session_id}",
    )
    completed = dict(checkpoint)
    completed.update({
        "status": "ready",
        "answers": dict(answers),
        "meaning_plan": plan.model_dump(mode="json"),
        "compilation": compilation,
    })
    return completed, plan
