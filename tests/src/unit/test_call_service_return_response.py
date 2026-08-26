"""Regression tests for ha_call_service's return_response placement (issue #2085).

Home Assistant's REST reply to ``return_response=true`` is an envelope:
``{"changed_states": [...], "service_response": {...}}``. The legacy REST path in
``ha_call_service`` used to hand that whole envelope to ``_project_service_result``
(which returns dicts unchanged) *and* copy ``service_response`` to the top level,
so the service's response shipped twice, byte-identical, doubling its token cost.

The envelope is now split before projection: ``result`` carries only the changed
states and ``service_response`` appears exactly once at the top level — the same
placement ``_build_component_call_response`` already used for the component path.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.tools_service import _NON_ENVELOPE_WARNING, ServiceTools

_SHELL_RESPONSE = {"stdout": "abc", "stderr": "", "returncode": 0}

_CHANGED_STATE = {
    "entity_id": "light.x",
    "state": "on",
    "attributes": {},
    "context": {"id": "ctx-1"},
    "last_changed": "t",
    "last_updated": "t",
    "last_reported": "t",
}


def _make_tools(call_service_return: Any) -> ServiceTools:
    client = MagicMock()
    client.call_service = AsyncMock(return_value=call_service_return)
    return ServiceTools(client, MagicMock())


def _occurrences(response: dict[str, Any], needle: str) -> int:
    """Count *needle* in the response payload, ignoring ``warnings`` prose.

    These counts exist to catch a payload shipped twice (issue #2085). Warning
    text is human-facing prose that legitimately names the envelope keys, so
    counting it would confuse "the records duplicated" with "we explained why
    the result is empty".
    """
    payload = {k: v for k, v in response.items() if k != "warnings"}
    return json.dumps(payload).count(needle)


@pytest.fixture(autouse=True)
def _no_component():
    """Belt-and-braces pin to the legacy REST path (component advertises nothing).

    These tests already take that path without the patch: none calls a service in
    ``_STATE_CHANGING_SERVICES`` (they all use ``shell_command.test``), so
    ``should_wait`` is False and ``_maybe_component_call_service`` early-returns
    before consulting the component at all. The service name is what is
    load-bearing here, NOT the absence of an ``entity_id`` —
    ``test_entity_id_filters_the_changed_states`` passes one. The patch keeps the
    routing decision from silently changing if that early-out ever moves —
    ``component_supports(None, ...)`` is False, so ``_call_service_via_component``
    returns None and ``ha_call_service`` falls through to the REST POST.
    """
    with patch(
        "ha_mcp.tools.tools_service.get_component_caps",
        new=AsyncMock(return_value=None),
    ):
        yield


class TestReturnResponsePlacement:
    async def test_service_response_appears_once_at_top_level(self):
        """The envelope splits: response data top-level, changed states in result."""
        tools = _make_tools(
            {"changed_states": [_CHANGED_STATE], "service_response": _SHELL_RESPONSE}
        )

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert response["service_response"] == _SHELL_RESPONSE
        result = response["result"]
        assert isinstance(result, list), (
            f"result must be the projected changed_states list, got: {result!r}"
        )
        assert len(result) == 1
        assert result[0]["entity_id"] == "light.x"
        assert "service_response" not in result[0]
        # The changed states now go through default compaction like every other
        # path — pre-fix they bypassed it entirely as part of the raw envelope.
        assert "context" not in result[0]
        assert "last_changed" not in result[0]
        assert _occurrences(response, "stdout") == 1, (
            f"service_response must be serialized exactly once: {response!r}"
        )

    async def test_verbose_still_returns_one_copy(self):
        """verbose=True bypasses projection but must not resurrect the duplicate."""
        tools = _make_tools(
            {"changed_states": [_CHANGED_STATE], "service_response": _SHELL_RESPONSE}
        )

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True, verbose=True
        )

        assert response["service_response"] == _SHELL_RESPONSE
        # verbose returns the changed states raw (metadata kept), never the envelope.
        assert response["result"] == [_CHANGED_STATE]
        assert _occurrences(response, "stdout") == 1, (
            f"service_response must be serialized exactly once: {response!r}"
        )

    async def test_envelope_without_service_response_key_surfaces_whole_dict(self):
        """No ``service_response`` key: the whole dict goes top-level, once.

        Carries a NON-empty ``changed_states`` on purpose: echoing the dict whole
        *and* projecting its states into ``result`` would ship those records twice
        — #2085 all over again, inside the branch meant to be the safe fallback.
        An empty list cannot tell the two behaviors apart.
        """
        tools = _make_tools({"changed_states": [_CHANGED_STATE]})

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert response["service_response"] == {"changed_states": [_CHANGED_STATE]}
        assert response["result"] == []
        assert _occurrences(response, "light.x") == 1, (
            f"the fallback dict must not double-ship its changed states: {response!r}"
        )
        assert _occurrences(response, "changed_states") == 1, (
            f"the fallback dict must be serialized exactly once: {response!r}"
        )
        # An empty result here is an artifact of the unrecognized shape, not a
        # claim that nothing changed — say so rather than letting it read that way.
        assert _NON_ENVELOPE_WARNING in response["warnings"], (
            f"an unrecognized envelope must warn about the empty result: {response!r}"
        )

    async def test_missing_changed_states_warns(self):
        """A reply with no ``changed_states`` warns instead of implying no changes.

        The whole reply is handed back, not just the recognised key — every
        non-conforming shape takes the same path, so the warning's "the whole
        reply is reported under 'service_response'" is true without exception.
        """
        reply = {"service_response": _SHELL_RESPONSE}
        tools = _make_tools(reply)

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert response["service_response"] == reply
        assert response["result"] == []
        assert _NON_ENVELOPE_WARNING in response["warnings"], (
            f"a missing changed_states must warn about the empty result: {response!r}"
        )

    async def test_non_dict_reply_still_surfaces_the_requested_key(self):
        """A non-envelope reply must not drop BOTH the key and the explanation.

        Returning neither is the masking this PR exists to remove: the caller
        asked for response data and would learn nothing about where it went.

        Direct contract test — this shape does NOT arrive through the real
        client. ``HomeAssistantClient.call_service`` normalises first: with
        ``return_response=True`` a non-dict reply is wrapped as
        ``{"service_response": <reply>}`` (``rest_client.py``), which reaches the
        helper as a dict missing ``changed_states`` and lands in the same
        fallback — the reachable shape, pinned by
        ``test_missing_changed_states_warns``. Kept because the helper is a
        static function with its own contract, not because the wiring can
        deliver this.
        """
        tools = _make_tools([_CHANGED_STATE])

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert response["service_response"] == [_CHANGED_STATE]
        assert response["result"] == []
        assert _NON_ENVELOPE_WARNING in response["warnings"], (
            f"a non-dict reply must warn rather than silently drop: {response!r}"
        )

    async def test_non_list_changed_states_warns_and_keeps_the_whole_reply(self):
        """A non-list ``changed_states`` would sail through projection untouched.

        ``compact_service_result`` returns non-lists unchanged, so the raw value
        would be emitted as ``result`` with ``result_fields`` silently ignored.
        The whole reply — malformed ``changed_states`` included — must survive
        under ``service_response``: peeling only the recognised key out would
        discard it AND make the warning's "the whole reply is reported under
        'service_response'" false.
        """
        reply = {
            "changed_states": {"unexpected": "shape"},
            "service_response": _SHELL_RESPONSE,
        }
        tools = _make_tools(reply)

        response = await tools.ha_call_service(
            domain="shell_command",
            service="test",
            return_response=True,
            result_fields=["entity_id"],
        )

        assert response["service_response"] == reply, (
            f"the malformed changed_states must not be discarded: {response!r}"
        )
        assert response["result"] == []
        assert _NON_ENVELOPE_WARNING in response["warnings"], (
            f"a non-list changed_states must warn: {response!r}"
        )

    async def test_conforming_envelope_emits_no_envelope_warning(self):
        """The normal HA shape must not add warning noise."""
        tools = _make_tools(
            {"changed_states": [_CHANGED_STATE], "service_response": _SHELL_RESPONSE}
        )

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert "warnings" not in response, (
            f"a conforming envelope must not warn: {response!r}"
        )

    async def test_null_service_response_is_still_set(self):
        """A legitimately null response data is reported, not dropped."""
        tools = _make_tools({"changed_states": [], "service_response": None})

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert "service_response" in response
        assert response["service_response"] is None
        assert response["result"] == []

    async def test_result_fields_projects_the_changed_states(self):
        """result_fields now reaches the changed states on a return_response call.

        Pre-fix ``result`` was the envelope dict, so ``_project_service_result``
        bailed on its ``not isinstance(result, list)`` guard and the parameter was
        silently ignored.
        """
        tools = _make_tools(
            {"changed_states": [_CHANGED_STATE], "service_response": _SHELL_RESPONSE}
        )

        response = await tools.ha_call_service(
            domain="shell_command",
            service="test",
            return_response=True,
            result_fields=["entity_id"],
        )

        assert response["result"] == [{"entity_id": "light.x"}]
        assert response["service_response"] == _SHELL_RESPONSE

    async def test_entity_id_filters_the_changed_states(self):
        """Compaction's target filter now applies to return_response changed states."""
        other_state = {**_CHANGED_STATE, "entity_id": "light.parent_group"}
        tools = _make_tools(
            {
                "changed_states": [_CHANGED_STATE, other_state],
                "service_response": _SHELL_RESPONSE,
            }
        )

        response = await tools.ha_call_service(
            domain="shell_command",
            service="test",
            entity_id="light.x",
            return_response=True,
        )

        result = response["result"]
        assert [record["entity_id"] for record in result] == ["light.x"], (
            f"the propagation chain must be filtered to the target: {result!r}"
        )
        assert response["service_response"] == _SHELL_RESPONSE

    async def test_projection_and_envelope_warnings_both_surface(self):
        """Warnings from projection and from the envelope split are concatenated.

        ``result_attribute_keys`` without ``attributes`` in ``result_fields`` emits
        a projection warning; the missing ``service_response`` emits the envelope
        warning. Both must reach the caller — one source must not shadow the other.
        """
        tools = _make_tools({"changed_states": [_CHANGED_STATE]})

        response = await tools.ha_call_service(
            domain="shell_command",
            service="test",
            return_response=True,
            result_fields=["state"],
            result_attribute_keys=["brightness"],
        )

        warnings = response["warnings"]
        assert _NON_ENVELOPE_WARNING in warnings, (
            f"the envelope warning must survive alongside projection ones: {warnings!r}"
        )
        assert any("result_attribute_keys was ignored" in w for w in warnings), (
            f"the projection warning must survive alongside the envelope one: {warnings!r}"
        )

    async def test_timeout_still_emits_the_requested_key(self):
        """A timed-out call must not be the one shape missing service_response.

        The reply never arrived, so the value is necessarily null — and that null
        proves nothing, hence the ambiguity warning rather than a bare null.
        """
        timed_out = HomeAssistantConnectionError("timed out")
        # ``_handle_connection_error`` classifies on ``__cause__`` — an httpx
        # timeout is a dispatched-but-unanswered call, not a failure.
        timed_out.__cause__ = httpx.ReadTimeout("slow")
        tools = _make_tools(None)
        tools._client.call_service = AsyncMock(side_effect=timed_out)

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert response["partial"] is True
        assert "service_response" in response
        assert response["service_response"] is None
        assert any("does NOT prove" in w for w in response["warnings"]), (
            f"a timed-out return_response call must flag the ambiguity: {response!r}"
        )

    async def test_return_response_false_has_no_service_response_key(self):
        """Without return_response the REST reply is a plain changed-states list."""
        tools = _make_tools([_CHANGED_STATE])

        response = await tools.ha_call_service(domain="shell_command", service="test")

        assert "service_response" not in response
        result = response["result"]
        assert isinstance(result, list)
        assert result[0]["entity_id"] == "light.x"
