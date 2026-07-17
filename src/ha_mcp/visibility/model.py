"""Config model for the opt-in entity visibility filter (issue #1728)."""

from __future__ import annotations

from typing import Any

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

    def to_wire(self) -> dict[str, Any]:
        """Serialize the hide dimensions for the component ``search`` fast path.

        Emits exactly the fields the ha_mcp_tools component's ``search``
        ``visibility`` param consumes (``_visibility_hidden_set``) — the nine hide
        dimensions, one-to-one with what ``config_has_active_hide_dimensions`` and
        ``hidden_entity_ids`` read. ``version``/``enabled`` are omitted: the
        component applies the dimensions unconditionally, and the server only ever
        sends this dict when the filter is active, so an ``enabled`` gate would be
        redundant. Kept in lockstep with ``_visibility_hidden_set``; a new
        dimension must be added on both sides (a new component capability), not
        silently dropped here.
        """
        return {
            "exclude_categories": list(self.exclude_categories),
            "exclude_hidden": self.exclude_hidden,
            "deny_entity_ids": list(self.deny_entity_ids),
            "exclude_areas": list(self.exclude_areas),
            "exclude_labels": list(self.exclude_labels),
            "allow_entity_ids": list(self.allow_entity_ids),
            "allow_areas": list(self.allow_areas),
            "allow_labels": list(self.allow_labels),
            "respect_assist_exposure": self.respect_assist_exposure,
        }
