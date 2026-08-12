"""Strict source-side contracts used by ``authoring-package-1.0``.

T08 and T09 own executable edges and nodes.  This module owns the small typed
documents that surround them: confirmed source units, resource bindings,
integrations, metadata annotations, and source-coverage entries.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, TypeAlias

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

from .ledger_contracts import Provenance, RequirementId, Sha256Hash
from .ledger_pending_templates import (
    HttpMethod,
    ResourceReference,
    SecretReference,
)
from .source_edge_contracts import EdgeId
from .source_node_contracts import NodeId, SourceUnitId

SOURCE_CONTRACT_VERSION = "authoring-source-contracts-1.0"
SOURCE_PACKAGE_CONTRACT_VERSION = SOURCE_CONTRACT_VERSION

NonEmptyText: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100_000),
]
Identifier: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]*$",
    ),
]
IntegrationReference: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=13,
        max_length=320,
        pattern=r"^integration:[A-Za-z0-9][A-Za-z0-9._~:/-]{0,255}$",
    ),
]
MetadataKey: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]*$",
    ),
]


class SourceContractError(ValueError):
    """Raised when a source-side package contract is malformed."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class SourceUnit(_StrictModel):
    """One confirmed, addressable unit of source prose."""

    id: SourceUnitId = Field(validation_alias=AliasChoices("id", "source_unit_id"))
    text: NonEmptyText = Field(
        validation_alias=AliasChoices("text", "quote", "source_quote", "content")
    )
    source_hash: Sha256Hash | None = None


class SourceDocument(_StrictModel):
    """Confirmed prose and the source units referenced by typed elements."""

    confirmed_prose: NonEmptyText
    source_hash: Sha256Hash
    source_units: list[SourceUnit] = Field(
        default_factory=list,
        validation_alias=AliasChoices("source_units", "units"),
    )

    @model_validator(mode="after")
    def source_units_are_unique_and_bound(self) -> SourceDocument:
        unit_ids = [unit.id for unit in self.source_units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("duplicate source unit IDs are not allowed")
        for unit in self.source_units:
            if unit.source_hash is not None and unit.source_hash != self.source_hash:
                raise ValueError(f"source unit {unit.id} has a mismatched source_hash")
        return self


class ResourceType(str, Enum):
    """Closed resource categories used by source and ledger payloads."""

    MEDIA = "media"
    CONTACT = "contact"
    CONTACT_FIELD = "contact_field"
    COLLECTION = "collection"
    SUBFLOW = "subflow"
    TEMPLATE = "template"
    QUEUE = "queue"
    GENERIC = "generic"


class PackageResource(_StrictModel):
    """A named resource binding; values remain references, never secrets."""

    id: Identifier = Field(validation_alias=AliasChoices("id", "resource_id"))
    type: ResourceType = Field(validation_alias=AliasChoices("type", "kind"))
    ref: ResourceReference = Field(
        validation_alias=AliasChoices("ref", "resource_ref", "reference")
    )
    description: str | None = Field(default=None, max_length=10_000)
    provenance: list[Provenance] = Field(default_factory=list)


class IntegrationType(str, Enum):
    """Closed integration categories supported by the authoring boundary."""

    WEBHOOK_API = "webhook_api"
    HTTP = "http"
    CRM = "crm"
    GENERIC = "generic"


class PackageIntegration(_StrictModel):
    """An integration binding with optional HTTP execution configuration."""

    id: Identifier = Field(validation_alias=AliasChoices("id", "integration_id"))
    type: IntegrationType = Field(validation_alias=AliasChoices("type", "kind"))
    ref: IntegrationReference = Field(
        validation_alias=AliasChoices("ref", "integration_ref", "reference")
    )
    method: HttpMethod | None = None
    url: HttpUrl | None = None
    secret_ref: SecretReference | None = Field(
        default=None,
        validation_alias=AliasChoices("secret_ref", "secret_reference"),
    )
    provenance: list[Provenance] = Field(default_factory=list)

    @model_validator(mode="after")
    def http_fields_are_paired(self) -> PackageIntegration:
        if (self.method is None) != (self.url is None):
            raise ValueError("integration method and url must be supplied together")
        return self


class MetadataType(str, Enum):
    """Closed metadata meanings; metadata cannot carry hidden topology."""

    ANNOTATION = "annotation"
    POLICY = "policy"
    REVIEW = "review"
    EXECUTION = "execution"
    LOCALE = "locale"
    RETRY = "retry"
    TIMEOUT = "timeout"
    CUSTOM = "custom"


class PackageMetadata(_StrictModel):
    """One explicit annotation or versioned policy attached to a package."""

    type: MetadataType = Field(validation_alias=AliasChoices("type", "kind"))
    key: MetadataKey
    value: Any
    provenance: list[Provenance] = Field(default_factory=list)


class SourceCoverageEntry(_StrictModel):
    """Links one source unit to the typed package elements it grounds."""

    source_unit_id: SourceUnitId = Field(
        validation_alias=AliasChoices("source_unit_id", "source_id")
    )
    requirement_ids: list[RequirementId] = Field(
        default_factory=list,
        validation_alias=AliasChoices("requirement_ids", "requirements"),
    )
    node_ids: list[NodeId] = Field(
        default_factory=list,
        validation_alias=AliasChoices("node_ids", "nodes"),
    )
    edge_ids: list[EdgeId] = Field(
        default_factory=list,
        validation_alias=AliasChoices("edge_ids", "edges"),
    )

    @field_validator("requirement_ids", "node_ids", "edge_ids")
    @classmethod
    def identifiers_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("source coverage identifiers must be unique")
        return value

    @model_validator(mode="after")
    def covers_at_least_one_element(self) -> SourceCoverageEntry:
        if not (self.requirement_ids or self.node_ids or self.edge_ids):
            raise ValueError("source coverage must reference at least one package element")
        return self


# Naming aliases keep the source vocabulary stable for later consumers.
Source = SourceDocument
SourceContract = SourceDocument
SourceUnitContract = SourceUnit
Resource = PackageResource
ResourceContract = PackageResource
Integration = PackageIntegration
IntegrationContract = PackageIntegration
Metadata = PackageMetadata
MetadataContract = PackageMetadata
CoverageEntry = SourceCoverageEntry
SourceCoverage = SourceCoverageEntry
PackageSource = SourceDocument


__all__ = [
    "SOURCE_CONTRACT_VERSION",
    "SOURCE_PACKAGE_CONTRACT_VERSION",
    "CoverageEntry",
    "Identifier",
    "Integration",
    "IntegrationContract",
    "IntegrationReference",
    "IntegrationType",
    "Metadata",
    "MetadataContract",
    "MetadataKey",
    "MetadataType",
    "NonEmptyText",
    "PackageIntegration",
    "PackageMetadata",
    "PackageResource",
    "PackageSource",
    "Resource",
    "ResourceContract",
    "ResourceType",
    "Source",
    "SourceContract",
    "SourceContractError",
    "SourceCoverage",
    "SourceCoverageEntry",
    "SourceDocument",
    "SourceUnit",
    "SourceUnitContract",
    "SourceUnitId",
]
