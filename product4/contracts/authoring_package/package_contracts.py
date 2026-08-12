"""Canonical ``authoring-package-1.0`` envelope and deterministic helpers.

The package is the hard boundary between authoring and deterministic engines.
It embeds the T07 frozen typed ledger and reuses the closed T08/T09 edge and
node unions.  No legacy ``FlowNode`` or ``source-flow-0.1`` contract is
imported here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    model_validator,
)
from pydantic_core import core_schema

from .capabilities import CAPABILITY_PROFILE_VERSION
from .ledger_contracts import (
    Decision,
    LedgerStatus,
    Provenance,
    canonical_ledger_hash,
)
from .ledger_merge import (
    CombinedRequirementsLedger,
    FrozenCombinedRequirementsLedger,
    freeze_ledger,
)
from .source_contracts import (
    PackageIntegration,
    PackageMetadata,
    PackageResource,
    SourceCoverageEntry,
    SourceDocument,
)
from .source_edge_contracts import SourceEdgeUnion
from .source_node_contracts import SourceNodeUnion

AUTHORING_PACKAGE_SCHEMA_VERSION = "authoring-package-1.0"
PACKAGE_SCHEMA_VERSION = AUTHORING_PACKAGE_SCHEMA_VERSION
AUTHORING_PACKAGE_VERSION = AUTHORING_PACKAGE_SCHEMA_VERSION
AUTHORING_PACKAGE_CONTRACT_VERSION = AUTHORING_PACKAGE_SCHEMA_VERSION
PACKAGE_CONTRACT_VERSION = AUTHORING_PACKAGE_SCHEMA_VERSION


class PackageIssueCode(str, Enum):
    """Closed issue vocabulary used by explicit package checks."""

    DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"
    DUPLICATE_EDGE_ID = "DUPLICATE_EDGE_ID"
    DUPLICATE_REQUIREMENT_ID = "DUPLICATE_REQUIREMENT_ID"
    DUPLICATE_RESOURCE_ID = "DUPLICATE_RESOURCE_ID"
    DUPLICATE_INTEGRATION_ID = "DUPLICATE_INTEGRATION_ID"
    DUPLICATE_SOURCE_UNIT_ID = "DUPLICATE_SOURCE_UNIT_ID"
    DUPLICATE_COVERAGE_SOURCE_UNIT_ID = "DUPLICATE_COVERAGE_SOURCE_UNIT_ID"
    DANGLING_NODE_ROUTE = "DANGLING_NODE_ROUTE"
    DANGLING_EDGE_ENDPOINT = "DANGLING_EDGE_ENDPOINT"
    DANGLING_SOURCE_UNIT = "DANGLING_SOURCE_UNIT"
    DANGLING_REQUIREMENT = "DANGLING_REQUIREMENT"
    DANGLING_RESOURCE = "DANGLING_RESOURCE"
    DANGLING_INTEGRATION = "DANGLING_INTEGRATION"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    LEDGER_NOT_FROZEN = "LEDGER_NOT_FROZEN"
    LEDGER_HASH_MISMATCH = "LEDGER_HASH_MISMATCH"
    CAPABILITY_PROFILE_MISMATCH = "CAPABILITY_PROFILE_MISMATCH"


class PackageContractError(ValueError):
    """Raised by non-Pydantic package helpers for a typed package issue."""

    def __init__(self, code: PackageIssueCode, message: str):
        self.code = code
        self.issue_code = code.value
        super().__init__(f"{code.value}: {message}")


class FrozenLedgerContract(FrozenCombinedRequirementsLedger):
    """Pydantic-aware field type retaining T07's immutable ledger view."""

    @classmethod
    def _coerce(cls, value: Any) -> FrozenLedgerContract:
        if isinstance(value, cls):
            return value
        if isinstance(value, FrozenCombinedRequirementsLedger):
            return cls(value.to_mutable_ledger())
        if isinstance(value, CombinedRequirementsLedger):
            if value.status is not LedgerStatus.FROZEN:
                raise ValueError("authoring package requires an already frozen ledger")
            return cls(freeze_ledger(value).to_mutable_ledger())
        raise TypeError("authoring package requires a typed frozen combined ledger")

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> core_schema.CoreSchema:
        ledger_schema = handler.generate_schema(CombinedRequirementsLedger)
        accepted = core_schema.union_schema(
            [
                core_schema.is_instance_schema(FrozenCombinedRequirementsLedger),
                ledger_schema,
            ]
        )
        return core_schema.no_info_after_validator_function(
            cls._coerce,
            accepted,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: value.to_mutable_ledger().model_dump(mode="json")
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_value: core_schema.CoreSchema,
        handler: Any,
    ) -> dict[str, Any]:
        return handler(CombinedRequirementsLedger.__pydantic_core_schema__)


class AuthoringPackage(BaseModel):
    """The closed, execution-complete authoring package envelope."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    schema_version: Literal[AUTHORING_PACKAGE_SCHEMA_VERSION]
    source: SourceDocument
    ledger: FrozenLedgerContract = Field(
        validation_alias=AliasChoices("ledger", "frozen_ledger", "requirements_ledger")
    )
    nodes: list[SourceNodeUnion] = Field(min_length=1)
    edges: list[SourceEdgeUnion] = Field(default_factory=list)
    resources: list[PackageResource] = Field(default_factory=list)
    integrations: list[PackageIntegration] = Field(default_factory=list)
    metadata: list[PackageMetadata] = Field(default_factory=list)
    source_coverage: list[SourceCoverageEntry] = Field(
        default_factory=list,
        validation_alias=AliasChoices("source_coverage", "coverage"),
    )
    capability_profile_version: Literal[CAPABILITY_PROFILE_VERSION]

    @model_validator(mode="before")
    @classmethod
    def normalize_top_level_ledger_shape(cls, value: Any) -> Any:
        """Accept the design-note top-level ledger spelling as an input alias."""

        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        has_nested_ledger = any(
            key in data for key in ("ledger", "frozen_ledger", "requirements_ledger")
        )
        if "requirements" not in data and "decisions" not in data:
            return data
        if has_nested_ledger:
            raise ValueError("package cannot provide both ledger and top-level requirements")
        source = data.get("source")
        source_hash = source.get("source_hash") if isinstance(source, Mapping) else None
        requirements = data.pop("requirements", None)
        decisions = data.pop("decisions", [])
        ledger = {
            "schema_version": data.pop("ledger_schema_version", "requirements-ledger-1.0"),
            "id": data.pop("ledger_id", "LEDGER-AUTHORING-PACKAGE"),
            "source_hash": data.pop("ledger_source_hash", source_hash),
            "requirements": requirements,
            "decisions": decisions,
            "status": data.pop("ledger_status", "frozen"),
            "confirmation": data.pop("confirmation", None),
            "frozen_hash": data.pop("frozen_hash", None),
            "revision": data.pop("ledger_revision", 1),
        }
        data["ledger"] = ledger
        return data

    @model_validator(mode="after")
    def validate_package_integrity(self) -> AuthoringPackage:
        ledger = self.ledger.to_mutable_ledger()
        if ledger.status is not LedgerStatus.FROZEN:
            raise ValueError(f"{PackageIssueCode.LEDGER_NOT_FROZEN.value}: ledger must be frozen")
        ledger_hash = canonical_ledger_hash(ledger)
        if ledger.frozen_hash != ledger_hash:
            raise ValueError(
                f"{PackageIssueCode.LEDGER_HASH_MISMATCH.value}: frozen_hash does not match ledger"
            )
        if ledger.source_hash != self.source.source_hash:
            raise ValueError(
                f"{PackageIssueCode.SOURCE_HASH_MISMATCH.value}: source and ledger hashes differ"
            )
        self._validate_collection_ids()
        self._validate_node_and_edge_references()
        self._validate_source_references()
        self._validate_ledger_provenance(ledger)
        self._validate_coverage(ledger)
        self._validate_external_bindings(ledger)
        return self

    def _validate_collection_ids(self) -> None:
        _require_unique_ids(
            [node.id for node in self.nodes],
            PackageIssueCode.DUPLICATE_NODE_ID,
            "nodes",
        )
        _require_unique_ids(
            [edge.id for edge in self.edges],
            PackageIssueCode.DUPLICATE_EDGE_ID,
            "edges",
        )
        _require_unique_ids(
            [resource.id for resource in self.resources],
            PackageIssueCode.DUPLICATE_RESOURCE_ID,
            "resources",
        )
        _require_unique_ids(
            [integration.id for integration in self.integrations],
            PackageIssueCode.DUPLICATE_INTEGRATION_ID,
            "integrations",
        )
        coverage_ids = [entry.source_unit_id for entry in self.source_coverage]
        _require_unique_ids(
            coverage_ids,
            PackageIssueCode.DUPLICATE_COVERAGE_SOURCE_UNIT_ID,
            "source coverage source_unit_id",
        )
        metadata_keys = [(entry.type.value, entry.key) for entry in self.metadata]
        if len(metadata_keys) != len(set(metadata_keys)):
            raise ValueError("duplicate metadata type/key pairs are not allowed")

    def _validate_node_and_edge_references(self) -> None:
        node_ids = {node.id for node in self.nodes}
        for node in self.nodes:
            for route_id in getattr(node, "route_node_ids", ()):
                if route_id not in node_ids:
                    raise ValueError(
                        f"{PackageIssueCode.DANGLING_NODE_ROUTE.value}: "
                        f"node {node.id} references unknown route {route_id}"
                    )
        for edge in self.edges:
            if edge.source_id not in node_ids or edge.target_id not in node_ids:
                raise ValueError(
                    f"{PackageIssueCode.DANGLING_EDGE_ENDPOINT.value}: "
                    f"edge {edge.id} references an unknown source or target node"
                )

    def _validate_source_references(self) -> None:
        source_unit_ids = {unit.id for unit in self.source.source_units}
        for node in self.nodes:
            for source_ref in node.source_refs:
                if source_ref.source_unit_id not in source_unit_ids:
                    raise ValueError(
                        f"{PackageIssueCode.DANGLING_SOURCE_UNIT.value}: "
                        f"node {node.id} references unknown source unit {source_ref.source_unit_id}"
                    )

    def _validate_ledger_provenance(self, ledger: CombinedRequirementsLedger) -> None:
        source_hash = self.source.source_hash
        provenance_values: list[Provenance] = []
        for requirement in ledger.requirements:
            provenance_values.extend(requirement.provenance)
        for decision in ledger.decisions:
            provenance_values.append(decision.provenance)
        for edge in self.edges:
            provenance_values.extend(edge.provenance)
        for resource in self.resources:
            provenance_values.extend(resource.provenance)
        for integration in self.integrations:
            provenance_values.extend(integration.provenance)
        for metadata in self.metadata:
            provenance_values.extend(metadata.provenance)
        for provenance in provenance_values:
            if provenance.source_hash is not None and provenance.source_hash != source_hash:
                raise ValueError(
                    f"{PackageIssueCode.SOURCE_HASH_MISMATCH.value}: "
                    "a provenance source_hash differs from package source_hash"
                )

    def _validate_coverage(self, ledger: CombinedRequirementsLedger) -> None:
        requirement_ids = {requirement.id for requirement in ledger.requirements}
        node_ids = {node.id for node in self.nodes}
        edge_ids = {edge.id for edge in self.edges}
        source_unit_ids = {unit.id for unit in self.source.source_units}
        for entry in self.source_coverage:
            if entry.source_unit_id not in source_unit_ids:
                raise ValueError(
                    f"{PackageIssueCode.DANGLING_SOURCE_UNIT.value}: "
                    f"coverage references unknown source unit {entry.source_unit_id}"
                )
            if missing := set(entry.requirement_ids) - requirement_ids:
                raise ValueError(
                    f"{PackageIssueCode.DANGLING_REQUIREMENT.value}: "
                    f"coverage references unknown requirements {sorted(missing)}"
                )
            if missing := set(entry.node_ids) - node_ids:
                raise ValueError(
                    f"{PackageIssueCode.DANGLING_NODE_ROUTE.value}: "
                    f"coverage references unknown nodes {sorted(missing)}"
                )
            if missing := set(entry.edge_ids) - edge_ids:
                raise ValueError(
                    f"{PackageIssueCode.DANGLING_EDGE_ENDPOINT.value}: "
                    f"coverage references unknown edges {sorted(missing)}"
                )

    def _validate_external_bindings(self, ledger: CombinedRequirementsLedger) -> None:
        resource_refs = {resource.ref for resource in self.resources}
        integration_refs = {integration.ref for integration in self.integrations}

        for node in self.nodes:
            node_payload = node.model_dump(mode="json")
            _validate_binding_fields(
                node_payload,
                resource_refs=resource_refs,
                integration_refs=integration_refs,
                owner=f"node {node.id}",
            )
        for requirement in ledger.requirements:
            requirement_payload = requirement.model_dump(mode="json")
            _validate_binding_fields(
                requirement_payload,
                resource_refs=resource_refs,
                integration_refs=integration_refs,
                owner=f"requirement {requirement.id}",
            )

    @property
    def frozen_ledger(self) -> FrozenLedgerContract:
        """Compatibility spelling for callers that name the field explicitly."""

        return self.ledger

    @property
    def requirements(self) -> tuple[Any, ...]:
        """Expose typed ledger requirements without duplicating package data."""

        return tuple(self.ledger.requirements)

    @property
    def decisions(self) -> tuple[Decision, ...]:
        """Expose typed ledger decisions without duplicating package data."""

        return tuple(self.ledger.decisions)

    @property
    def coverage(self) -> tuple[SourceCoverageEntry, ...]:
        return tuple(self.source_coverage)

    @property
    def package_hash(self) -> str:
        return canonical_authoring_package_hash(self)


class AuthoringPackageDocument(RootModel[AuthoringPackage]):
    """Root-model adapter for callers that want a document wrapper."""


def _require_unique_ids(values: list[str], code: PackageIssueCode, label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{code.value}: duplicate {label} are not allowed")


def _validate_binding_fields(
    value: Any,
    *,
    resource_refs: set[str],
    integration_refs: set[str],
    owner: str,
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(child, str):
                if key in {
                    "resource_ref",
                    "binding",
                    "collection_ref",
                    "subflow_ref",
                    "template_ref",
                    "queue",
                } and child not in resource_refs:
                    raise ValueError(
                        f"{PackageIssueCode.DANGLING_RESOURCE.value}: "
                        f"{owner} references unbound resource {child}"
                    )
                if key == "integration_ref" and child not in integration_refs:
                    raise ValueError(
                        f"{PackageIssueCode.DANGLING_INTEGRATION.value}: "
                        f"{owner} references unbound integration {child}"
                    )
            _validate_binding_fields(
                child,
                resource_refs=resource_refs,
                integration_refs=integration_refs,
                owner=owner,
            )
    elif isinstance(value, list):
        for child in value:
            _validate_binding_fields(
                child,
                resource_refs=resource_refs,
                integration_refs=integration_refs,
                owner=owner,
            )


def _as_package(value: AuthoringPackage | Mapping[str, Any] | str | bytes | bytearray) -> AuthoringPackage:
    if isinstance(value, AuthoringPackage):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        return AuthoringPackage.model_validate_json(value)
    return AuthoringPackage.model_validate(value)


def _canonical_ledger_document(ledger: FrozenLedgerContract) -> dict[str, Any]:
    payload = ledger.to_mutable_ledger().model_dump(mode="json")
    payload["requirements"] = sorted(payload["requirements"], key=lambda item: item["id"])
    payload["decisions"] = sorted(payload["decisions"], key=lambda item: item["id"])
    return payload


def canonical_authoring_package_payload(
    value: AuthoringPackage | Mapping[str, Any] | str | bytes | bytearray,
) -> dict[str, Any]:
    """Return the sorted, lifecycle-complete package payload."""

    package = _as_package(value)
    return {
        "capability_profile_version": package.capability_profile_version,
        "edges": sorted(
            (edge.model_dump(mode="json") for edge in package.edges),
            key=lambda item: item["id"],
        ),
        "integrations": sorted(
            (integration.model_dump(mode="json") for integration in package.integrations),
            key=lambda item: item["id"],
        ),
        "ledger": _canonical_ledger_document(package.ledger),
        "metadata": sorted(
            (metadata.model_dump(mode="json") for metadata in package.metadata),
            key=lambda item: (item["type"], item["key"]),
        ),
        "nodes": sorted(
            (node.model_dump(mode="json") for node in package.nodes),
            key=lambda item: item["id"],
        ),
        "resources": sorted(
            (resource.model_dump(mode="json") for resource in package.resources),
            key=lambda item: item["id"],
        ),
        "schema_version": package.schema_version,
        "source": {
            "confirmed_prose": package.source.confirmed_prose,
            "source_hash": package.source.source_hash,
            "source_units": sorted(
                (unit.model_dump(mode="json") for unit in package.source.source_units),
                key=lambda item: item["id"],
            ),
        },
        "source_coverage": sorted(
            (entry.model_dump(mode="json") for entry in package.source_coverage),
            key=lambda item: item["source_unit_id"],
        ),
    }


def canonical_authoring_package_json(
    value: AuthoringPackage | Mapping[str, Any] | str | bytes | bytearray,
) -> str:
    return json.dumps(
        canonical_authoring_package_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_authoring_package_hash(
    value: AuthoringPackage | Mapping[str, Any] | str | bytes | bytearray,
) -> str:
    return hashlib.sha256(
        canonical_authoring_package_json(value).encode("utf-8")
    ).hexdigest()


def serialize_authoring_package_json(
    value: AuthoringPackage | Mapping[str, Any] | str | bytes | bytearray,
) -> str:
    """Validate and serialize a package in canonical form."""

    return canonical_authoring_package_json(value)


def parse_authoring_package_json(
    value: str | bytes | bytearray,
) -> AuthoringPackage:
    return AuthoringPackage.model_validate_json(value)


def parse_authoring_package(
    value: AuthoringPackage | Mapping[str, Any] | str | bytes | bytearray,
) -> AuthoringPackage:
    return _as_package(value)


def canonical_package_schema_json() -> str:
    return json.dumps(
        AuthoringPackage.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_package_schema_hash() -> str:
    return hashlib.sha256(canonical_package_schema_json().encode("utf-8")).hexdigest()


def validate_authoring_package(
    value: AuthoringPackage | Mapping[str, Any] | str | bytes | bytearray,
) -> AuthoringPackage:
    """Explicit validation entry point used by later deterministic engines."""

    return _as_package(value)


# Compatibility spellings follow the plan and the high-level design note.
AuthoringPackageContract = AuthoringPackage
SourcePackage = AuthoringPackage
FrozenAuthoringPackage = AuthoringPackage
PackageContract = AuthoringPackage
PackageSource = SourceDocument
FrozenRequirementsLedger = FrozenLedgerContract
FrozenLedger = FrozenLedgerContract
PackageResourceContract = PackageResource
PackageIntegrationContract = PackageIntegration
PackageMetadataContract = PackageMetadata
SourceCoverageContract = SourceCoverageEntry
canonical_package_json = canonical_authoring_package_json
canonical_package_hash = canonical_authoring_package_hash
serialize_package_json = serialize_authoring_package_json
parse_package_json = parse_authoring_package_json
authoring_package_schema_json = canonical_package_schema_json
authoring_package_schema_hash = canonical_package_schema_hash


__all__ = [
    "AUTHORING_PACKAGE_CONTRACT_VERSION",
    "AUTHORING_PACKAGE_SCHEMA_VERSION",
    "AUTHORING_PACKAGE_VERSION",
    "PACKAGE_CONTRACT_VERSION",
    "PACKAGE_SCHEMA_VERSION",
    "AuthoringPackage",
    "AuthoringPackageContract",
    "AuthoringPackageDocument",
    "FrozenAuthoringPackage",
    "FrozenLedger",
    "FrozenLedgerContract",
    "FrozenRequirementsLedger",
    "PackageContract",
    "PackageContractError",
    "PackageIntegrationContract",
    "PackageIssueCode",
    "PackageMetadataContract",
    "PackageResourceContract",
    "PackageSource",
    "SourceCoverageContract",
    "SourcePackage",
    "authoring_package_schema_hash",
    "authoring_package_schema_json",
    "canonical_authoring_package_hash",
    "canonical_authoring_package_json",
    "canonical_authoring_package_payload",
    "canonical_package_hash",
    "canonical_package_json",
    "canonical_package_schema_hash",
    "canonical_package_schema_json",
    "parse_authoring_package",
    "parse_authoring_package_json",
    "parse_package_json",
    "serialize_authoring_package_json",
    "serialize_package_json",
    "validate_authoring_package",
]
