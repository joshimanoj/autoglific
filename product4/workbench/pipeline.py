"""Offline Product 4 workbench pipeline orchestration.

This module owns request-level stage reporting only.  Lowering remains in the
existing Engine 1, Engine 2, and Engine 3 adapters.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTOBUILDER_ROOT = PROJECT_ROOT.parent
PRODUCT2_BACKEND = AUTOBUILDER_ROOT / "product2" / "backend"
for import_path in (AUTOBUILDER_ROOT, PRODUCT2_BACKEND):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from product4.authoring.review import (
    authored_mermaid_review,
    expanded_mermaid_review,
)
from product4.capabilities.registry import (
    REGISTRY_VERSION,
    registry_hash,
)
from product4.capabilities.technical_policy import (
    TECHNICAL_POLICY_VERSION,
    policy_hash,
)
from product4.contracts.package_boundary import (
    canonical_authoring_package_hash,
    validate_frozen_package,
)
from product4.contracts.session import AuthoringSession, SessionState
from product4.engine_boundary.engine1_adapter import (
    ingest_frozen_package,
)
from product4.engine_boundary.engine2_adapter import (
    build_compatibility_report,
    build_flow_spec,
)
from product4.engine_boundary.engine3_adapter import (
    build_glific_structural_report,
    compile_glific,
    validate_glific_structure,
)


class WorkbenchPipelineError(ValueError):
    """A typed lifecycle or stage error exposed by the workbench API."""


def canonical_hash(value: Any) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _offline_product2_bundle(graph: Any) -> Any:
    """Build Product 2's offline source witness for its real validator.

    This is not lowering.  It supplies the pinned validator with the source
    lineage already present in the Engine 1 graph, matching the accepted P48
    evidence path without model/provider calls.
    """
    from app.contracts import (
        Product1Bundle,
        Product1Producer,
        Product1ValidationReport,
        SemanticEdge,
        SemanticIR,
        SemanticNode,
        SourceRef,
        SourceUnit,
    )

    units: list[Any] = []
    offset = 0
    for raw_unit in sorted(graph.source_units, key=lambda item: item["id"]):
        text = raw_unit["text"]
        units.append(
            SourceUnit(
                id=raw_unit["id"],
                start_offset=offset,
                end_offset=offset + len(text),
                text=text,
                normalized_text=text,
            )
        )
        offset += len(text) + 1

    nodes_by_id = {node.id: node for node in graph.nodes}
    type_by_capability = {
        "send_text_message": "message",
        "capture_user_input": "input",
        "fixed_choice": "decision",
        "persist_contact_field": "action",
        "end": "end",
        "retry_policy": "action",
    }
    semantic_nodes = [
        SemanticNode(
            id=node.id,
            type=type_by_capability[node.capability],
            label=node.capability,
            source_refs=[
                SourceRef(**ref)
                for ref in sorted(
                    node.source_refs,
                    key=lambda item: (item["source_unit_id"], item["source_quote"]),
                )
            ],
        )
        for node in sorted(graph.nodes, key=lambda item: item.id)
    ]
    source_unit_ids = {unit.id for unit in units}
    semantic_edges: list[Any] = []
    for edge in sorted(graph.edges, key=lambda item: item.id):
        owner_refs = sorted(
            nodes_by_id[edge.source_id].source_refs,
            key=lambda item: (item["source_unit_id"], item["source_quote"]),
        )
        quote = next(
            (
                item.get("quote")
                for item in edge.provenance
                if isinstance(item.get("quote"), str)
            ),
            None,
        )
        edge_ref = next(
            (ref for ref in owner_refs if ref["source_quote"] == quote),
            owner_refs[0],
        )
        if edge_ref["source_unit_id"] not in source_unit_ids:
            raise WorkbenchPipelineError(f"P4_WORKBENCH_SOURCE_REFERENCE_MISSING:{edge.id}")
        semantic_edges.append(
            SemanticEdge(
                id=edge.id,
                from_=edge.source_id,
                to=edge.target_id,
                label=edge.role,
                condition_source_text=(
                    json.dumps(edge.condition, sort_keys=True, separators=(",", ":"))
                    if edge.condition
                    else None
                ),
                source_refs=[SourceRef(**edge_ref)],
            )
        )

    semantic_ir = SemanticIR(
        schema_version="source-flow-0.1",
        source_hash=graph.source_hash,
        title=graph.title,
        nodes=semantic_nodes,
        edges=semantic_edges,
    )
    validation = Product1ValidationReport(
        schema_version="validation-report-0.1",
        source_hash=graph.source_hash,
        attempt=1,
        passed=True,
        renderable=True,
        validator_versions={"offline_witness": "product4-workbench-p48-source-witness-1.0"},
        issues=[],
    )
    return Product1Bundle(
        schema_version="product1-bundle-0.1",
        producer=Product1Producer(
            product="product1",
            session_id="product4-p47-normalized-graph",
            generation_id=graph.source_package_hash,
            revision_number=1,
        ),
        confirmed_requirements="\n".join(unit.text for unit in units),
        source_hash=graph.source_hash,
        source_units=units,
        semantic_ir=semantic_ir,
        product1_validation=validation,
        imported_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


def _stage(
    name: str,
    *,
    status: str,
    payload: Any = None,
    digest: str | None = None,
    input_hash: str | None = None,
    details: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "canonical_hash": digest,
        "input_hash": input_hash,
        "json": payload,
        "details": details or {},
        "error": error,
    }


def _frozen_stage(session: AuthoringSession) -> tuple[dict[str, Any], Any, str]:
    if session.state is not SessionState.FROZEN:
        raise WorkbenchPipelineError("P4_COMPILE_REQUIRES_FROZEN_SESSION")
    if not session.frozen_package or not session.frozen_hash:
        raise WorkbenchPipelineError("P4_FROZEN_PACKAGE_MISSING")
    try:
        package = validate_frozen_package(session.frozen_package)
    except Exception as exc:  # pragma: no cover - defensive boundary response
        raise WorkbenchPipelineError("P4_FROZEN_PACKAGE_INVALID") from exc
    package_hash = canonical_authoring_package_hash(package)
    if package_hash != session.frozen_hash:
        raise WorkbenchPipelineError("P4_FROZEN_PACKAGE_HASH_MISMATCH")
    payload = package.model_dump(mode="json")
    return (
        _stage(
            "frozen_package",
            status="passed",
            payload=payload,
            digest=package_hash,
            details={
                "package_validation": "passed",
                "freeze_status": "frozen",
                "canonical_hash_matches_session": True,
            },
        ),
        package,
        package_hash,
    )


def run_pipeline(session: AuthoringSession) -> dict[str, Any]:
    """Run the accepted offline Engine 1 -> Product 2 -> Engine 3 chain."""
    frozen_stage, package, package_hash = _frozen_stage(session)
    authored_mermaid = authored_mermaid_review(session, require_frozen=True)
    expanded_mermaid = expanded_mermaid_review(session)
    stages = [frozen_stage]

    try:
        graph = ingest_frozen_package(package.model_dump(mode="json"), package_hash)
        engine1_stage = _stage(
            "engine1_graph",
            status="passed",
            payload=graph.model_dump(mode="json"),
            digest=graph.canonical_hash(),
            input_hash=package_hash,
            details={
                "input_package_hash": graph.source_package_hash,
                "registry_version": REGISTRY_VERSION,
                "registry_hash": registry_hash(),
                "technical_policy_version": TECHNICAL_POLICY_VERSION,
                "technical_policy_hash": policy_hash(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - convert any adapter failure to a stage result
        stages.append(
            _stage(
                "engine1_graph",
                status="error",
                input_hash=package_hash,
                error=str(exc),
            )
        )
        stages.extend(
            [
                _stage("engine2_flow_spec", status="blocked", error="Engine 1 failed."),
                _stage("engine3_glific_artifact", status="blocked", error="Engine 1 failed."),
            ]
        )
        return _pipeline_result(session, package_hash, authored_mermaid, expanded_mermaid, stages)
    stages.append(engine1_stage)

    try:
        spec = build_flow_spec(graph)
        validation_report = __import__(
            "app.flow_spec.validation", fromlist=["validate_flow_spec"]
        ).validate_flow_spec(spec, _offline_product2_bundle(graph))
        spec_payload = spec.model_dump(mode="json")
        engine2_passed = bool(validation_report.passed)
        engine2_stage = _stage(
            "engine2_flow_spec",
            status="passed" if engine2_passed else "error",
            payload=spec_payload,
            digest=canonical_hash(spec_payload),
            input_hash=graph.canonical_hash(),
            details={
                "product2_validation_report": validation_report.model_dump(mode="json"),
                "compatibility_report": build_compatibility_report(graph, spec),
            },
            error=None if engine2_passed else "Product 2 Flow Spec validation failed.",
        )
    except Exception as exc:  # noqa: BLE001 - convert any adapter failure to a stage result
        stages.append(
            _stage(
                "engine2_flow_spec",
                status="error",
                input_hash=graph.canonical_hash(),
                error=str(exc),
            )
        )
        stages.append(
            _stage(
                "engine3_glific_artifact",
                status="blocked",
                error="Engine 2 failed.",
            )
        )
        return _pipeline_result(session, package_hash, authored_mermaid, expanded_mermaid, stages)
    stages.append(engine2_stage)

    if engine2_stage["status"] != "passed":
        stages.append(
            _stage(
                "engine3_glific_artifact",
                status="blocked",
                error="Engine 2 Product 2 validation failed.",
            )
        )
        return _pipeline_result(session, package_hash, authored_mermaid, expanded_mermaid, stages)

    try:
        result = compile_glific(spec)
        structural_report = build_glific_structural_report(result, spec)
        if structural_report["passed"]:
            validate_glific_structure(result, spec)
        engine3_stage = _stage(
            "engine3_glific_artifact",
            status="passed" if structural_report["passed"] else "error",
            payload=result["artifact"],
            digest=result["canonical_hash"],
            input_hash=canonical_hash(spec_payload),
            details={
                "compilation_map": result["compilation_map"],
                "metadata": result["metadata"],
                "structural_report": structural_report,
            },
            error=None if structural_report["passed"] else "Product 4 Glific structural validation failed.",
        )
    except Exception as exc:  # noqa: BLE001 - convert any adapter failure to a stage result
        stages.append(
            _stage(
                "engine3_glific_artifact",
                status="error",
                input_hash=canonical_hash(spec_payload),
                error=str(exc),
            )
        )
        return _pipeline_result(session, package_hash, authored_mermaid, expanded_mermaid, stages)
    stages.append(engine3_stage)
    return _pipeline_result(session, package_hash, authored_mermaid, expanded_mermaid, stages)


def _pipeline_result(
    session: AuthoringSession,
    package_hash: str,
    authored_mermaid: str,
    expanded_mermaid: str,
    stages: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "product4-workbench-pipeline-1.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "session_id": session.id,
        "session_revision": session.revision,
        "frozen_package_hash": package_hash,
        "frozen_semantic_checkpoint": {
            "label": "Frozen semantic package",
            "authored_mermaid": authored_mermaid,
            "expanded_mermaid": expanded_mermaid,
            "semantic_verification_note": (
                "This checkpoint supports human semantic verification and proves "
                "authoring topology/config completeness; Mermaid alone does not "
                "mathematically prove that message meaning is correct."
            ),
        },
        "stages": stages,
        "all_stages_passed": all(stage["status"] == "passed" for stage in stages),
    }


__all__ = ["WorkbenchPipelineError", "canonical_hash", "run_pipeline"]
