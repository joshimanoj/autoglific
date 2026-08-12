from __future__ import annotations

import hashlib
import html
import json
import re

from product4.capabilities.technical_policy import POLICY, policy_hash
from product4.contracts.package_boundary import (
    canonical_authoring_package_hash,
    validate_frozen_package,
)
from product4.contracts.session import AuthoringSession, SessionState

from .package_builder import expand_package_graph, validate_draft


def _generated(node: dict) -> bool:
    return any(ref["source_quote"].startswith("Generated technical behavior:") for ref in node["source_refs"])


def text_review(session: AuthoringSession) -> str:
    validate_draft(session)
    graph = expand_package_graph(session)
    lines = [
        f"Flow: {session.title}",
        f"Authored nodes: {len(session.nodes)}",
        f"Expanded nodes: {len(graph.nodes)}",
        f"Technical policy: {POLICY.version} ({policy_hash()})",
        f"Policy retry: {POLICY.retry.max_attempts} attempts",
        f"Policy no-response timeout: {POLICY.no_response_timeout_seconds} seconds",
    ]
    if session.flow_trigger_metadata is not None:
        lines.append(
            "Trigger keywords: "
            + json.dumps(
                [item.value for item in session.flow_trigger_metadata.keywords],
                ensure_ascii=False,
            )
        )
    outgoing: dict[str, list[dict]] = {}
    for edge in graph.edges:
        outgoing.setdefault(edge["source_id"], []).append(edge)
    for node in graph.nodes:
        marker = "generated-policy" if _generated(node) else "authored"
        exits = ", ".join(
            f"{edge['role']}->{edge['target_id']}" for edge in sorted(outgoing.get(node["id"], []), key=lambda item: item["id"])
        ) or "terminal"
        execution_values = {
            key: value
            for key, value in node.items()
            if key not in {"id", "type", "label", "source_refs"}
        }
        lines.append(
            f"{node['id']} [{marker}] {node['type']}: {exits}; values="
            + json.dumps(execution_values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    lines.append("Routes:")
    for edge in sorted(graph.edges, key=lambda item: item["id"]):
        route = {
            "id": edge["id"], "source": edge["source_id"],
            "target": edge["target_id"], "role": edge["role"],
            "condition": edge["condition"],
            "generated_policy": all(
                item.get("source") == "approved_versioned_policy"
                for item in edge.get("provenance", [])
            ),
        }
        lines.append(json.dumps(route, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return "\n".join(lines)


def mermaid_review(session: AuthoringSession) -> str:
    """Return both source-of-truth authored and expanded Mermaid views.

    The combined output keeps the historical review contract while making the
    exact authored configuration available to callers that previously saw
    only node types and edge roles.  Use ``authored_mermaid_review`` or
    ``expanded_mermaid_review`` when a distinct view is needed.
    """
    return (
        authored_mermaid_review(session)
        + "\n\n%% Expanded technical-policy graph\n"
        + expanded_mermaid_review(session)
    )


def _mermaid_escape(value: object) -> str:
    """Escape user text for a Mermaid quoted label deterministically."""
    text = str(value).replace("\\", "\\\\")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    text = html.escape(text, quote=True)
    return (
        text.replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
        .replace("|", "&#124;")
    )


def _canonical_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _display_value(value: object) -> str:
    return _mermaid_escape(_canonical_value(value))


def _safe_node_ids(session: AuthoringSession) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in session.nodes:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", node.id):
            result[node.id] = node.id
        else:
            digest = hashlib.sha256(node.id.encode("utf-8")).hexdigest()[:12]
            result[node.id] = f"node_{digest}"
    return result


def _authored_node_lines(node: object) -> list[str]:
    capability = node.capability
    config = node.config
    fields: list[tuple[str, object]]
    if capability == "send_text_message":
        fields = [("copy", config.get("copy")), ("locale", config.get("locale"))]
    elif capability == "capture_user_input":
        fields = [
            ("prompt", config.get("prompt")),
            ("input_type", config.get("input_type")),
            ("save_as", config.get("save_as")),
            ("required", config.get("required")),
            ("validation", config.get("validation")),
        ]
    elif capability == "fixed_choice":
        fields = [("title", config.get("title")), ("options", config.get("options"))]
    elif capability == "persist_contact_field":
        fields = [
            ("source_variable", config.get("source_variable")),
            ("field_name", config.get("field_name")),
        ]
    elif capability == "end":
        fields = [("reason", config.get("reason"))]
    else:
        fields = sorted(config.items(), key=lambda item: item[0])
    return [f"{key}={_display_value(value)}" for key, value in fields]


def authored_mermaid_review(
    session: AuthoringSession,
    *,
    require_frozen: bool = False,
) -> str:
    """Render the exact authored graph from the session's approved values.

    This deliberately uses only ``session.nodes`` and ``session.edges``.  It
    does not infer or copy downstream Engine topology.  Frozen callers can ask
    for a package/hash check so the displayed graph is bound to the frozen
    authoring revision.
    """
    if require_frozen:
        if session.state is not SessionState.FROZEN or not session.frozen_package or not session.frozen_hash:
            raise ValueError("P4_FROZEN_REVIEW_REQUIRED")
        package = validate_frozen_package(session.frozen_package)
        if canonical_authoring_package_hash(package) != session.frozen_hash:
            raise ValueError("P4_FROZEN_REVIEW_HASH_MISMATCH")

    node_ids = _safe_node_ids(session)
    lines = [
        "flowchart TD",
        "  %% Authored semantic graph: exact approved Product 4 configuration.",
    ]
    for node in session.nodes:
        label_lines = [
            _mermaid_escape(f"{node.id} · {node.capability}"),
            *_authored_node_lines(node),
        ]
        label = "<br/>".join(label_lines)
        lines.append(f'  {node_ids[node.id]}["{label}"]')
        lines.append(f"  %% authored-config {node.id}: {_canonical_value(node.config)}")
    for edge in session.edges:
        if edge.stable_value is not None or edge.label is not None:
            label = (
                f"{edge.label or edge.exit_key} · stable_value={edge.stable_value or ''}"
            )
        else:
            label = edge.exit_key
        lines.append(
            f'  {node_ids[edge.source_id]} -->|"{_mermaid_escape(label)}"| '
            f"{node_ids[edge.target_id]}"
        )
        lines.append(
            f"  %% authored-edge {edge.id}: "
            f"{_canonical_value(edge.model_dump(mode='json'))}"
        )
    return "\n".join(lines)


def authored_semantic_mermaid_review(
    session: AuthoringSession,
    *,
    require_frozen: bool = False,
) -> str:
    """Explicitly named alias for the default frozen semantic view."""
    return authored_mermaid_review(session, require_frozen=require_frozen)


_PRESENTATION_TRIGGER_RE = re.compile(
    r"\b(?:keyword|trigger(?:\s+word)?)\s+['\"“‘]?([^'\"”’.,;:!?]+)",
    re.IGNORECASE,
)
_PRESENTATION_INTERNAL_RE = re.compile(
    r"\b(?:source_variable|save_as|stable_value|capture_reference|"
    r"input_type|field_name|locale|validation|capability|node_id|"
    r"translation_id|position_id)\b|\b(?:N|E|POS|OPT)(?:-|\d+)[A-Z0-9_-]*\b",
    re.IGNORECASE,
)


def _presentation_wrap(value: object, *, width: int = 58) -> str:
    """Escape and wrap a user-facing label without changing its wording."""
    text = str(value or "").strip()
    if not text:
        return ""
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        next_length = length + (1 if current else 0) + len(word)
        if current and next_length > width:
            lines.append(" ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length = next_length
    if current:
        lines.append(" ".join(current))
    return "<br/>".join(_mermaid_escape(line) for line in lines)


def _presentation_trigger(session: AuthoringSession) -> str | None:
    trigger_state = session.flow_trigger_metadata
    if trigger_state is not None and trigger_state.keywords:
        values = " or ".join(item.value for item in trigger_state.keywords)
        return f"User sends {values}"
    source = session.original_brief
    if not source and session.nodes:
        first = session.nodes[0]
        source = first.source_excerpt or first.source_statement
    if not source:
        return None
    match = _PRESENTATION_TRIGGER_RE.search(source)
    if not match:
        return None
    trigger = match.group(1).strip(" *'\"“”‘’")
    return f"User sends {trigger}" if trigger else None


def _presentation_business_action(node: object) -> str:
    for candidate in (node.source_excerpt, node.source_statement):
        source = str(candidate or "").strip()
        if source and not _PRESENTATION_INTERNAL_RE.search(source):
            return source
    return "Save the captured answer"


def _presentation_node_label(node: object) -> str:
    config = node.config
    if node.capability == "send_text_message":
        return str(config.get("copy") or "Message")
    if node.capability == "capture_user_input":
        return str(config.get("prompt") or "Question")
    if node.capability == "fixed_choice":
        return str(config.get("title") or "Choose an option")
    if node.capability == "persist_contact_field":
        return _presentation_business_action(node)
    if node.capability == "end":
        return "End"
    return _presentation_business_action(node)


def authored_presentation_mermaid(
    session: AuthoringSession,
    *,
    require_frozen: bool = False,
) -> str:
    """Render a plain-language projection of the approved authored graph.

    This is deliberately a presentation-only projection.  The exact authored
    Mermaid returned by :func:`authored_mermaid_review` remains the technical
    source used by the frozen checkpoint and downstream engines.
    """
    if require_frozen:
        if session.state is not SessionState.FROZEN or not session.frozen_package or not session.frozen_hash:
            raise ValueError("P4_FROZEN_REVIEW_REQUIRED")
        package = validate_frozen_package(session.frozen_package)
        if canonical_authoring_package_hash(package) != session.frozen_hash:
            raise ValueError("P4_FROZEN_REVIEW_HASH_MISMATCH")

    if not session.nodes:
        return "flowchart TD\n  empty[\"Your flow will appear here\"]"

    # End reasons and authored IDs are intentionally not projected.  Each
    # terminal keeps its own visual destination so nested branches remain
    # readable beneath their parent in the presentation graph.
    node_ids: dict[str, str] = {}
    next_step = 1
    next_end = 1
    for node in session.nodes:
        if node.capability == "end":
            node_ids[node.id] = f"presentation_end_{next_end:03d}"
            next_end += 1
        else:
            node_ids[node.id] = f"presentation_step_{next_step:03d}"
            next_step += 1

    lines = [
        "flowchart TD",
        "  %% Plain-language top-down journey; exact technical graph is in Advanced details.",
        "  classDef triggerNode fill:#eaf1ff,stroke:#3564df,stroke-width:2.5px,color:#17223b;",
        "  classDef choiceNode fill:#f1edff,stroke:#866bea,stroke-width:2.5px,color:#17223b;",
        "  classDef normalNode fill:#ffffff,stroke:#9eabe0,stroke-width:2px,color:#17223b;",
        "  classDef terminalNode fill:#e8f8f1,stroke:#28a377,stroke-width:2.5px,color:#17223b;",
    ]
    trigger = _presentation_trigger(session)
    if trigger:
        lines.append(f'  presentation_trigger(["{_presentation_wrap(trigger)}"])')

    declared: set[str] = set()
    class_nodes: dict[str, list[str]] = {"triggerNode": [], "choiceNode": [], "normalNode": [], "terminalNode": []}
    if trigger:
        class_nodes["triggerNode"].append("presentation_trigger")
    for node in session.nodes:
        presentation_id = node_ids[node.id]
        if presentation_id in declared:
            continue
        node_class = "terminalNode" if node.capability == "end" else "choiceNode" if node.capability == "fixed_choice" else "normalNode"
        shape = "([\"{label}\"])" if node.capability in {"fixed_choice", "end"} else "[\"{label}\"]"
        lines.append(f'  {presentation_id}{shape.format(label=_presentation_wrap(_presentation_node_label(node)))}')
        class_nodes[node_class].append(presentation_id)
        declared.add(presentation_id)

    for class_name, ids in class_nodes.items():
        if ids:
            lines.append(f"  class {','.join(ids)} {class_name};")

    if trigger:
        first_id = node_ids[session.nodes[0].id]
        lines.append(f"  presentation_trigger --> {first_id}")
    for edge in session.edges:
        source = node_ids[edge.source_id]
        target = node_ids[edge.target_id]
        source_node = next(node for node in session.nodes if node.id == edge.source_id)
        if source_node.capability == "fixed_choice" and edge.label:
            lines.append(
                f'  {source} -->|"{_presentation_wrap(edge.label)}"| {target}'
            )
        else:
            lines.append(f"  {source} --> {target}")
    return "\n".join(lines)


def expanded_mermaid_review(session: AuthoringSession) -> str:
    """Render the existing expanded authored + technical-policy graph."""
    graph = expand_package_graph(session)
    lines = [
        "flowchart TD",
        "  %% Expanded technical-policy graph; generated nodes are marked policy.",
    ]
    for node in graph.nodes:
        marker = "policy" if _generated(node) else "authored"
        label = f"{node['id']}: {node['type']} ({marker})"
        lines.append(f'  {node["id"]}["{label}"]')
    for edge in sorted(graph.edges, key=lambda item: item["id"]):
        lines.append(f'  {edge["source_id"]} -->|"{edge["role"]}"| {edge["target_id"]}')
    return "\n".join(lines)
