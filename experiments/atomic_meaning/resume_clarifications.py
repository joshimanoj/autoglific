from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from experiments.atomic_meaning import runner as base


def resume(checkpoint_path: Path, answers_path: Path, output_path: Path) -> dict:
    checkpoint = json.loads(checkpoint_path.read_text())
    answer_payload = json.loads(answers_path.read_text())
    answers = answer_payload.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("P4_EXPERIMENT_CLARIFICATION_ANSWERS_INVALID")

    graph = checkpoint["graph"]
    binding_rows = checkpoint["bindings"]
    binding_map = {
        row["semantic_node_id"]: row["capability_id"]
        for row in binding_rows
        if row["status"] == "bound" and row["capability_id"] is not None
    }
    base.PROSE = str(checkpoint["prose"])
    plan, compilation = base.compile_plan(graph, binding_map, answers)

    base.validate_meaning_plan(plan)
    active: list[dict] = []
    executable_errors: list[str] = []

    result = {
        **checkpoint,
        "status": "ready",
        "clarification_answers": answers,
        "compilation": compilation,
        "meaning_plan": plan.model_dump(mode="json"),
        "active_findings": active,
        "executable_errors": executable_errors,
        "summary": {
            "answered_configuration_gaps": len(answers),
            "generated_variable_names": {
                step.id: step.supplied_values["save_as"]
                for step in plan.steps
                if step.capability == "capture_user_input"
            },
            "active_configuration_findings": len(active),
            "executable_errors": executable_errors,
        },
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("answers", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = resume(args.checkpoint, args.answers, args.output)
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
