"""
Configuration management for Home Assistant MCP Server.
"""

import os

# Load environment variables from .env file with HAMCP_ENV_FILE support
# Use absolute path to ensure .env is found regardless of cwd
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

project_root = Path(__file__).parent.parent.parent

# Support for different environment files via HAMCP_ENV_FILE
env_file = os.getenv("HAMCP_ENV_FILE", ".env")
env_path = project_root / env_file

# Load the specified environment file
if env_path.exists():
    load_dotenv(env_path)
    print(f"[ENV] Loaded environment from: {env_path}")
else:
    # Fallback to default .env
    default_env_path = project_root / ".env"
    if default_env_path.exists():
        load_dotenv(default_env_path)
        print(f"[ENV] Fallback: Loaded environment from: {default_env_path}")
    else:
        print(
            f"[ENV] WARNING: No environment file found. Tried: {env_path}, {default_env_path}"
        )


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Home Assistant connection
    homeassistant_url: str = Field(..., alias="HOMEASSISTANT_URL")
    homeassistant_token: str = Field(..., alias="HOMEASSISTANT_TOKEN")

    # Server configuration
    timeout: int = Field(30, alias="HA_TIMEOUT")
    max_retries: int = Field(3, alias="HA_MAX_RETRIES")

    # Tool configuration
    fuzzy_threshold: int = Field(60, alias="FUZZY_THRESHOLD")
    entity_search_limit: int = Field(20, alias="ENTITY_SEARCH_LIMIT")

    # WebSocket configuration (essential for async operations)
    enable_websocket: bool = Field(True, alias="ENABLE_WEBSOCKET")

    # Development/Debug configuration
    debug: bool = Field(False, alias="DEBUG")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # MCP Server configuration
    mcp_server_name: str = Field("ha-mcp", alias="MCP_SERVER_NAME")
    mcp_server_version: str = Field("0.1.0", alias="MCP_SERVER_VERSION")

    # Environment configuration
    environment: str = Field("development", alias="ENVIRONMENT")

    @property
    def env_file_name(self) -> str:
        """Get the current environment file name."""
        return os.getenv("HAMCP_ENV_FILE", ".env")

    @field_validator("homeassistant_url")
    @classmethod
    def validate_homeassistant_url(cls, v: str) -> str:
        """Ensure URL is properly formatted."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("Home Assistant URL must start with http:// or https://")
        return v.rstrip("/")  # Remove trailing slash

    @field_validator("homeassistant_token")
    @classmethod
    def validate_homeassistant_token(cls, v: str) -> str:
        """Ensure token is not empty."""
        if not v or v == "your_long_lived_access_token_here":
            raise ValueError("Home Assistant token must be provided")
        return v

    @field_validator("fuzzy_threshold")
    @classmethod
    def validate_fuzzy_threshold(cls, v: int) -> int:
        """Ensure fuzzy threshold is reasonable."""
        if not 0 <= v <= 100:
            raise ValueError("Fuzzy threshold must be between 0 and 100")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Ensure log level is valid."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Log level must be one of {valid_levels}")
        return v.upper()

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="allow"
    )


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()  # type: ignore[call-arg]


def validate_settings() -> tuple[bool, str | None]:
    """
    Validate settings and return (is_valid, error_message).

    Returns:
        tuple: (True, None) if valid, (False, error_message) if invalid
    """
    try:
        settings = get_settings()

        # Additional validation
        if not settings.homeassistant_url:
            return False, "Home Assistant URL is required"

        if not settings.homeassistant_token:
            return False, "Home Assistant token is required"

        return True, None
    except Exception as e:
        return False, str(e)


# Global settings instance
_settings: Settings | None = None


def get_global_settings() -> Settings:
    """Get global settings instance (singleton pattern)."""
    global _settings
    if _settings is None:
        _settings = get_settings()
    return _settings
