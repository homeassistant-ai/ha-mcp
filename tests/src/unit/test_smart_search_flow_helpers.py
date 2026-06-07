"""Unit tests for ``ha_search`` coverage of UI-created flow-based
helpers (template, group, utility_meter, derivative, ...).

Issue #1457: deep_search previously hard-coded the helper list to
``input_*`` only, so config-entry helpers were invisible. The flow-helper
branch now lists config entries for any domain in ``FLOW_HELPER_TYPES``
and probes each entry's options flow so the helper's current config
(template body, group members, etc.) is searchable alongside the
storage-based helpers.
"""

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools


def _make_tools(client: Any) -> SmartSearchTools:
    """Construct SmartSearchTools without loading global settings."""
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


def _make_flow_form(suggested_value: str) -> dict[str, Any]:
    """Build the options-flow response shape HA returns for a template helper."""
    return {
        "flow_id": "flow-1",
        "type": "form",
        "step_id": "binary_sensor",
        "data_schema": [
            {
                "name": "state",
                "selector": {"template": {}},
                "description": {"suggested_value": suggested_value},
                "required": True,
            }
        ],
    }


@pytest.mark.asyncio
class TestFlowHelperDeepSearch:
    """``_search_flow_helpers`` surfaces flow-helper config entries."""

    async def test_template_helper_matches_on_title(self) -> None:
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXTEMPLATEWEATHER",
                    "domain": "template",
                    "title": "Weather Message",
                    "supports_options": True,
                },
                {
                    "entry_id": "01HXTEMPLATEOTHER",
                    "domain": "template",
                    "title": "Unrelated",
                    "supports_options": True,
                },
            ]
        )
        # Title-only exact match — no probe needed for the matching entry.
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form("{{ states('sensor.x') }}")
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        semaphore = asyncio.Semaphore(8)
        results, _ = await tools._search_flow_helpers(
            "weather", exact_match=True, semaphore=semaphore, include_config=False
        )

        entry_ids = {r["entry_id"] for r in results}
        assert "01HXTEMPLATEWEATHER" in entry_ids
        assert "01HXTEMPLATEOTHER" not in entry_ids
        match = next(r for r in results if r["entry_id"] == "01HXTEMPLATEWEATHER")
        assert match["helper_type"] == "template"
        assert match["name"] == "Weather Message"
        assert match["match_in_name"] is True
        # include_config=False → no config in result, no options probe needed.
        assert "config" not in match

    async def test_template_helper_matches_on_template_body(self) -> None:
        # Query is a substring of the template body but NOT the title —
        # forces the options-flow probe path.
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXTEMPLATEX",
                    "domain": "template",
                    "title": "Renamed Sensor",
                    "supports_options": True,
                },
            ]
        )
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form(
                "{{ states('sensor.outside_temperature') | float }}"
            )
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        results, _ = await tools._search_flow_helpers(
            "outside_temperature",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )

        assert len(results) == 1
        assert results[0]["entry_id"] == "01HXTEMPLATEX"
        assert results[0]["match_in_config"] is True
        client.start_options_flow.assert_awaited_once_with("01HXTEMPLATEX")
        client.abort_options_flow.assert_awaited_once_with("flow-1")

    async def test_include_config_attaches_options_body(self) -> None:
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXTEMPLATEZ",
                    "domain": "template",
                    "title": "Comfort Index",
                    "supports_options": True,
                }
            ]
        )
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form("{{ comfort_index() }}")
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        results, _ = await tools._search_flow_helpers(
            "comfort",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=True,
        )
        assert results[0]["config"] == {"state": "{{ comfort_index() }}"}
        # include_config=True forces the probe even though the title already
        # exact-matches (the config body has to be fetched to attach it).
        client.start_options_flow.assert_awaited_once_with("01HXTEMPLATEZ")

    async def test_skips_non_flow_helper_domains(self) -> None:
        # Mixed list: only the template entry should be considered.
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXLIGHTHUE",
                    "domain": "hue",
                    "title": "Hue Bridge",
                    "supports_options": True,
                },
                {
                    "entry_id": "01HXTEMPLATEAA",
                    "domain": "template",
                    "title": "Match Me",
                    "supports_options": True,
                },
            ]
        )
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form("{{ true }}")
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        results, _ = await tools._search_flow_helpers(
            "match",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert [r["entry_id"] for r in results] == ["01HXTEMPLATEAA"]

    async def test_skips_entries_without_supports_options(self) -> None:
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXNOOPTS",
                    "domain": "template",
                    "title": "Locked Template",
                    "supports_options": False,
                }
            ]
        )
        tools = _make_tools(client)
        results, _ = await tools._search_flow_helpers(
            "locked",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert results == []

    async def test_rest_call_failure_signals_failed_for_partial(self) -> None:
        # The config-entries list fetch raising means the whole flow-helper
        # surface is unreachable — it must signal a non-zero failure count (not
        # just an empty list) so ``_deep_search_helpers`` routes it to
        # ``partial``. The whole-surface failure counts as 1.
        client = MagicMock()
        client._request = AsyncMock(side_effect=RuntimeError("REST down"))
        tools = _make_tools(client)
        results, failed = await tools._search_flow_helpers(
            "anything",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert results == []
        assert failed == 1

    async def test_unexpected_list_shape_signals_failed(self) -> None:
        # A non-list response from the config-entries endpoint (e.g. an error
        # dict on a future HA version) is a backend failure, not "no helpers" —
        # it must signal a non-zero count rather than be swallowed to empty.
        client = MagicMock()
        client._request = AsyncMock(return_value={"error": "boom"})
        tools = _make_tools(client)
        results, failed = await tools._search_flow_helpers(
            "anything",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert results == []
        assert failed == 1

    async def test_empty_flow_entries_is_not_a_failure(self) -> None:
        # A successful list with no flow-helper entries is a genuine zero —
        # the failure count must stay 0 so a clean instance doesn't report
        # partial.
        client = MagicMock()
        client._request = AsyncMock(return_value=[])
        tools = _make_tools(client)
        results, failed = await tools._search_flow_helpers(
            "anything",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert results == []
        assert failed == 0

    async def test_does_not_match_on_opaque_entry_id(self) -> None:
        # Regression (issue #1457 review): the config-entry ULID must not be a
        # search target. Here the entry_id contains "weather" but neither the
        # title nor the template body does — the entry must NOT match "weather".
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXWEATHERZZZ",
                    "domain": "template",
                    "title": "Bedroom Light",
                    "supports_options": True,
                }
            ]
        )
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form("{{ 1 + 1 }}")
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        results, _ = await tools._search_flow_helpers(
            "weather",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert results == []

    async def test_skips_entry_with_non_string_entry_id(self) -> None:
        # A malformed config entry (missing/None entry_id) is skipped without a
        # probe; the valid sibling is still returned.
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": None,
                    "domain": "template",
                    "title": "Match Me",
                    "supports_options": True,
                },
                {
                    "entry_id": "01HXTEMPLATEOK",
                    "domain": "template",
                    "title": "Match Me",
                    "supports_options": True,
                },
            ]
        )
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form("{{ true }}")
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        results, _ = await tools._search_flow_helpers(
            "match",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert [r["entry_id"] for r in results] == ["01HXTEMPLATEOK"]

    async def test_probe_failure_counts_but_keeps_other_entries(self) -> None:
        # start_options_flow raising for one entry is a probe failure: that
        # entry's config body is never searched, so it doesn't match — but the
        # failure is now COUNTED (returned as 1) so deep_search can surface
        # ``partial`` instead of a silent false no-match. The healthy sibling
        # is still returned. The gather-level isolation path (a code bug inside
        # score_entry) is covered by test_score_entry_crash_is_isolated.
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXBAD",
                    "domain": "template",
                    "title": "Broken",
                    "supports_options": True,
                },
                {
                    "entry_id": "01HXGOOD",
                    "domain": "template",
                    "title": "Renamed Sensor",
                    "supports_options": True,
                },
            ]
        )

        async def flaky_flow(entry_id: str) -> dict[str, Any]:
            if entry_id == "01HXBAD":
                raise RuntimeError("flow init exploded")
            return _make_flow_form("{{ states('sensor.outside_temperature') }}")

        client.start_options_flow = AsyncMock(side_effect=flaky_flow)
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        results, failed = await tools._search_flow_helpers(
            "outside_temperature",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert [r["entry_id"] for r in results] == ["01HXGOOD"]
        # The bad entry's options-flow probe failed → counted as 1.
        assert failed == 1

    async def test_probe_failure_counted_even_when_entry_matches(self) -> None:
        # A probe failure must be counted even when the entry still MATCHES (on
        # title): the entry is returned, but its config body was never searched,
        # so the failure is reported so the caller sees ``partial``. Guards the
        # ``return result, probe_failed`` match-branch — a refactor that early-
        # returns ``result`` alone would drop the signal and ship green.
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXMATCH",
                    "domain": "template",
                    "title": "Kitchen Temp",
                    "supports_options": True,
                }
            ]
        )
        # Probe fails — title still matches, so the entry is returned, but the
        # config body is unread → the failure must be counted.
        client.start_options_flow = AsyncMock(
            side_effect=RuntimeError("options flow down")
        )
        client.abort_options_flow = AsyncMock()
        tools = _make_tools(client)

        # Force a mid-range title score (matches at threshold 60, below the
        # perfect-100 probe-skip), so the entry matches AND the probe runs.
        with patch.object(tools, "_score_deep_match", return_value=(70, 60, True)):
            results, failed = await tools._search_flow_helpers(
                "kitchen",
                exact_match=False,
                semaphore=asyncio.Semaphore(8),
                include_config=False,
            )

        assert [r["entry_id"] for r in results] == ["01HXMATCH"]
        # The probe failed → counted as 1, even though the entry matched.
        assert failed == 1

    async def test_score_entry_crash_is_isolated_and_logged(self, caplog) -> None:
        # A real bug inside score_entry (not a probe/API failure) is isolated by
        # gather: the bad entry is dropped and logged at WARNING (discoverable,
        # per review), the healthy entry is still returned, and the multi-source
        # search does not crash. Unlike a probe failure, a scoring bug is NOT a
        # backend outage, so it must NOT count toward partial (failed stays 0).
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXGOOD",
                    "domain": "template",
                    "title": "Good",
                    "supports_options": True,
                },
                {
                    "entry_id": "01HXBOOM",
                    "domain": "template",
                    "title": "Boom",
                    "supports_options": True,
                },
            ]
        )
        client.start_options_flow = AsyncMock(return_value=_make_flow_form("{{ x }}"))
        client.abort_options_flow = AsyncMock()
        tools = _make_tools(client)

        def scorer(entity_id, friendly_name, *args, **kwargs):
            if friendly_name == "Boom":
                raise TypeError("scoring blew up")
            return (100, 100, True)

        with (
            patch.object(tools, "_score_deep_match", side_effect=scorer),
            caplog.at_level(logging.WARNING, logger="ha_mcp.tools.smart_search"),
        ):
            results, failed = await tools._search_flow_helpers(
                "good",
                exact_match=True,
                semaphore=asyncio.Semaphore(8),
                include_config=False,
            )

        assert [r["entry_id"] for r in results] == ["01HXGOOD"]
        assert "flow-helper scoring failed" in caplog.text
        # A scoring bug is logged but not counted — it's a code error, not a
        # backend probe failure, so it must not inflate the partial count.
        assert failed == 0

    async def test_below_threshold_score_filters_entry(self) -> None:
        # A non-zero score below the threshold is filtered via
        # `if total_score < threshold: return None`.
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXLOW",
                    "domain": "template",
                    "title": "Low Score",
                    "supports_options": True,
                }
            ]
        )
        client.start_options_flow = AsyncMock(return_value=_make_flow_form("{{ x }}"))
        client.abort_options_flow = AsyncMock()
        tools = _make_tools(client)

        with patch.object(tools, "_score_deep_match", return_value=(50, 100, False)):
            results, _ = await tools._search_flow_helpers(
                "x",
                exact_match=True,
                semaphore=asyncio.Semaphore(8),
                include_config=False,
            )
        assert results == []

    async def test_fuzzy_mode_probes_when_title_below_perfect_score(self) -> None:
        # Regression guard for the probe-skip threshold. In fuzzy mode a title
        # scoring below 100 (but above fuzzy_threshold) must STILL trigger the
        # options probe — the config could score higher, and results sort by
        # score. The earlier logic skipped the probe once the title cleared
        # fuzzy_threshold, under-ranking such entries.
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXFUZZY",
                    "domain": "template",
                    "title": "Weather Stuff",
                    "supports_options": True,
                }
            ]
        )
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form("{{ true }}")
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        # Force a mid-range title score: above fuzzy_threshold (60), below 100.
        with patch.object(tools, "_score_deep_match", return_value=(70, 60, True)):
            results, _ = await tools._search_flow_helpers(
                "weather",
                exact_match=False,
                semaphore=asyncio.Semaphore(8),
                include_config=False,
            )
        client.start_options_flow.assert_awaited_once_with("01HXFUZZY")
        assert [r["entry_id"] for r in results] == ["01HXFUZZY"]
