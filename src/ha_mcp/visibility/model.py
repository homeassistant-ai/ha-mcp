"""Config model for the opt-in entity visibility filter (issue #1728)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class VisibilityConfig(BaseModel):
    """Per-install visibility filter config. Default is disabled (no-op)."""

    version: int = 1
    enabled: bool = False
    exclude_categories: list[str] = Field(default_factory=lambda: ["diagnostic", "config"])
    exclude_hidden: bool = False
    deny_entity_ids: list[str] = Field(default_factory=list)
    exclude_areas: list[str] = Field(default_factory=list)
    exclude_labels: list[str] = Field(default_factory=list)
