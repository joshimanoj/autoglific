from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from experiments.atomic_meaning import runner as base
from product4.authoring.freeze import freeze, prepare_confirmation
from product4.authoring.interpreter import RegistryInterpreter
from product4.authoring.atomic_projection import project_meaning_plan, validate_meaning_plan
from product4.authoring.review import authored_mermaid_review, expanded_mermaid_review
from product4.authoring.session import AuthoringService
from product4.contracts.package_boundary import (
    canonical_authoring_package_hash,
    validate_frozen_package,
)
from product4.contracts.session import AuthoringSession, SessionState
from product4.workbench.pipeline import run_pipeline


PROSE = (
    'When a user sends TRAIN on WhatsApp, start the community workshop registration flow. '
    'Send "Welcome to community workshop registration." '
    'Ask "Would you like to register for a workshop?" and let them select exactly one of Yes or No. '
    'If they select No, send "No problem. You can register another time." and end with the outcome declined_workshop_registration. '
    'If they select Yes, collect their full name as a text response. '
    'Then ask "Which workshop would you like to attend?" and let them select exactly one of First Aid or Digital Literacy. '
    'Next ask "How would you like to attend?" and let them select exactly one of Online or In Person. '
    'If they select In Person, ask "Which learning centre do you prefer?" and let them select exactly one of North Centre or South Centre. '
    'After either attendance mode, collect an email response for the workshop confirmation. '
    'Then collect a phone response for workshop updates. '
    'Send "Thank you. Your workshop registration has been recorded." and end with the outcome workshop_registration_recorded.'
)


CANNED_PROMPTS = {
    "full name": "What is your full name?",
    "email": "What email address should receive your workshop confirmation?",
    "phone": "What phone number should we use for workshop updates?",
}


OUTPUT_ROOT = Path("/tmp/atomic-meaning-not-happy-medium")
CONFIRMATION_TIME = datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc)


def _validate_bindings(graph: dict, rows: list[dict]) -> dict[str, str]:
    node_ids = {node["id"] for node in graph["nodes"]}
    binding_ids = [row["semantic_node_id"] for row in rows]
    if len(binding_ids) != len(set(binding_ids)) or set(binding_ids) != node_ids:
        raise RuntimeError("P4_EXPERIMENT_BINDING_COVERAGE_INVALID")
    unsupported = [
        row for row in rows
        if row["status"] != "bound" or row["capability_id"] is None
    ]
    if unsupported:
        raise RuntimeError("P4_EXPERIMENT_UNSUPPORTED_NODES")
    return {row["semantic_node_id"]: row["capability_id"] for row in rows}


def _canned_answers(gaps: list[dict]) -> dict[str, str]:
    answers: dict[str, str] = {}
    for gap in gaps:
        if gap["field_path"] != "prompt":
            raise RuntimeError(
                f"P4_EXPERIMENT_UNEXPECTED_CONFIGURATION_GAP:{gap['field_path']}"
            )
        context = f"{gap['behavior']} {gap['source_span']}".casefold()
        matches = [answer for key, answer in CANNED_PROMPTS.items() if key in context]
        if len(matches) != 1:
            raise RuntimeError(
                f"P4_EXPERIMENT_CANNED_ANSWER_AMBIGUOUS:{gap['gap_id']}"
            )
        answers[gap["gap_id"]] = matches[0]
    if len(answers) != len(CANNED_PROMPTS):
        raise RuntimeError("P4_EXPERIMENT_EXPECTED_PROMPT_GAPS_MISSING")
    return answers


def main() -> None:
    api_key, project = base.credentials()
    base.PROSE = PROSE
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    graph, graph_call = base.provider_call(
        api_key,
        project,
        name="atomic_semantic_graph_not_happy_medium",
        system=base.GRAPH_SYSTEM,
        user=PROSE,
        schema=base.GRAPH_SCHEMA,
    )
    graph_errors = base.exact_graph_errors(graph)
    if graph_errors:
        raise RuntimeError("P4_EXPERIMENT_GRAPH_INVALID:" + ",".join(graph_errors))

    bound, binder_call = base.provider_call(
        api_key,
        project,
        name="capability_bindings_not_happy_medium",
        system=base.BINDER_SYSTEM,
        user=json.dumps(
            {"registry": base.registry_payload(), "graph": graph},
            ensure_ascii=False,
        ),
        schema=base.binder_schema(),
    )
    binding_rows = bound["bindings"]
    binding_map = _validate_bindings(graph, binding_rows)
    configuration = base.bind_configurations(graph, binding_map)
    gaps = configuration["gaps"]
    questions, question_call = base.phrase_configuration_questions(
        api_key,
        project,
        graph,
        gaps,
    )
    answers = _canned_answers(gaps)
    plan, compilation = base.compile_plan(graph, binding_map, answers)

    validate_meaning_plan(plan)

    service = AuthoringService(RegistryInterpreter(None), workbench_mode=True)
    session = service.start(
        "atomic-not-happy-medium",
        "Atomic not-happy medium",
        original_brief=PROSE,
    )
    projected = project_meaning_plan(session, plan, service)
    if projected.state is not SessionState.READY_FOR_REVIEW or projected.open_positions:
        raise RuntimeError("P4_EXPERIMENT_PROJECTION_INVALID")

    package, digest = prepare_confirmation(
        projected,
        confirmed_by="bounded-experiment-canned-answers",
        clock=lambda: CONFIRMATION_TIME,
    )
    validated = validate_frozen_package(package)
    if validated.model_dump(mode="json") != package:
        raise RuntimeError("P4_EXPERIMENT_PACKAGE_CANONICALIZATION_MISMATCH")
    if canonical_authoring_package_hash(package) != digest:
        raise RuntimeError("P4_EXPERIMENT_PACKAGE_HASH_MISMATCH")
    frozen = freeze(projected, digest, package)
    pipeline = run_pipeline(frozen)
    if not pipeline["all_stages_passed"]:
        raise RuntimeError("P4_EXPERIMENT_DOWNSTREAM_PIPELINE_FAILED")
    engine3 = next(
        stage for stage in pipeline["stages"]
        if stage["name"] == "engine3_glific_artifact"
    )

    artifacts = {
        "checkpoint": OUTPUT_ROOT / "not-happy-medium-checkpoint.json",
        "meaning_plan": OUTPUT_ROOT / "not-happy-medium-meaning-plan.json",
        "authored_mermaid": OUTPUT_ROOT / "not-happy-medium-authored.mmd",
        "expanded_mermaid": OUTPUT_ROOT / "not-happy-medium-expanded.mmd",
        "package": OUTPUT_ROOT / "not-happy-medium-authoring-package.json",
        "glific": OUTPUT_ROOT / "not-happy-medium-glific-import.json",
    }
    artifacts["checkpoint"].write_text(json.dumps({
        "prose": PROSE,
        "calls": {
            "graph": graph_call,
            "binder": binder_call,
            "questions": question_call,
        },
        "graph": graph,
        "bindings": binding_rows,
        "configuration_gaps": gaps,
        "question_batch": questions,
        "canned_answers": answers,
        "generated_variable_names": {
            node_id: values["save_as"]
            for node_id, values in compilation["configs"].items()
            if "save_as" in values
        },
    }, ensure_ascii=False, indent=2))
    artifacts["meaning_plan"].write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)
    )
    artifacts["authored_mermaid"].write_text(authored_mermaid_review(frozen, require_frozen=True))
    artifacts["expanded_mermaid"].write_text(expanded_mermaid_review(frozen))
    artifacts["package"].write_text(json.dumps(package, ensure_ascii=False, indent=2))
    artifacts["glific"].write_text(json.dumps(engine3["json"], ensure_ascii=False, indent=2))

    print(json.dumps({
        "status": "passed",
        "atomic_nodes": len(graph["nodes"]),
        "configuration_gaps": len(gaps),
        "questions": questions,
        "canned_answers": answers,
        "generated_variable_names": {
            node_id: values["save_as"]
            for node_id, values in compilation["configs"].items()
            if "save_as" in values
        },
        "meaning_steps": len(plan.steps),
        "authored_nodes": len(projected.nodes),
        "package_nodes": len(package["nodes"]),
        "pipeline_stages": {
            stage["name"]: stage["status"] for stage in pipeline["stages"]
        },
        "engine3_counts": engine3["details"]["structural_report"]["counts"],
        "artifacts": {key: str(value) for key, value in artifacts.items()},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
