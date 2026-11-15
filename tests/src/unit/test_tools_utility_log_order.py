"""``ha_get_logs`` ordering and system-log message capping, on real Core.

Newest-first (default) vs oldest-first for every time-ordered source, plus the
warning for ``order`` on the non time-ordered ``logger`` source. The logbook
windowing mirrors the traces newest-first fix (#1178). ``compact`` caps
oversized ``system`` message strings.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import LogTools

from ._journal_gatewayd import FakeGatewayd, ask, per_minute
from ._journal_gatewayd import gatewayd as gatewayd
from ._journal_gatewayd import in_app_client as in_app_client
from ._log_tool_calls import call_kwargs

# The per-message cap the tool's ``compact`` description promises.
SYSTEM_MESSAGE_CAP = 2000


async def get_logs(client: HomeAssistantClient, **overrides: Any) -> dict[str, Any]:
    """``ha_get_logs`` through the server's client, defaults for what is not given."""
    return await LogTools(client).get_logs(**call_kwargs(**overrides))


async def record_states(hass: HomeAssistant, states: dict[str, list[str]]) -> None:
    """Have Core's recorder write each entity's states, in order, into its logbook."""
    # The logbook registers a sidebar panel; the real frontend needs the
    # frontend build, which is not installed with Core.
    mock_integration(hass, MockModule("frontend"))
    assert await async_setup_component(hass, "logbook", {})
    for step in range(max(len(values) for values in states.values())):
        for entity_id, values in states.items():
            if step < len(values):
                hass.states.async_set(entity_id, values[step])
                await hass.async_block_till_done()
    await async_wait_recording_done(hass)


def logbook_states(result: dict[str, Any]) -> list[str]:
    """The ``state`` of each logbook row ``ha_get_logs`` returned, in order."""
    return [row["state"] for row in result["data"]["entries"]]


async def set_up_system_log(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "system_log", {})


def log_record(
    name: str, message: str = "m", level: int = logging.ERROR, **extra: Any
) -> None:
    """One record on logger ``name``, as an integration writes it."""
    logging.getLogger(name).log(level, "%s", message, **extra)


@pytest.fixture
def error_log_file(hass: HomeAssistant, tmp_path: Path) -> Path:
    """The file Core serves as its error log; asked for before ``core_client``,
    whose ``api`` setup only serves the log when Core knows the file."""
    path = tmp_path / "home-assistant.log"
    path.write_text(
        "\n".join(
            f"2026-05-27 10:00:0{i}.000 ERROR (MainThread) [a.b.c] l{i}"
            for i in range(4)
        )
        + "\n",
        encoding="utf-8",
    )
    hass.data[KEY_DATA_LOGGING] = str(path)
    return path


_FIVE = ["s0", "s1", "s2", "s3", "s4"]  # s0 oldest ... s4 newest
_ONE_LIGHT = {"light.x": ["off", *_FIVE]}


async def test_logbook_newest_is_default_and_reverses_window(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await record_states(hass, _ONE_LIGHT)
    result = await get_logs(core_client, source="logbook", limit=2)
    data = result["data"]
    assert data["order"] == "newest"
    assert logbook_states(result) == ["s4", "s3"]
    assert data["offset"] == 0
    assert data["total_entries"] == 5
    assert data["has_more"] is True


async def test_logbook_oldest_returns_chronological_window(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await record_states(hass, _ONE_LIGHT)
    result = await get_logs(core_client, source="logbook", limit=2, order="oldest")
    assert result["data"]["order"] == "oldest"
    assert logbook_states(result) == ["s0", "s1"]
    assert result["data"]["has_more"] is True


async def test_logbook_newest_offset_pages_into_older_entries(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await record_states(hass, _ONE_LIGHT)
    result = await get_logs(core_client, source="logbook", limit=2, offset=2)
    assert logbook_states(result) == ["s2", "s1"]
    assert result["data"]["has_more"] is True


async def test_logbook_oldest_pagination_hint_carries_order(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await record_states(hass, _ONE_LIGHT)
    result = await get_logs(core_client, source="logbook", limit=2, order="oldest")
    # Quoted: the hint is meant to be copied back as a call.
    assert "order='oldest'" in result["data"]["pagination_hint"]


async def test_logbook_newest_default_hint_omits_order(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await record_states(hass, _ONE_LIGHT)
    result = await get_logs(core_client, source="logbook", limit=2)
    assert "pagination_hint" in result["data"]
    assert "order=" not in result["data"]["pagination_hint"]


async def test_logbook_newest_offset_beyond_total_returns_empty(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await record_states(hass, _ONE_LIGHT)
    data = (await get_logs(core_client, source="logbook", limit=2, offset=10))["data"]
    assert data["entries"] == []
    assert data["has_more"] is False
    assert data["offset"] == 10


async def test_logbook_newest_limit_exceeds_total_returns_all_reversed(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await record_states(hass, _ONE_LIGHT)
    result = await get_logs(core_client, source="logbook", limit=100)
    assert logbook_states(result) == ["s4", "s3", "s2", "s1", "s0"]
    assert result["data"]["has_more"] is False


async def test_logbook_newest_last_page_sets_has_more_false(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await record_states(hass, _ONE_LIGHT)
    result = await get_logs(core_client, source="logbook", limit=2, offset=4)
    assert logbook_states(result) == ["s0"]
    assert result["data"]["has_more"] is False


async def test_logbook_newest_filters_by_search_then_orders(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # The lights change in turn, so s0, s2 and s4 belong to light.match.
    await record_states(
        hass,
        {"light.match": ["off", "s0", "s2", "s4"], "light.other": ["off", "s1", "s3"]},
    )
    result = await get_logs(core_client, source="logbook", limit=10, search="match")
    data = result["data"]
    assert logbook_states(result) == ["s4", "s2", "s0"]
    assert data["total_entries"] == 3
    assert data["filters_applied"]["search"] == "match"


async def test_logbook_invalid_end_time_raises_structured_validation_error(
    core_client: HomeAssistantClient,
) -> None:
    """`datetime.fromisoformat` runs before the fetch try-block; without
    the guard a raw ValueError escapes the ToolError contract."""
    with pytest.raises(ToolError) as exc_info:
        await get_logs(core_client, source="logbook", end_time="invalid")
    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "end_time" in payload["error"]["message"]


async def test_system_log_newest_sorts_timestamp_descending(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_system_log(hass)
    for name in ("order_probe.a", "order_probe.b", "order_probe.c"):
        log_record(name)
    result = await get_logs(core_client, source="system", search="order_probe")
    assert result["order"] == "newest"
    assert [e["name"] for e in result["entries"]] == [
        "order_probe.c",
        "order_probe.b",
        "order_probe.a",
    ]
    stamps = [e["timestamp"] for e in result["entries"]]
    assert stamps == sorted(stamps, reverse=True)


async def test_system_log_oldest_sorts_timestamp_ascending(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_system_log(hass)
    for name in ("order_probe.a", "order_probe.b", "order_probe.c"):
        log_record(name)
    result = await get_logs(
        core_client, source="system", search="order_probe", order="oldest"
    )
    assert result["order"] == "oldest"
    assert [e["name"] for e in result["entries"]] == [
        "order_probe.a",
        "order_probe.b",
        "order_probe.c",
    ]


async def test_system_log_level_and_search_keep_only_matching_entries(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_system_log(hass)
    log_record("filter_probe.kept", "disk full")
    log_record("filter_probe.other_text", "something else")
    log_record("filter_probe.other_level", "disk full", logging.WARNING)
    result = await get_logs(
        core_client, source="system", level="ERROR", search="disk full"
    )
    assert [e["name"] for e in result["entries"]] == ["filter_probe.kept"]
    assert result["total_entries"] == 1


async def test_error_log_newest_reverses_recent_records(
    error_log_file: object, core_client: HomeAssistantClient
) -> None:
    result = await get_logs(core_client, source="error_log", limit=2)
    assert result["order"] == "newest"
    assert [ln.split()[-1] for ln in result["log"].splitlines()] == ["l3", "l2"]
    assert result["returned_lines"] == 2
    assert result["returned_records"] == 2
    assert result["total_lines"] == 4
    assert result["total_records"] == 4


async def test_error_log_oldest_keeps_chronological_window(
    error_log_file: object, core_client: HomeAssistantClient
) -> None:
    result = await get_logs(core_client, source="error_log", limit=2, order="oldest")
    assert result["order"] == "oldest"
    assert [ln.split()[-1] for ln in result["log"].splitlines()] == ["l2", "l3"]


async def test_supervisor_newest_reverses(
    gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
) -> None:
    gatewayd.load(per_minute(4))  # m3 oldest ... m0 newest
    result = await ask(in_app_client, source="supervisor", slug="core_x", limit=2)
    assert result["order"] == "newest"
    assert result["log"] == "m0\nm1"
    assert result["slug"] == "core_x"


async def test_system_service_oldest_keeps_chronological(
    gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
) -> None:
    gatewayd.load(per_minute(4))
    result = await ask(
        in_app_client,
        source="system_service",
        slug="supervisor",
        limit=2,
        order="oldest",
    )
    assert result["order"] == "oldest"
    assert result["log"] == "m1\nm0"


async def test_logger_non_default_order_warns_and_is_ignored(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "logger", {})
    result = await get_logs(core_client, source="logger", order="oldest")
    assert result["loggers"]
    assert "order" not in result  # logger response has no order field
    assert any("order" in w for w in result["warnings"])


async def test_logger_default_order_emits_no_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "logger", {})
    result = await get_logs(core_client, source="logger")
    assert result["loggers"]
    assert "warnings" not in result


_LEAD = "Detected blocking call to write_bytes with args "
_LAST_FRAME = '  File "/config/custom_components/pyscript/eval.py", line 1915'


def _blocking_call_message() -> str:
    """A system_log blocking-call warning in Home Assistant's own field order.

    ``raise_for_blocking_call`` formats the call's args (here a large bytes
    repr) before the integration and a ``format_stack()`` of the whole stack,
    so the frames that identify the offender come last.
    """
    frames = "".join(
        f'  File "/usr/src/homeassistant/homeassistant/core.py", line {n}, '
        f"in _run\n    self._loop.run_forever()\n"
        for n in range(60)
    )
    return (
        f"{_LEAD}(PosixPath('/config/www/snap.jpg'), b'" + "\\xff" * 50_000 + "') "
        "inside the event loop by custom integration 'pyscript' at "
        "custom_components/pyscript/eval.py, line 1915: return func(*args)\n"
        "Traceback (most recent call last):\n"
        + frames
        + _LAST_FRAME
        + ", in call_func\n    return func(*args, **kwargs)\n"
    )


async def test_compact_keeps_both_ends_of_an_oversized_message(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # The lead names the call and the stack's innermost frames name the
    # offender; the bytes repr between them is what has to go.
    await set_up_system_log(hass)
    message = _blocking_call_message()
    for text in (message, "short"):
        log_record("homeassistant.util.loop", text)
    result = await get_logs(core_client, source="system", search="blocking call")
    capped, short = result["entries"][0]["message"]
    assert len(capped) <= SYSTEM_MESSAGE_CAP
    assert capped.startswith(_LEAD)
    assert _LAST_FRAME in capped
    cut = len(message) - len(capped.split("…[", 1)[0]) - len(capped.rsplit("]…", 1)[1])
    assert f"{cut:,} chars cut" in capped
    assert "limit=1" in capped
    assert short == "short"
    assert result["truncated_messages"] == 1


async def test_compact_false_returns_the_full_message(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_system_log(hass)
    message = _blocking_call_message()
    log_record("homeassistant.util.loop", message)
    result = await get_logs(
        core_client, source="system", search="blocking call", compact=False
    )
    assert result["entries"][0]["message"] == [message]
    assert "truncated_messages" not in result


async def test_compact_never_caps_the_traceback(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_system_log(hass)
    try:
        raise ValueError("x" * 3 * SYSTEM_MESSAGE_CAP)
    except ValueError as err:
        log_record("cap_probe", exc_info=err)
    result = await get_logs(core_client, source="system", search="cap_probe")
    traceback = result["entries"][0]["exception"]
    assert len(traceback) > SYSTEM_MESSAGE_CAP
    assert traceback.endswith("x" * 3 * SYSTEM_MESSAGE_CAP)
    assert "truncated_messages" not in result


async def test_search_matches_text_inside_the_cut(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # The filter reads the full message before the cap, so an entry whose
    # only match falls inside the cut is still returned, without the match.
    await set_up_system_log(hass)
    log_record("cut_probe", "lead " + "a" * 50_000 + "needle" + "a" * 50_000 + " tail")
    result = await get_logs(core_client, source="system", search="needle")
    assert result["returned_entries"] == 1
    assert "needle" not in result["entries"][0]["message"][0]
    assert result["truncated_messages"] == 1
