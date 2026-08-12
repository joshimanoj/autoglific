from __future__ import annotations

from typing import Any

from app.config import settings

CAPABILITIES_PATH = settings.project_root / "contracts" / "glific-capabilities-verified-0.1.json"


DEFAULT_CAPABILITIES = {
    "contract_version": "glific-import-verified-0.1",
    "glific_spec_version": "13.1.0",
    "primitives": {
        "send_text_message": {"enabled_local": True, "enabled_staging": True},
        "wait_for_text_response": {"enabled_local": True, "enabled_staging": True},
        "exact_categorical_branch": {"enabled_local": True, "enabled_staging": True},
        "number_branch": {"enabled_local": True, "enabled_staging": True},
        "flow_result_interpolation": {"enabled_local": True, "enabled_staging": True},
        "end_terminal": {"enabled_local": True, "enabled_staging": True},
        "quick_reply": {"enabled_local": True, "enabled_staging": True},
        "list_message": {"enabled_local": True, "enabled_staging": True},
        "call_webhook": {"enabled_local": False, "enabled_staging": False},
        "update_contact_field": {"enabled_local": False, "enabled_staging": False},
        "add_remove_collection": {"enabled_local": False, "enabled_staging": False},
        "wait_for_time": {"enabled_local": False, "enabled_staging": False},
        "enter_child_flow": {"enabled_local": False, "enabled_staging": False},
        "open_ticket": {"enabled_local": False, "enabled_staging": False},
    },
}


def load_capabilities() -> dict[str, Any]:
    if CAPABILITIES_PATH.is_file():
        import json

        try:
            return json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return DEFAULT_CAPABILITIES


def primitive_for_node_kind(kind: str) -> str:
    return {
        "send_message": "send_text_message",
        "wait_for_response": "wait_for_text_response",
        "switch": "exact_categorical_branch",
        "end": "end_terminal",
        "call_webhook": "call_webhook",
        "update_contact": "update_contact_field",
        "add_to_collection": "add_remove_collection",
        "remove_from_collection": "add_remove_collection",
        "wait_for_time": "wait_for_time",
        "enter_flow": "enter_child_flow",
        "open_ticket": "open_ticket",
    }.get(kind, kind)


def enabled_for_local(kind: str) -> bool:
    primitive = load_capabilities().get("primitives", {}).get(primitive_for_node_kind(kind), {})
    return bool(primitive.get("enabled_local", False))


def enabled_for_staging(kind: str) -> bool:
    primitive = load_capabilities().get("primitives", {}).get(primitive_for_node_kind(kind), {})
    return bool(primitive.get("enabled_staging", False))
