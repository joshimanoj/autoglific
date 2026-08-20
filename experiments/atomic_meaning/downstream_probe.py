from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from product4.authoring.freeze import prepare_confirmation
from product4.authoring.interpreter import RegistryInterpreter
from product4.authoring.atomic_projection import project_meaning_plan
from product4.authoring.review import authored_mermaid_review
from product4.authoring.session import AuthoringService
from product4.contracts.atomic_meaning import MeaningPlan
from product4.contracts.package_boundary import (
    canonical_authoring_package_hash,
    validate_frozen_package,
)
from product4.contracts.session import SessionState


CONFIRMATION_TIME = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def probe(result_path: Path, output_root: Path) -> dict[str, Any]:
    source = json.loads(result_path.read_text())
    prose = str(source["prose"])
    plan = MeaningPlan.model_validate(source["meaning_plan"])

    service = AuthoringService(RegistryInterpreter(None), workbench_mode=True)
    session = service.start(
        f"downstream-{source.get('case') or result_path.stem}",
        f"Downstream probe: {source.get('case') or result_path.stem}",
        original_brief=prose,
    )
    projected = project_meaning_plan(session, plan, service)
    if projected.state is not SessionState.READY_FOR_REVIEW:
        raise RuntimeError("P4_EXPERIMENT_PROJECT_NOT_READY_FOR_REVIEW")
    if projected.open_positions:
        raise RuntimeError("P4_EXPERIMENT_PROJECT_OPEN_POSITIONS")

    mermaid = authored_mermaid_review(projected)
    if not mermaid.startswith("flowchart TD"):
        raise RuntimeError("P4_EXPERIMENT_AUTHORED_MERMAID_INVALID")

    package, digest = prepare_confirmation(
        projected,
        confirmed_by="bounded-experiment-reviewer",
        clock=lambda: CONFIRMATION_TIME,
    )
    validated = validate_frozen_package(package)
    if canonical_authoring_package_hash(package) != digest:
        raise RuntimeError("P4_EXPERIMENT_PACKAGE_HASH_MISMATCH")
    if validated.model_dump(mode="json") != package:
        raise RuntimeError("P4_EXPERIMENT_PACKAGE_CANONICALIZATION_MISMATCH")

    output_root.mkdir(parents=True, exist_ok=True)
    stem = result_path.stem.removesuffix("-result")
    session_path = output_root / f"{stem}-projected-session.json"
    mermaid_path = output_root / f"{stem}-authored.mmd"
    package_path = output_root / f"{stem}-authoring-package.json"
    session_path.write_text(json.dumps(projected.model_dump(mode="json"), ensure_ascii=False, indent=2))
    mermaid_path.write_text(mermaid)
    package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2))

    summary = {
        "input": str(result_path),
        "meaning_steps": len(plan.steps),
        "authored_nodes": len(projected.nodes),
        "authored_edges": len(projected.edges),
        "session_state": projected.state.value,
        "open_positions": len(projected.open_positions),
        "mermaid_lines": len(mermaid.splitlines()),
        "package_schema": package["schema_version"],
        "package_nodes": len(package["nodes"]),
        "package_edges": len(package["edges"]),
        "package_requirements": len(package["ledger"]["requirements"]),
        "package_decisions": len(package["ledger"]["decisions"]),
        "package_hash": digest,
        "artifacts": {
            "session": str(session_path),
            "mermaid": str(mermaid_path),
            "package": str(package_path),
        },
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("/tmp/atomic-meaning-downstream"))
    args = parser.parse_args()
    summaries = []
    for path in args.results:
        summary = probe(path, args.output_root)
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    print(json.dumps({"cases": summaries}, ensure_ascii=False))


if __name__ == "__main__":
    main()
