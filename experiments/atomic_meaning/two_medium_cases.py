from __future__ import annotations

import json
import runpy
from pathlib import Path


BASE = runpy.run_path(
    str(Path(__file__).with_name("runner.py")),
    run_name="atomic_two_medium_base",
)


CASES = {
    "course_registration": (
        'When a user sends COURSE on WhatsApp, start the community course registration flow. '
        'Send "Welcome to community learning registration." '
        'Ask "Would you like to register for a course?" and let them select exactly one of Yes or No. '
        'If they select No, send "No problem. You can register another time." and then end with the outcome declined_course_registration. '
        'If they select Yes, ask "Which course would you like?" and let them select exactly one of Literacy, Digital Skills, or Bookkeeping. '
        'Then ask "How would you like to attend?" and let them select exactly one of Online or In Person. '
        'If they select Online, ask "What email should receive the joining link?" and capture an email response. '
        'If they select In Person, ask "Which learning centre do you prefer?" and let them select exactly one of North Centre or South Centre. '
        'After either attendance mode, ask "Would you like to apply for a scholarship?" and let them select exactly one of Yes or No. '
        'If they select Yes for the scholarship, ask "What is your monthly household income?" and capture a number response. '
        'After either scholarship choice, ask "What phone number should we use for course updates?" and capture a phone response. '
        'Then send "Thank you. Your course registration has been recorded." and then end with the outcome course_registration_recorded.'
    ),
    "relief_intake": (
        'When a user sends AID on WhatsApp, start the community relief intake flow. '
        'Send "Welcome to the community relief request service." '
        'Ask "How many people are in your household?" and capture a number response. '
        'Then ask "What kind of support do you need?" and let them select exactly one of Food, Medical, or Shelter. '
        'If they select Food, ask "Do you have any dietary requirements?" and capture a text response. '
        'If they select Medical, ask "Please describe the medical support needed." and capture a text response. '
        'If they select Shelter, ask "Which area do you currently live in?" and capture a text response. '
        'After every support type, ask "How urgent is this request?" and let them select exactly one of High or Normal. '
        'If they select High, send "Your request will be marked for priority review." '
        'After either urgency choice, ask "What phone number can our team call?" and capture a phone response. '
        'Then ask "May our team contact you about this request?" and let them select exactly one of Yes or No. '
        'If they select Yes, send "Thank you. Your relief request has been recorded." and then end with the outcome relief_request_recorded. '
        'If they select No, send "Understood. We will not contact you." and then end with the outcome contact_not_authorized.'
    ),
}


def execute_case(name: str, prose: str, api_key: str, project: str) -> dict:
    BASE["PROSE"] = prose
    BASE["exact_graph_errors"].__globals__["PROSE"] = prose
    BASE["compile_plan"].__globals__["PROSE"] = prose
    graph, graph_call = BASE["provider_call"](
        api_key,
        project,
        name=f"atomic_semantic_graph_{name}",
        system=BASE["GRAPH_SYSTEM"],
        user=prose,
        schema=BASE["GRAPH_SCHEMA"],
    )
    checkpoint_path = BASE["OUTPUT_ROOT"] / f"autoglific-atomic-{name}-checkpoint.json"
    checkpoint = {
        "case": name,
        "prose": prose,
        "credentials": {
            "project": project,
            "key_suffix": BASE["EXPECTED_KEY_SUFFIX"],
            "model": BASE["MODEL"],
            "reasoning": "high",
        },
        "calls": {"graph": graph_call},
        "graph": graph,
    }
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2))
    graph_errors = BASE["exact_graph_errors"](graph)
    if graph_errors:
        raise RuntimeError(f"{name}:P4_EXPERIMENT_GRAPH_INVALID:" + ",".join(graph_errors))

    bound, binder_call = BASE["provider_call"](
        api_key,
        project,
        name=f"capability_bindings_{name}",
        system=BASE["BINDER_SYSTEM"],
        user=json.dumps({"registry": BASE["registry_payload"](), "graph": graph}, ensure_ascii=False),
        schema=BASE["binder_schema"](),
    )
    checkpoint["calls"]["binder"] = binder_call
    checkpoint["bindings"] = bound["bindings"]
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2))

    node_ids = {node["id"] for node in graph["nodes"]}
    binding_rows = bound["bindings"]
    binding_ids = [row["semantic_node_id"] for row in binding_rows]
    if len(binding_ids) != len(set(binding_ids)) or set(binding_ids) != node_ids:
        raise RuntimeError(f"{name}:P4_EXPERIMENT_BINDING_COVERAGE_INVALID")
    unsupported = [row for row in binding_rows if row["status"] != "bound" or row["capability_id"] is None]
    if unsupported:
        raise RuntimeError(
            f"{name}:P4_EXPERIMENT_UNSUPPORTED_NODES:"
            + ",".join(row["semantic_node_id"] for row in unsupported)
        )

    binding_map = {row["semantic_node_id"]: row["capability_id"] for row in binding_rows}
    plan, compilation = BASE["compile_plan"](graph, binding_map)
    BASE["validate_meaning_plan"](plan)
    active: list[dict] = []
    executable_errors: list[str] = []
    result = {
        **checkpoint,
        "compilation": compilation,
        "meaning_plan": plan.model_dump(mode="json"),
        "active_findings": active,
        "executable_errors": executable_errors,
        "summary": {
            "nodes": len(graph["nodes"]),
            "relationships": len(graph["relationships"]),
            "terminal_nodes": sum(node["completion"] == "terminal" for node in graph["nodes"]),
            "all_source_covered": graph["self_audit"]["all_source_covered"],
            "all_nodes_connected": graph["self_audit"]["all_nodes_connected"],
            "all_paths_terminate": graph["self_audit"]["all_paths_terminate"],
            "missing_fact_fields": {
                key: value for key, value in compilation["fact_gaps"].items() if value
            },
            "active_configuration_findings": len(active),
            "executable_errors": executable_errors,
        },
    }
    result_path = BASE["OUTPUT_ROOT"] / f"autoglific-atomic-{name}-result.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    if active or executable_errors or result["summary"]["missing_fact_fields"]:
        raise RuntimeError(f"{name}:P4_EXPERIMENT_VALIDATION_FAILED")
    return result


def main() -> None:
    api_key, project = BASE["credentials"]()
    summaries = {}
    for name, prose in CASES.items():
        result = execute_case(name, prose, api_key, project)
        summaries[name] = {
            **result["summary"],
            "calls": result["calls"],
            "artifact": str(BASE["OUTPUT_ROOT"] / f"autoglific-atomic-{name}-result.json"),
        }
        print(json.dumps({name: summaries[name]}, ensure_ascii=False), flush=True)
    print(json.dumps({"all_cases": summaries}, ensure_ascii=False))


if __name__ == "__main__":
    main()
