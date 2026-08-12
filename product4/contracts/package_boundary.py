"""Pinned import boundary for Product 4's frozen authoring package contract."""

from __future__ import annotations

from typing import Any

from product4.contracts.authoring_package.package_contracts import (
    AUTHORING_PACKAGE_SCHEMA_VERSION,
    AuthoringPackage,
    canonical_authoring_package_hash,
    canonical_authoring_package_json,
    canonical_package_schema_hash,
)


def validate_frozen_package(value: dict[str, Any]) -> AuthoringPackage:
    return AuthoringPackage.model_validate(value)


__all__ = [
    "AUTHORING_PACKAGE_SCHEMA_VERSION",
    "AuthoringPackage",
    "canonical_authoring_package_hash",
    "canonical_authoring_package_json",
    "canonical_package_schema_hash",
    "validate_frozen_package",
]
