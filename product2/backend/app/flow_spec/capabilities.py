from __future__ import annotations

import json
from typing import Any

from app.config import settings

CAPABILITIES_PATH = settings.project_root / "contracts" / "glific-flow-spec-capabilities-1.0.json"


DEFAULT_FLOW_SPEC_CAPABILITIES: dict[str, Any] = {
    "capability_version": "glific-flow-spec-capabilities-1.0",
    "target_contract": "glific-import-verified-0.1",
    "nodes": {
        "send_message": {"enabled_local": True, "compiler_mapping": "send_message"},
        "send_media": {"enabled_local": False, "compiler_mapping": "send_media"},
        "ask_choice": {
            "enabled_local": True,
            "compiler_mapping": "interactive_wait_router",
            "presentations": ["quick_reply", "list"],
            "quick_reply_max": 3,
            "list_max": 10,
        },
        "ask_input": {"enabled_local": True, "compiler_mapping": "wait_router"},
        "evaluate": {"enabled_local": True, "compiler_mapping": "switch"},
        "call_webhook": {"enabled_local": False, "compiler_mapping": "webhook"},
        "update_contact": {"enabled_local": False, "compiler_mapping": "contact_update"},
        "record_request": {
            "enabled_local": True,
            "compiler_mapping": "native_set_contact_field",
            "mechanisms": ["contact_fields"],
            "requires_resource": True,
            "staging_mutation": False,
        },
        "handoff": {"enabled_local": False, "compiler_mapping": "handoff"},
        "delay": {"enabled_local": False, "compiler_mapping": "delay"},
        "enter_subflow": {"enabled_local": False, "compiler_mapping": "enter_subflow"},
        "end": {"enabled_local": True, "compiler_mapping": "end"},
    },
    "input_parsers": {
        "plain_text": {"enabled_local": True},
        "integer": {"enabled_local": True},
        "local_12_hour_time": {"enabled_local": True},
        "email": {"enabled_local": True},
        "phone": {"enabled_local": True},
    },
    "interactive_limits": {"quick_reply_max": 3, "list_max": 10},
    "interactive_selection": {
        "runtime_matcher": "visible_title",
        "stable_value_lowering": "native_set_run_result",
    },
}


def load_flow_spec_capabilities() -> dict[str, Any]:
    if CAPABILITIES_PATH.is_file():
        try:
            return json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return DEFAULT_FLOW_SPEC_CAPABILITIES


def node_capability(node_type: str) -> dict[str, Any]:
    return load_flow_spec_capabilities().get("nodes", {}).get(node_type, {})


def node_enabled_locally(node_type: str) -> bool:
    return bool(node_capability(node_type).get("enabled_local", False))


def parser_enabled_locally(parser: str) -> bool:
    return bool(
        load_flow_spec_capabilities()
        .get("input_parsers", {})
        .get(parser, {})
        .get("enabled_local", False)
    )
