"""Calendar writes Home Assistant refuses reach the caller with Core's reason.

The event tools send Core's ``calendar/event/*`` WebSocket commands, whose
schema answers before the handler looks the calendar up, so a refused event
needs no writable calendar in the test instance.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ...utilities.assertions import MCPAssertions


@pytest.mark.calendar
async def test_an_empty_all_day_event_is_refused_with_cores_reason(mcp_client):
    """An all-day end date is exclusive: end == start is an empty event, and
    the error says what Core expects instead of a bare refusal."""
    day = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()

    await MCPAssertions(mcp_client).call_tool_failure(
        "ha_config_set_calendar_event",
        {
            "entity_id": f"calendar.e2e_refusal_{uuid.uuid4().hex[:8]}",
            "summary": "E2E empty all-day event",
            "start": day,
            "end": day,
        },
        expected_error="Expected minimum event duration",
    )
