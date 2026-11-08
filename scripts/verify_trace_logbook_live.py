"""Live check for the trace logbook path.

Creates a temporary automation, triggers it, and asserts that
``ha_get_automation_traces(detailed=True)`` returns at least one logbook row.
This is the live assertion the testcontainer lane cannot make: its recorder
does not commit new rows from a freshly created automation.

Run against a disposable HA:

    HOMEASSISTANT_URL=http://... HOMEASSISTANT_TOKEN=... \
        uv run python scripts/verify_trace_logbook_live.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_traces import TraceTools


async def main() -> None:
    url = os.environ["HOMEASSISTANT_URL"]
    token = os.environ["HOMEASSISTANT_TOKEN"]
    verify_ssl = os.environ.get("HA_VERIFY_SSL", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    client = HomeAssistantClient(base_url=url, token=token, verify_ssl=verify_ssl)
    tools = TraceTools(client)
    suffix = uuid.uuid4().hex[:8]
    unique_id = f"trace_logbook_live_{suffix}"
    entity_id = None
    try:
        states = await client.get_states()
        light = next(
            state["entity_id"]
            for state in states
            if str(state.get("entity_id", "")).startswith("light.")
        )
        config = {
            "id": unique_id,
            "alias": f"Trace logbook live {suffix}",
            "trigger": [{"platform": "event", "event_type": unique_id}],
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": light},
                },
                {"delay": {"seconds": 0}},
            ],
            "mode": "single",
            "trace": {"stored_traces": 5},
        }
        created = await client.upsert_automation_config(
            config, identifier=unique_id, wait_for_entity=True
        )
        entity_id = created.get("entity_id")
        assert entity_id, created

        await client.call_service("automation", "trigger", {"entity_id": entity_id})
        # The recorder commits logbook rows asynchronously.
        await asyncio.sleep(5)

        listed = await tools.ha_get_automation_traces(entity_id=entity_id)
        assert listed["trace_count"] > 0, listed
        run_id = listed["traces"][0]["run_id"]
        detail = await tools.ha_get_automation_traces(
            entity_id=entity_id, run_id=run_id, detailed=True
        )
        assert detail["logbook_transport"] in {"component", "rest"}, detail
        assert detail["logbook_total"] > 0, detail
        assert detail["logbook_entries"], detail
        print(
            "trace logbook ok:",
            detail["logbook_transport"],
            detail["logbook_total"],
            detail["logbook_entries"][0].get("message"),
        )
    finally:
        if entity_id:
            try:
                await client.delete_automation_config(unique_id)
            except Exception as err:  # noqa: BLE001
                print("automation cleanup failed:", err)
        await client.httpx_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
