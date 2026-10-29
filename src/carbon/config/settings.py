"""Carbon application settings and runtime configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for Carbon modular monolith."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="CARBON_",
        extra="ignore",
    )

    # Local loopback API configuration (Enforces Rule 6: Never expose publicly)
    api_host: str = Field(default="127.0.0.1", description="Loopback host binding")
    api_port: int = Field(default=8765, description="Port for local API")
    api_token: str | None = Field(default=None, description="Optional bearer token for IPC")

    # Local AI provider configuration
    ollama_base_url: str = Field(
        default="http://127.0.0.1:11434", description="Ollama API endpoint"
    )
    primary_model: str = Field(default="qwen3.5:9b", description="Primary approved LLM identifier")
    inference_timeout_seconds: float = Field(
        default=300.0, description="Inference timeout threshold"
    )

    # Persistence & storage
    data_dir: Path = Field(default=Path("data"), description="Root directory for local data")
    database_path: Path = Field(default=Path("data/carbon.db"), description="SQLite database path")

    # Governance & Autonomy defaults
    autonomy_level: str = Field(default="A1", description="Default autonomy level: A0, A1, A2, A3")
    max_auto_permission_level: str = Field(
        default="P1", description="Maximum permission tier for automated execution: P0, P1, P2"
    )

    # Diagnostics & logging
    log_level: str = Field(default="INFO", description="Console logging severity")
    enable_audit_log: bool = Field(default=True, description="Enable structured audit logging")
    audit_log_path: Path = Field(
        default=Path("data/audit.log"), description="Path for append-only audit log"
    )

    @field_validator("api_host")
    @classmethod
    def validate_loopback_only(cls, v: str) -> str:
        """Enforce that Carbon only ever binds to loopback."""
        allowed_loopbacks = {"127.0.0.1", "localhost", "::1"}
        if v.strip().lower() not in allowed_loopbacks:
            raise ValueError(
                f"Security violation: Carbon API host must be loopback ({allowed_loopbacks}). "
                f"Attempted to bind to '{v}', violating Non-Negotiable Rule 6."
            )
        return v

    def ensure_directories(self) -> None:
        """Ensure necessary runtime directories exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    settings = Settings()
    settings.ensure_directories()
    return settings
