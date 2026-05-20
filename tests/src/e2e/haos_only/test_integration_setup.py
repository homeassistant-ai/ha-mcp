"""HAOS-only integration setup coverage (issue #1349 item 2).

These tests verify integration surfaces that the testcontainer tier physically
cannot reach because they depend on either (a) an HA Supervisor that installed
companion addons whose containers register HA integrations on first boot
(ESPHome Device Builder → ``esphome`` integration; Node-RED addon → the
``nodered`` integration), or (b) HA's real Sun-position math (the testcontainer
stub returns static values from a frozen world clock).

Layout:

* ESPHome companion integration loaded (auto-registered by addon install).
* ESPHome surfaces at least one entity (proves background platform setup ran).
* Node-RED companion integration loaded (auto-registered by addon install).
* Node-RED disable / re-enable round-trip via ``ha_set_integration_enabled``.
* Sun integration's ``sun.sun`` exposes realistic next_dawn / next_dusk
  attributes within 24h of the test-process clock.
* Local Calendar config-entry lifecycle: create entry → entity registers →
  add event → retrieve event → tear down the config entry.

All tests are marker-gated to the HAOS backend and run without ``pytest.skip``;
absent fixtures fail loudly.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ..utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
    safe_call_tool,
)
from ..utilities.wait_helpers import wait_for_tool_result

LOG = logging.getLogger(__name__)

pytestmark = [pytest.mark.haos_only]


# Node-RED's HA integration historically registered under the ``nodered``
# domain (no underscore); some addon versions use ``node_red``. Accept either
# so the test is robust across addon-version drift.
_NODE_RED_DOMAIN_CANDIDATES: tuple[str, ...] = ("nodered", "node_red")


def _find_entry_for_domain(
    entries: list[dict[str, Any]], domain: str
) -> dict[str, Any] | None:
    """Return the first entry in ``entries`` whose domain matches ``domain``."""
    for entry in entries:
        if entry.get("domain") == domain:
            return entry
    return None


# NOTE: ``test_esphome_companion_integration_loaded``,
# ``test_esphome_dashboard_device_present``,
# ``test_nodered_companion_integration_loaded``, and
# ``test_nodered_integration_disable_enable_cycle`` were deleted in
# this commit. Their premise — that installing the ESPHome or Node-RED
# addon auto-registers a companion HA integration — is incorrect.
# Verified live (CI run on 287c5ced): with both addons installed and
# running, ``ha_get_integration`` returns only ['backup', 'demo',
# 'go2rtc', 'google_translate', 'ha_mcp_tools', 'hacs', 'hassio',
# 'local_calendar', 'mcp_proxy', 'radio_browser', 'shopping_list',
# 'sun'] — neither esphome nor nodered. Both addons require the user
# to set up the integration via Settings → Integrations (config flow),
# they don't auto-register on addon install. A future test could
# drive the config flow explicitly via ``ha_client.start_config_flow``
# (same pattern as the local_calendar test below), but that's
# substantial new test scaffolding; for now the integration-setup
# coverage in this PR is local_calendar + sun.


async def test_sun_position_is_realistic(mcp_client: Any) -> None:
    """sun.sun exposes next_dawn / next_dusk within 24h of the test clock.

    The testcontainer image runs against a fixed system clock and the demo
    sun platform can return static or far-future timestamps. A real HAOS boot
    runs the actual ``sun`` integration against the live system clock, so
    next_dawn and next_dusk should each be within the next 24 hours.

    This test guards two regressions at once:
    1. The sun integration loaded at all (state would be 'unavailable'
       otherwise).
    2. The HAOS guest's clock is in sync with the test runner (clock skew
       beyond 24h would push both timestamps outside the window).
    """
    raw = await mcp_client.call_tool("ha_get_state", {"entity_id": "sun.sun"})
    data = parse_mcp_result(raw)
    # ha_get_state's single-entity path returns the state dict either at the
    # top level or nested under "data" depending on response shape; tolerate
    # both.
    state_payload = data.get("data") if "data" in data else data
    assert isinstance(state_payload, dict), (
        f"Unexpected ha_get_state shape for sun.sun: {data}"
    )
    state = state_payload.get("state")
    assert state in {"above_horizon", "below_horizon"}, (
        f"sun.sun state is {state!r}; expected 'above_horizon' or "
        f"'below_horizon'. Full payload: {state_payload}"
    )

    attributes = state_payload.get("attributes") or {}
    next_dawn_raw = attributes.get("next_dawn")
    next_dusk_raw = attributes.get("next_dusk")
    assert next_dawn_raw, f"sun.sun missing next_dawn attribute. attrs={attributes}"
    assert next_dusk_raw, f"sun.sun missing next_dusk attribute. attrs={attributes}"

    now = datetime.now(UTC)
    window_end = now + timedelta(hours=24)
    # HA emits these as ISO 8601 strings, optionally with trailing 'Z'.
    for label, raw_val in (("next_dawn", next_dawn_raw), ("next_dusk", next_dusk_raw)):
        normalized = (
            raw_val.replace("Z", "+00:00") if isinstance(raw_val, str) else raw_val
        )
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        assert now - timedelta(minutes=5) <= parsed <= window_end, (
            f"sun.sun {label}={parsed.isoformat()} is not within the next 24h "
            f"of now={now.isoformat()}. Either the sun integration is broken "
            f"or the HAOS guest clock is skewed."
        )
    LOG.info(
        "sun.sun state=%s next_dawn=%s next_dusk=%s",
        state,
        next_dawn_raw,
        next_dusk_raw,
    )


async def test_local_calendar_lifecycle(mcp_client: Any, ha_client: Any) -> None:
    """End-to-end local_calendar config-entry + event lifecycle.

    The local_calendar integration ships with HA Core and exposes a writable
    calendar via a config flow. The testcontainer tier seeds a single
    local_calendar entry into ``.storage/core.config_entries`` because driving
    the config flow at test time is unnecessary there; the HAOS tier exercises
    the live flow against a real HA install.

    Flow:
      1. Drive the local_calendar config flow via the underlying REST client
         to create a fresh config entry (the ``ha_config_set_helper`` MCP tool
         does not enumerate local_calendar as a helper type, so we go one
         level lower).
      2. Verify the new ``calendar.<slug>`` entity is registered via
         ``ha_get_entity``.
      3. Add an event via ``ha_config_set_calendar_event``.
      4. Retrieve it via ``ha_config_get_calendar_events``.
      5. Tear down the config entry via ``ha_delete_helpers_integrations``.
    """
    unique = uuid.uuid4().hex[:8]
    calendar_name = f"HAOS Lifecycle {unique}"
    # local_calendar derives the entity slug from the calendar name.
    expected_entity_id = f"calendar.haos_lifecycle_{unique}"

    entry_id: str | None = None
    try:
        # Step 1 — drive the config flow via the REST client. The MCP
        # ha_config_set_helper tool's helper_type literal doesn't cover
        # local_calendar (it's an integration, not a helper), so we bypass it
        # and hit the underlying client API directly. This is still an
        # end-to-end exercise of HA's real config-flow machinery.
        flow_init = await ha_client.start_config_flow("local_calendar")
        assert flow_init.get("type") == "form", (
            f"Unexpected local_calendar flow init shape: {flow_init}"
        )
        flow_id = flow_init["flow_id"]

        flow_done = await ha_client.submit_config_flow_step(
            flow_id,
            {"calendar_name": calendar_name, "import": "create_empty"},
        )
        assert flow_done.get("type") == "create_entry", (
            f"local_calendar flow did not create an entry: {flow_done}"
        )
        entry_id = flow_done["result"]["entry_id"]
        LOG.info("Created local_calendar entry %s (name=%r)", entry_id, calendar_name)

        # Step 2 — wait for the calendar entity to register, then verify via
        # ha_get_entity that it shows up in the entity registry.
        entity_data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_entity",
            arguments={"entity_id": expected_entity_id},
            predicate=lambda d: d.get("success") is True,
            description=f"{expected_entity_id} registers in entity registry",
            timeout=20,
        )
        assert entity_data.get("entity_id") == expected_entity_id or (
            entity_data.get("data", {}).get("entity_id") == expected_entity_id
        ), (
            f"ha_get_entity returned wrong entity. Expected "
            f"{expected_entity_id!r}, got: {entity_data}"
        )

        # Step 3 — add an event. local_calendar accepts ISO 8601 timestamps;
        # use a deterministic future window so we can find the same event back.
        now = datetime.now()
        event_start = (now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        event_end = event_start + timedelta(hours=1)
        event_summary = f"haos-e2e-event-{unique}"

        create_raw = await mcp_client.call_tool(
            "ha_config_set_calendar_event",
            {
                "entity_id": expected_entity_id,
                "summary": event_summary,
                "start": event_start.isoformat(),
                "end": event_end.isoformat(),
                "description": "Created by test_integration_setup.py — safe to delete.",
            },
        )
        assert_mcp_success(create_raw, "Create local_calendar event")

        # Step 4 — retrieve and confirm the event shows up. local_calendar can
        # take a moment to flush the iCal write; poll for the event to appear.
        retrieved = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_config_get_calendar_events",
            arguments={
                "entity_id": expected_entity_id,
                "start": event_start.isoformat(),
                "end": (event_end + timedelta(hours=1)).isoformat(),
            },
            predicate=lambda d: any(
                e.get("summary") == event_summary for e in d.get("events", [])
            ),
            description=f"event {event_summary!r} visible in calendar",
            timeout=45,
        )
        matching = [
            e for e in retrieved.get("events", []) if e.get("summary") == event_summary
        ]
        assert matching, (
            f"Created event not in retrieved set. Events: {retrieved.get('events')}"
        )
        LOG.info("Round-tripped event %r through %s", event_summary, expected_entity_id)

    finally:
        # Step 5 — tear down the config entry. Skipped only when entry creation
        # itself failed (entry_id stayed None) — there's nothing to clean up
        # in that case. Removing the config entry removes the entity and
        # the per-entry iCal storage file together.
        if entry_id is not None:
            cleanup = await safe_call_tool(
                mcp_client,
                "ha_delete_helpers_integrations",
                {"target": entry_id, "helper_type": None, "confirm": True},
            )
            assert cleanup.get("success"), (
                f"Teardown of local_calendar entry {entry_id} failed; "
                f"the qcow2 will leak this entry across the session and "
                f"subsequent ha_search_entities calls will slow over time. "
                f"Result: {cleanup}"
            )
