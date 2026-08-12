from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCT2_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration.

    The shared AutoBuilder dotenv file is intentionally not configured as a
    Pydantic env file. It is read by ``shared_allowlisted_values`` below so
    only the three permitted keys can ever leave that file.
    """

    model_config = SettingsConfigDict(
        env_file=PRODUCT2_ROOT / ".env", extra="ignore", case_sensitive=False
    )

    product1_base_url: str = "http://127.0.0.1:8000"
    product1_request_timeout_seconds: float = Field(default=30, gt=0, le=120)
    llm_api_key: SecretStr | None = Field(default=None, repr=False)
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=90, gt=0, le=300)
    database_url: str = "sqlite:///./data/product2.sqlite3"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8100, ge=1, le=65535)
    glific_contract_version: str = "glific-import-verified-0.1"
    autobuilder_shared_env_file: str = "../.env"
    glific_api_validation_mode: str = "off"
    glific_staging_base_url: str | None = None
    glific_production_base_url: str | None = None
    glific_staging_organization_name: str | None = None
    glific_staging_confirmation: SecretStr | None = Field(default=None, repr=False)
    glific_staging_phone: SecretStr | None = Field(default=None, repr=False)
    glific_staging_password: SecretStr | None = Field(default=None, repr=False)
    glific_staging_allow_import: bool = False
    glific_staging_allow_publish: bool = False
    glific_staging_flow_name_prefix: str = "product2_validation_"
    glific_staging_allow_cleanup: bool = False
    product2_fake_model: bool = True
    product2_fake_staging: bool = False

    @property
    def project_root(self) -> Path:
        return PRODUCT2_ROOT

    @property
    def shared_env_path(self) -> Path:
        candidate = Path(self.autobuilder_shared_env_file)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        return candidate.resolve()

    def shared_allowlisted_values(self) -> dict[str, str]:
        """Read only the explicitly allowlisted keys from the shared dotenv file."""

        allowed = {"GLIFIC_PHONE", "GLIFIC_PASSWORD", "DEEPSEEK_API_KEY"}
        path = self.shared_env_path
        if not path.is_file():
            return {}
        values: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in allowed:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            values[key] = value
        return values

    def effective_staging_credentials(self) -> tuple[str | None, str | None]:
        shared = self.shared_allowlisted_values()
        phone = self.glific_staging_phone.get_secret_value() if self.glific_staging_phone else None
        password = (
            self.glific_staging_password.get_secret_value()
            if self.glific_staging_password
            else None
        )
        return phone or shared.get("GLIFIC_PHONE"), password or shared.get("GLIFIC_PASSWORD")

    def effective_llm_api_key(self) -> str | None:
        if self.llm_api_key:
            return self.llm_api_key.get_secret_value()
        if self.llm_model and self.llm_base_url.lower().find("deepseek") >= 0:
            return self.shared_allowlisted_values().get("DEEPSEEK_API_KEY")
        return None

    def validate_mode(self) -> None:
        allowed = {"off", "preflight", "staging_import", "runtime_validation"}
        if self.glific_api_validation_mode not in allowed:
            raise ValueError(f"GLIFIC_API_VALIDATION_MODE must be one of {sorted(allowed)}")

    def import_gate_error(self) -> tuple[str, str] | None:
        if self.glific_api_validation_mode not in {"staging_import", "runtime_validation"}:
            return "STAGING_IMPORT_DISABLED", "Staging import requires an explicit import mode."
        if not self.glific_staging_allow_import:
            return "STAGING_IMPORT_DISABLED", "Staging import is disabled by its explicit switch."
        if not self.glific_staging_base_url:
            return "GLIFIC_STAGING_BASE_URL_MISSING", "A dedicated staging base URL is required."
        if not self.glific_staging_organization_name:
            return (
                "GLIFIC_STAGING_ORGANIZATION_MISSING",
                "The dedicated staging organization identity is required.",
            )
        confirmation = (
            self.glific_staging_confirmation.get_secret_value()
            if self.glific_staging_confirmation
            else None
        )
        if confirmation != "I_CONFIRM_DISPOSABLE_GLIFIC_STAGING":
            return (
                "GLIFIC_STAGING_CONFIRMATION_INVALID",
                "The exact disposable-staging confirmation is required before import.",
            )
        if self.glific_production_base_url:
            staging = urlsplit(self.glific_staging_base_url)
            production = urlsplit(self.glific_production_base_url)
            if (staging.scheme.lower(), staging.netloc.lower()) == (
                production.scheme.lower(),
                production.netloc.lower(),
            ):
                return (
                    "GLIFIC_STAGING_PRODUCTION_ORIGIN_MATCH",
                    "The staging origin must differ from the configured production origin.",
                )
        return None

    def public(self) -> dict[str, Any]:
        return {
            "product1_base_url": self.product1_base_url,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "database_url": self.database_url,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "glific_contract_version": self.glific_contract_version,
            "glific_api_validation_mode": self.glific_api_validation_mode,
            "glific_staging_base_url_configured": bool(self.glific_staging_base_url),
            "glific_production_base_url_configured": bool(self.glific_production_base_url),
            "glific_staging_organization_configured": bool(self.glific_staging_organization_name),
            "glific_staging_confirmation_valid": bool(
                self.glific_staging_confirmation
                and self.glific_staging_confirmation.get_secret_value()
                == "I_CONFIRM_DISPOSABLE_GLIFIC_STAGING"
            ),
            "glific_staging_allow_import": self.glific_staging_allow_import,
            "glific_staging_allow_publish": self.glific_staging_allow_publish,
        }


settings = Settings()
settings.validate_mode()
