from __future__ import annotations

import hashlib

from product4.capabilities.registry import ExitDefinition, require_capability
from product4.contracts.session import DraftNode, OpenPosition


def positions_for_node(node: DraftNode, incoming: OpenPosition) -> list[OpenPosition]:
    return positions_from_exit(node, incoming, require_capability(node.capability).exit)


def positions_from_exit(
    node: DraftNode,
    incoming: OpenPosition,
    exit_definition: ExitDefinition,
) -> list[OpenPosition]:
    if exit_definition.kind == "terminal":
        return []
    if exit_definition.kind == "linear":
        exits = [("next", None)]
    else:
        exits = [(str(item["value"]), str(item["value"])) for item in node.config[exit_definition.source_field or "options"]]
    result: list[OpenPosition] = []
    for exit_key, branch in exits:
        digest = hashlib.sha256(f"{node.id}:{exit_key}".encode()).hexdigest()[:12].upper()
        result.append(OpenPosition(
            id=f"POS-{digest}", parent_node_id=node.id, exit_key=exit_key,
            branch_path=(*incoming.branch_path, branch) if branch else incoming.branch_path,
        ))
    return result


def next_position(positions: list[OpenPosition]) -> OpenPosition:
    if not positions:
        raise ValueError("P4_NO_OPEN_POSITION")
    return positions[0]
