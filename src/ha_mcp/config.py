"""
Configuration management for Home Assistant MCP Server.
"""

import os

# Load environment variables from .env file with HAMCP_ENV_FILE support
# Use absolute path to ensure .env is found regardless of cwd
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ha_mcp._version import get_version

_PACKAGE_VERSION = get_version()

project_root = Path(__file__).parent.parent.parent

# Demo environment token - use HOMEASSISTANT_TOKEN="demo" to connect to the public demo
# Demo server: https://ha-mcp-demo-server.qc-h.net (login: mcp/mcp, resets weekly)
DEMO_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxOTE5ZTZlMTVkYjI0Mzk2YTQ4YjFiZTI1MDM1YmU2YSIsImlhdCI6MTc1NzI4OTc5NiwiZXhwIjoyMDcyNjQ5Nzk2fQ.Yp9SSAjm2gvl9Xcu96FFxS8SapHxWAVzaI0E3cD9xac"

# OAuth mode sentinel values — when these are present, HA credentials come from OAuth tokens
OAUTH_MODE_URL = "http://oauth-mode"
OAUTH_MODE_TOKEN = "oauth-mode-token"

# Support for different environment files via HAMCP_ENV_FILE
env_file = os.getenv("HAMCP_ENV_FILE", ".env")
env_path = project_root / env_file

# Load the specified environment file (silently, since env vars may come from other sources)
if env_path.exists():
    load_dotenv(env_path)
else:
    # Fallback to default .env
    default_env_path = project_root / ".env"
    if default_env_path.exists():
        load_dotenv(default_env_path)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Home Assistant connection
    # In OAuth mode, these are optional and provided per-request
    homeassistant_url: str = Field(default=OAUTH_MODE_URL, alias="HOMEASSISTANT_URL")
    homeassistant_token: str = Field(
        default=OAUTH_MODE_TOKEN, alias="HOMEASSISTANT_TOKEN"
    )

    # Server configuration
    timeout: int = Field(30, alias="HA_TIMEOUT")
    max_retries: int = Field(3, alias="HA_MAX_RETRIES")

    # Tool configuration
    fuzzy_threshold: int = Field(60, alias="FUZZY_THRESHOLD")
    entity_search_limit: int = Field(20, alias="ENTITY_SEARCH_LIMIT")

    # Backup tool configuration
    backup_hint: str = Field("normal", alias="BACKUP_HINT")

    # WebSocket configuration (essential for async operations)
    enable_websocket: bool = Field(True, alias="ENABLE_WEBSOCKET")

    # Development/Debug configuration
    debug: bool = Field(False, alias="DEBUG")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # MCP Server configuration
    mcp_server_name: str = Field("ha-mcp", alias="MCP_SERVER_NAME")
    mcp_server_version: str = Field(
        default=_PACKAGE_VERSION, alias="MCP_SERVER_VERSION"
    )

    # Environment configuration
    environment: str = Field("development", alias="ENVIRONMENT")

    # Tool filtering - comma-separated list of module names to enable
    # Special values: "all" (default), "automation" (automation-related tools only)
    # Examples: "tools_config_automations,tools_config_scripts,tools_traces"
    enabled_tool_modules: str = Field("all", alias="ENABLED_TOOL_MODULES")

    # Dashboard partial update tools (python_transform, find_card)
    # These are token-efficient alternatives to full config replacement.
    # Disable when using clients with programmatic tool use (future).
    enable_dashboard_partial_tools: bool = Field(
        True, alias="ENABLE_DASHBOARD_PARTIAL_TOOLS"
    )

    # Skills configuration
    # Serve bundled HA best-practice skills as MCP resources (skill:// URIs).
    # Resources are not auto-injected — clients must explicitly request them.
    enable_skills: bool = Field(True, alias="ENABLE_SKILLS")

    # Expose skills and doc resources as tools (list_resources/read_resource)
    # for clients that don't support MCP resources natively.
    # Defaults to True so all clients can access documentation and skills.
    # Resource-capable clients can set to False to reduce tool count.
    enable_skills_as_tools: bool = Field(True, alias="ENABLE_SKILLS_AS_TOOLS")

    # Tool search transform — replaces the full tool catalog with a unified
    # BM25 search tool and categorized call proxies (read/write/delete).
    # Dramatically reduces idle context token usage for LLMs.
    enable_tool_search: bool = Field(False, alias="ENABLE_TOOL_SEARCH")

    # Managed YAML config editing — allows ha_config_set_yaml to add,
    # replace, or remove top-level keys in configuration.yaml and package
    # files. Disabled by default; only for YAML-only features with no UI/API path.
    enable_yaml_config_editing: bool = Field(False, alias="ENABLE_YAML_CONFIG_EDITING")

    # Seed values for tool visibility (comma-separated tool names).
    # Used as initial config when no tool_config.json exists.
    # The web settings UI (/settings) is the primary interface for managing these.
    disabled_tools: str = Field("", alias="DISABLED_TOOLS")
    pinned_tools: str = Field("", alias="PINNED_TOOLS")

    # Max results returned by ha_search_tools. Pydantic enforces the
    # 2-10 range; the addon-dev schema also uses ``int(2,10)?`` so the
    # supervisor UI rejects out-of-range values before they reach env vars.
    tool_search_max_results: int = Field(5, ge=2, le=10, alias="TOOL_SEARCH_MAX_RESULTS")

    @model_validator(mode="after")
    def _skills_dependency(self) -> "Settings":
        """Auto-enable skills (resources) when skills-as-tools is on.

        skills_as_tools wraps ResourcesAsTools which requires skills to be
        registered as MCP resources first. Without this, enabling
        skills_as_tools alone would produce empty list_resources results.
        """
        if self.enable_skills_as_tools and not self.enable_skills:
            self.enable_skills = True
        return self

    @property
    def env_file_name(self) -> str:
        """Get the current environment file name."""
        return os.getenv("HAMCP_ENV_FILE", ".env")

    @field_validator("homeassistant_url")
    @classmethod
    def validate_homeassistant_url(cls, v: str) -> str:
        """Ensure URL is properly formatted."""
        # Allow OAuth mode placeholder
        if v == OAUTH_MODE_URL:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError("Home Assistant URL must start with http:// or https://")
        return v.rstrip("/")  # Remove trailing slash

    @field_validator("homeassistant_token")
    @classmethod
    def validate_homeassistant_token(cls, v: str) -> str:
        """Ensure token is not empty. Use 'demo' for public demo environment."""
        # Allow OAuth mode placeholder
        if v == OAUTH_MODE_TOKEN:
            return v
        if not v or v == "your_long_lived_access_token_here":
            raise ValueError("Home Assistant token must be provided")
        # Replace "demo" with actual demo token for easy onboarding
        if v.lower() == "demo":
            return DEMO_TOKEN
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

    @field_validator("backup_hint")
    @classmethod
    def validate_backup_hint(cls, v: str) -> str:
        """Ensure backup hint is valid."""
        valid_hints = ["strong", "normal", "weak", "auto"]
        if v.lower() not in valid_hints:
            raise ValueError(f"Backup hint must be one of {valid_hints}")
        return v.lower()

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
