"""Config model for the opt-in entity visibility filter (issue #1728)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VisibilityConfig(BaseModel):
    """Per-install visibility filter config. Default is disabled (no-op)."""

    # Unknown keys are dropped rather than rejected: forward-compatibility with a
    # newer add-on / hand-edited file that carries a not-yet-known field must not
    # fail the whole config load (which would fail-open-disable the filter). The
    # trade-off is that a typo'd key (e.g. "exclude_area") is silently ignored;
    # exclude_categories values are separately validated with a surfaced warning,
    # and this is the pydantic default made explicit so the choice is documented.
    model_config = ConfigDict(extra="ignore")

    version: int = 1
    enabled: bool = False
    exclude_categories: list[str] = Field(
        default_factory=lambda: ["diagnostic", "config"]
    )
    exclude_hidden: bool = False
    deny_entity_ids: list[str] = Field(default_factory=list)
    exclude_areas: list[str] = Field(default_factory=list)
    exclude_labels: list[str] = Field(default_factory=list)
    # Allowlist (opt-in restrict mode): when any allow_* is non-empty the filter
    # inverts to "hide everything not matched here". Empty => allowlist inactive.
    allow_entity_ids: list[str] = Field(default_factory=list)
    allow_areas: list[str] = Field(default_factory=list)
    allow_labels: list[str] = Field(default_factory=list)
    # Respect HA Assist exposure: when true, hide entities not effectively exposed
    # to the "conversation" assistant (explicit override, else domain default).
    respect_assist_exposure: bool = False
