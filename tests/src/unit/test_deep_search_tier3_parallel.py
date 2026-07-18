"""Unit tests for parallel Attempt C config fetching in deep_search.

Validates that when bulk config fetches fail (Attempts A & B), Attempt C
fetches configs in parallel batches without name-score prioritization. This
ensures entities referenced only inside automation/script conditions/actions
(not in the name) are still found. Regression test for #879.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.smart_search._fetch import (
    is_timeout_error,
    record_first_failure,
    summarize_fetch_error,
)


def _make_tools(client):
    """Create SmartSearchTools with mocked global settings."""
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


def _make_entity(entity_id: str, friendly_name: str) -> dict:
    return {
        "entity_id": entity_id,
        "state": "on",
        "attributes": {"friendly_name": friendly_name},
    }


def _make_automation_entities(count: int) -> list[dict]:
    """Create automation entities with unique IDs in attributes."""
    return [
        {
            "entity_id": f"automation.auto_{i}",
            "state": "on",
            "attributes": {
                "friendly_name": f"Automation {i}",
                "id": f"uid_{i}",
            },
        }
        for i in range(count)
    ]


class TestAttemptCParallelFetch:
    """Test that Attempt C fetches configs in parallel without name-score prioritization."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # Bulk fetch fails (triggers Tier 3)
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_attempt_c_fetches_all_configs_not_just_name_matches(
        self, mock_client, smart_tools
    ):
        """Configs for ALL automations should be fetched, not just name-matched ones.

        Regression test for #879: an automation named "Morning Routine" that
        references "sensor.kitchen_temp" in its conditions should be found when
        searching for "kitchen_temp", even though the name doesn't match.
        """
        # Create automations: only auto_2 has the search term in its config,
        # but its NAME doesn't match the query at all.
        automations = [
            {
                "entity_id": "automation.morning_routine",
                "state": "on",
                "attributes": {
                    "friendly_name": "Morning Routine",
                    "id": "uid_morning",
                },
            },
            {
                "entity_id": "automation.evening_lights",
                "state": "on",
                "attributes": {
                    "friendly_name": "Evening Lights",
                    "id": "uid_evening",
                },
            },
        ]

        all_entities = automations + [
            _make_entity("sensor.kitchen_temp", "Kitchen Temperature"),
        ]

        mock_client.get_states = AsyncMock(return_value=all_entities)

        # Track which UIDs get fetched
        fetched_uids = []

        async def _individual_fetch(method: str, url: str) -> dict:
            uid = url.split("/")[-1]
            fetched_uids.append(uid)
            # "Morning Routine" references sensor.kitchen_temp in condition
            if uid == "uid_morning":
                return {
                    "id": uid,
                    "trigger": [{"platform": "time", "at": "07:00"}],
                    "condition": [
                        {
                            "condition": "numeric_state",
                            "entity_id": "sensor.kitchen_temp",
                            "below": 20,
                        }
                    ],
                    "action": [{"service": "light.turn_on"}],
                }
            return {
                "id": uid,
                "trigger": [{"platform": "time", "at": "18:00"}],
                "action": [
                    {
                        "service": "light.turn_on",
                        "target": {"entity_id": "light.living_room"},
                    }
                ],
            }

        mock_client._request = AsyncMock(side_effect=_individual_fetch)
        # Keep WebSocket failing to force Tier 3
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )

        result = await smart_tools.deep_search(
            query="kitchen_temp",
            search_types=["automation"],
            limit=10,
        )

        # Both UIDs should have been fetched (not just name-matched ones)
        assert "uid_morning" in fetched_uids, (
            "Morning Routine config should be fetched even though "
            "its name doesn't match 'kitchen_temp'"
        )
        assert "uid_evening" in fetched_uids, (
            "All automation configs should be fetched in Tier 3"
        )

        # The search should find the automation that references kitchen_temp
        auto_results = result.get("automations", [])
        matched_ids = [r["entity_id"] for r in auto_results]
        assert "automation.morning_routine" in matched_ids, (
            f"Should find automation referencing kitchen_temp in conditions. "
            f"Got: {matched_ids}"
        )

    @pytest.mark.asyncio
    async def test_attempt_c_respects_time_budget(self, mock_client, smart_tools):
        """Attempt C should stop fetching when time budget is exhausted."""
        automations = _make_automation_entities(30)
        mock_client.get_states = AsyncMock(return_value=automations)

        call_count = 0

        async def _slow_fetch(method: str, url: str) -> dict:
            nonlocal call_count
            uid = url.split("/")[-1]
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk unavailable")
            call_count += 1
            await asyncio.sleep(0.01)  # Minimal sleep to yield control
            return {"id": uid, "action": []}

        mock_client._request = AsyncMock(side_effect=_slow_fetch)
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )

        # Budget of 0.005s: batch 1 starts at t=0 (passes check), but by the
        # time it completes (~0.01s), the budget is exceeded so batch 2 is skipped.
        with patch(
            "ha_mcp.tools.smart_search._deep.AUTOMATION_CONFIG_TIME_BUDGET", 0.005
        ):
            await smart_tools.deep_search(
                query="test",
                search_types=["automation"],
                limit=10,
            )

        # Batch 1 (10 items) completes because the budget check happens before
        # launching each batch. Batch 2 at ~0.01s exceeds the 0.005s budget.
        assert call_count == 10, (
            f"Expected exactly one batch of 10, but fetched {call_count}"
        )

    @pytest.mark.parametrize(
        ("search_type", "budget_const_path", "individual_attr"),
        [
            (
                "automation",
                "ha_mcp.tools.smart_search._deep.AUTOMATION_CONFIG_TIME_BUDGET",
                "_request",
            ),
            (
                "script",
                "ha_mcp.tools.smart_search._deep.SCRIPT_CONFIG_TIME_BUDGET",
                "get_script_config",
            ),
            (
                "scene",
                "ha_mcp.tools.smart_search._scenes.SCENE_CONFIG_TIME_BUDGET",
                "get_scene_config",
            ),
        ],
        ids=["automation", "script", "scene"],
    )
    async def test_config_time_budget_param_overrides_env_default(
        self,
        mock_client,
        smart_tools,
        search_type,
        budget_const_path,
        individual_attr,
    ):
        """`config_time_budget=` param replaces the per-type env default for that call.

        Pins the "automation, script, AND scene" promise in the param
        docstring — all three per-type branches feed the same
        ``_individual_fetch_budgeted`` helper, so a regression that
        silently drops the override on one branch would otherwise slip
        through (the previous automation-only assertion missed two of the
        three threads).

        The env-default is patched high enough that without an override the
        test would fetch all three batches; the per-call override is tight
        enough to skip after the first batch (10 fetches). The assertion
        ``call_count == 10`` proves the override was honoured rather than
        silently ignored.
        """
        entities = [
            {
                "entity_id": f"{search_type}.x_{i}",
                "state": "on",
                "attributes": {"friendly_name": f"X {i}", "id": f"uid_{i}"},
            }
            for i in range(30)
        ]
        mock_client.get_states = AsyncMock(return_value=entities)

        call_count = 0

        async def _count_individual() -> dict:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return {"id": "x", "config": {}, "action": []}

        if search_type == "automation":
            # `_request` covers both the bulk-fetch URL (must raise to force
            # Tier 3) and the per-id URL (must count). Discriminate by path.
            async def _auto_request(method: str, url: str) -> dict:
                if url.rstrip("/") == "/config/automation/config":
                    raise Exception("Bulk unavailable")
                return await _count_individual()

            mock_client._request = AsyncMock(side_effect=_auto_request)
        else:
            # Script/scene per-id calls go through a dedicated client method;
            # the fixture's `_request`/`send_websocket_message` exceptions
            # already block their bulk-fetch and registry-walk tiers.
            async def _typed_individual(sid: str) -> dict:
                return await _count_individual()

            setattr(
                mock_client,
                individual_attr,
                AsyncMock(side_effect=_typed_individual),
            )

        # Env default high (would fetch all 3 batches); per-call override
        # is tight (stops after batch 1).
        with patch(budget_const_path, 60.0):
            await smart_tools.deep_search(
                query="test",
                search_types=[search_type],
                limit=10,
                config_time_budget=0.005,
            )

        assert call_count == 10, (
            f"Per-call config_time_budget=0.005 must override the {search_type} "
            f"env default; got {call_count} fetches (expected 10 = single batch)"
        )


class TestAttemptCScriptParallelFetch:
    """Test that Attempt C works for scripts (structurally different from automations)."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_script_attempt_c_fetches_configs(self, mock_client, smart_tools):
        """Script Attempt C should fetch configs and find matches inside them.

        Scripts use a different tuple format (entity_id, friendly_name, script_id,
        name_score) and client.get_script_config() instead of client._request().
        """
        scripts = [
            {
                "entity_id": "script.morning_coffee",
                "state": "off",
                "attributes": {"friendly_name": "Morning Coffee"},
            },
            {
                "entity_id": "script.night_lockup",
                "state": "off",
                "attributes": {"friendly_name": "Night Lockup"},
            },
        ]
        all_entities = scripts + [
            _make_entity("lock.front_door", "Front Door Lock"),
        ]
        mock_client.get_states = AsyncMock(return_value=all_entities)

        fetched_sids = []

        async def _script_config(sid: str) -> dict:
            fetched_sids.append(sid)
            if sid == "night_lockup":
                return {
                    "config": {
                        "sequence": [
                            {
                                "service": "lock.lock",
                                "target": {"entity_id": "lock.front_door"},
                            }
                        ]
                    }
                }
            return {"config": {"sequence": [{"service": "switch.turn_on"}]}}

        mock_client.get_script_config = AsyncMock(side_effect=_script_config)

        result = await smart_tools.deep_search(
            query="front_door",
            search_types=["script"],
            limit=10,
        )

        # Both script configs should have been fetched
        assert len(fetched_sids) == 2, f"Expected 2 script fetches, got {fetched_sids}"

        # Night Lockup references front_door in its sequence
        script_results = result.get("scripts", [])
        matched_ids = [r["entity_id"] for r in script_results]
        assert "script.night_lockup" in matched_ids, (
            f"Should find script referencing front_door. Got: {matched_ids}"
        )


class TestYamlSkippedClassification:
    """Component-level coverage of the 404 → ``yaml_skipped`` classification.

    These tests drive ``_deep_search_automations`` / ``_deep_search_scripts``
    directly and assert on their returned 6-tuple, pinning the
    FETCH→CLASSIFY→COUNT path *inside* each per-type helper (wrong exception
    type, wrong status-code attribute, wrong return slot) so a regression
    surfaces here rather than silently misclassifying YAML-defined entities
    as generic ``failed``. They stop at the helper return; the
    public-entrypoint seam (``deep_search`` forwarding the 4th tuple slot
    through ``_paginate_and_build_response`` into ``partial`` /
    ``partial_reason``) is covered by ``TestYamlSkippedThroughDeepSearch``.

    The companion unit tests for ``_apply_per_type_partial_flag`` in
    ``test_ha_search_merge.py`` cover the warning-assembly side.

    The classification matters for find-references honesty: only the
    ``yaml_skipped`` warning explains the gap is *structural* (the
    config exists, the REST endpoint just can't return it), so a caller
    knows not to retry. KP13's PR #1529 R5 blind-agent BAT found that
    lumping these into ``failed`` let agents rationalise the result as
    complete.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # Bulk fetch fails (triggers Tier 3 per-id fallback).
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_automation_404_classifies_as_yaml_skipped(
        self, mock_client, smart_tools
    ):
        """A 404 on ``/config/automation/config/<uid>`` must increment
        ``yaml_skipped_count`` (not ``failed_count``). The 404 is the
        documented HA behaviour for YAML-defined automations."""
        automations = [
            {
                "entity_id": "automation.yaml_one",
                "state": "on",
                "attributes": {"friendly_name": "YAML One", "id": "uid_yaml_one"},
            },
            {
                "entity_id": "automation.yaml_two",
                "state": "on",
                "attributes": {"friendly_name": "YAML Two", "id": "uid_yaml_two"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _per_id_404(method: str, url: str) -> dict:
            # Bulk URL stays at the fixture's generic Exception (drives
            # fallback to Attempt C); per-id URL returns 404 like the
            # live HA REST API does for YAML-defined automations.
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            raise HomeAssistantAPIError(
                f"API error: 404 - Not Found ({url})", status_code=404
            )

        mock_client._request = AsyncMock(side_effect=_per_id_404)

        (
            matches,
            skipped_count,
            failed_count,
            yaml_skipped_count,
            _timeout_count,
            _failed_sample,
        ) = await smart_tools._deep_search_automations(
            automations,
            {
                "automation.yaml_one": "uid_yaml_one",
                "automation.yaml_two": "uid_yaml_two",
            },
            query_lower="anything",
            exact_match=False,
        )
        assert yaml_skipped_count == 2, (
            f"both automation 404s must classify as yaml_skipped; got "
            f"yaml_skipped={yaml_skipped_count}, failed={failed_count}"
        )
        assert failed_count == 0, (
            f"404s must NOT count as generic failed; got failed={failed_count}"
        )
        assert skipped_count == 0
        assert matches == []
        assert _failed_sample is None, "yaml_skipped 404s must not capture a sample"

    @pytest.mark.asyncio
    async def test_automation_non_404_classifies_as_failed(
        self, mock_client, smart_tools
    ):
        """A non-404 ``HomeAssistantAPIError`` (e.g. 500) and a generic
        ``Exception`` both classify as ``failed`` — the structural-vs-
        transient distinction only fires on 404."""
        automations = [
            {
                "entity_id": "automation.five_hundred",
                "state": "on",
                "attributes": {
                    "friendly_name": "Five Hundred",
                    "id": "uid_500",
                },
            },
            {
                "entity_id": "automation.generic_oops",
                "state": "on",
                "attributes": {
                    "friendly_name": "Generic Oops",
                    "id": "uid_oops",
                },
            },
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _per_id_mixed(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            if url.endswith("uid_500"):
                raise HomeAssistantAPIError(
                    "API error: 500 - Internal Server Error", status_code=500
                )
            raise RuntimeError("generic explosion")

        mock_client._request = AsyncMock(side_effect=_per_id_mixed)

        (
            _matches,
            _skipped_count,
            failed_count,
            yaml_skipped_count,
            _timeout_count,
            _failed_sample,
        ) = await smart_tools._deep_search_automations(
            automations,
            {
                "automation.five_hundred": "uid_500",
                "automation.generic_oops": "uid_oops",
            },
            query_lower="anything",
            exact_match=False,
        )
        assert failed_count == 2, (
            f"500 + generic Exception must count as failed; got "
            f"failed={failed_count}, yaml_skipped={yaml_skipped_count}"
        )
        assert yaml_skipped_count == 0, (
            f"only 404s count as yaml_skipped; got yaml_skipped={yaml_skipped_count}"
        )

    @pytest.mark.asyncio
    async def test_automation_none_status_code_classifies_as_failed(
        self, mock_client, smart_tools
    ):
        """A ``HomeAssistantAPIError`` with the constructor-default
        ``status_code=None`` must classify as ``failed`` — only an exact
        404 triggers the structural ``yaml_skipped`` class, never any
        ``HomeAssistantAPIError`` (a status-less API error is transient,
        not a YAML-defined-entity signal)."""
        automations = [
            {
                "entity_id": "automation.none_status",
                "state": "on",
                "attributes": {"friendly_name": "None Status", "id": "uid_none"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _per_id_none_status(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            # status_code defaults to None — not an explicit 404.
            raise HomeAssistantAPIError("API error: connection reset")

        mock_client._request = AsyncMock(side_effect=_per_id_none_status)

        (
            _matches,
            _skipped_count,
            failed_count,
            yaml_skipped_count,
            _timeout_count,
            _failed_sample,
        ) = await smart_tools._deep_search_automations(
            automations,
            {"automation.none_status": "uid_none"},
            query_lower="anything",
            exact_match=False,
        )
        assert failed_count == 1, (
            f"status_code=None must classify as failed, not yaml_skipped; got "
            f"failed={failed_count}, yaml_skipped={yaml_skipped_count}"
        )
        assert yaml_skipped_count == 0

    @pytest.mark.asyncio
    async def test_script_404_classifies_as_yaml_skipped(
        self, mock_client, smart_tools
    ):
        """Mirror of the automation 404 test for scripts. The script
        path uses ``client.get_script_config`` (which already re-raises
        404s as ``HomeAssistantAPIError(status_code=404)`` per the REST
        client) rather than ``client._request``, so a separate fetch
        surface needs separate coverage."""
        scripts = [
            {
                "entity_id": "script.yaml_alpha",
                "state": "off",
                "attributes": {"friendly_name": "YAML Alpha"},
            },
            {
                "entity_id": "script.yaml_beta",
                "state": "off",
                "attributes": {"friendly_name": "YAML Beta"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=scripts)

        async def _script_404(sid: str) -> dict:
            raise HomeAssistantAPIError(f"Script not found: {sid}", status_code=404)

        mock_client.get_script_config = AsyncMock(side_effect=_script_404)

        (
            matches,
            skipped_count,
            failed_count,
            yaml_skipped_count,
            _timeout_count,
            _failed_sample,
        ) = await smart_tools._deep_search_scripts(
            scripts,
            query_lower="anything",
            exact_match=False,
        )
        assert yaml_skipped_count == 2, (
            f"both script 404s must classify as yaml_skipped; got "
            f"yaml_skipped={yaml_skipped_count}, failed={failed_count}"
        )
        assert failed_count == 0
        assert skipped_count == 0
        assert matches == []
        assert _failed_sample is None, "yaml_skipped 404s must not capture a sample"

    @pytest.mark.asyncio
    async def test_mixed_404_and_success_only_404s_yaml_skipped(
        self, mock_client, smart_tools
    ):
        """When some automations fetch successfully and others 404, only
        the 404s count as ``yaml_skipped`` — and the successful UI-stored
        automation flows into ``matches`` via its fetched config. The
        query matches only inside ``ui_one``'s config body (not either
        name), so a hit proves the success path fetched and scored the
        config rather than name-matching."""
        automations = [
            {
                "entity_id": "automation.ui_one",
                "state": "on",
                "attributes": {"friendly_name": "UI One", "id": "uid_ui_one"},
            },
            {
                "entity_id": "automation.yaml_one",
                "state": "on",
                "attributes": {
                    "friendly_name": "YAML One",
                    "id": "uid_yaml_one",
                },
            },
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _mixed_per_id(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            if url.endswith("uid_ui_one"):
                # The query token lives only here, inside the config body.
                return {
                    "id": "uid_ui_one",
                    "action": [
                        {
                            "service": "light.turn_on",
                            "target": {"entity_id": "light.lockup_marker"},
                        }
                    ],
                }
            raise HomeAssistantAPIError(
                f"API error: 404 - Not Found ({url})", status_code=404
            )

        mock_client._request = AsyncMock(side_effect=_mixed_per_id)

        (
            matches,
            skipped_count,
            failed_count,
            yaml_skipped_count,
            _timeout_count,
            _failed_sample,
        ) = await smart_tools._deep_search_automations(
            automations,
            {
                "automation.ui_one": "uid_ui_one",
                "automation.yaml_one": "uid_yaml_one",
            },
            query_lower="lockup_marker",
            exact_match=False,
        )
        assert yaml_skipped_count == 1, (
            f"only the YAML automation should count as yaml_skipped; got "
            f"yaml_skipped={yaml_skipped_count}, failed={failed_count}"
        )
        assert failed_count == 0
        assert skipped_count == 0
        assert _failed_sample is None, "yaml_skipped 404s must not capture a sample"
        # Two-sided: the successful UI-stored automation must land in
        # matches via its config body (the query matches nothing else).
        matched_ids = [m["entity_id"] for m in matches]
        assert matched_ids == ["automation.ui_one"], (
            f"the UI-stored automation must match on its fetched config and "
            f"the YAML 404 must not appear; got {matched_ids}"
        )


class TestYamlSkippedThroughDeepSearch:
    """End-to-end coverage of the ``yaml_skipped`` seam through the public
    ``deep_search`` entrypoint.

    ``TestYamlSkippedClassification`` pins each per-type helper's returned
    6-tuple; these tests pin the wiring *between* that return and the
    response — ``deep_search`` unpacks the 4th slot (the ``if "automation"``
    / ``if "script"`` blocks in ``deep_search``) and forwards it via the
    ``_paginate_and_build_response`` call to
    ``_apply_per_type_partial_flag``. A regression that unpacked the slot
    but left ``automation_yaml_skipped=`` at its ``0`` default would ship a
    ``partial: False`` response with no warning — the exact find-references
    honesty bug this commit exists to fix — while every component-level test
    still passed. These guard the gap by asserting the YAML fragment reaches
    ``result["partial_reason"]`` via the public call.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # Bulk fetch fails (triggers Tier 3 per-id fallback).
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_automation_404_surfaces_partial_through_deep_search(
        self, mock_client, smart_tools
    ):
        """An automation 404 driven through ``deep_search`` must set
        ``result["partial"] is True`` and name the structural YAML gap in
        ``result["partial_reason"]`` — pins the 4th-slot forward the
        component tests can't see."""
        automations = [
            {
                "entity_id": "automation.yaml_one",
                "state": "on",
                "attributes": {
                    "friendly_name": "YAML One",
                    "id": "uid_yaml_one",
                },
            },
            {
                "entity_id": "automation.yaml_two",
                "state": "on",
                "attributes": {
                    "friendly_name": "YAML Two",
                    "id": "uid_yaml_two",
                },
            },
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _per_id_404(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            raise HomeAssistantAPIError(
                f"API error: 404 - Not Found ({url})", status_code=404
            )

        mock_client._request = AsyncMock(side_effect=_per_id_404)

        result = await smart_tools.deep_search(
            query="anything",
            search_types=["automation"],
            limit=10,
        )

        assert result["partial"] is True, (
            f"a YAML-defined automation 404 must flag partial through "
            f"deep_search; got {result.get('partial')!r}"
        )
        reason = result["partial_reason"]
        assert "likely YAML-defined automations" in reason, (
            f"partial_reason must name the structural YAML gap; got {reason!r}"
        )
        # The count must flow through the seam too, not just the flag: two
        # YAML automations 404, so the real yaml_skipped count (2) must
        # reach the reason. Pins against a forward that hardcodes the slot
        # (e.g. =1) instead of passing the actual count.
        assert re.search(r"\b2 automation\(s\)", reason), (
            f"partial_reason must carry the real yaml_skipped count (2) as a "
            f"standalone token (not a substring of e.g. '12'); got {reason!r}"
        )

    @pytest.mark.asyncio
    async def test_script_404_surfaces_partial_through_deep_search(
        self, mock_client, smart_tools
    ):
        """Mirror for the script fetch surface (``get_script_config``),
        which forwards its own 4th slot (``script_yaml_skipped``)."""
        scripts = [
            {
                "entity_id": "script.yaml_one",
                "state": "off",
                "attributes": {"friendly_name": "YAML One"},
            },
            {
                "entity_id": "script.yaml_two",
                "state": "off",
                "attributes": {"friendly_name": "YAML Two"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=scripts)

        async def _script_404(sid: str) -> dict:
            raise HomeAssistantAPIError(f"Script not found: {sid}", status_code=404)

        mock_client.get_script_config = AsyncMock(side_effect=_script_404)

        result = await smart_tools.deep_search(
            query="anything",
            search_types=["script"],
            limit=10,
        )

        assert result["partial"] is True, (
            f"a YAML-defined script 404 must flag partial through "
            f"deep_search; got {result.get('partial')!r}"
        )
        reason = result["partial_reason"]
        assert "likely YAML-defined scripts" in reason, (
            f"partial_reason must name the structural YAML gap; got {reason!r}"
        )
        # The count must flow through the seam too (separate slot,
        # script_yaml_skipped, via the get_script_config surface).
        assert re.search(r"\b2 script\(s\)", reason), (
            f"partial_reason must carry the real yaml_skipped count (2) as a "
            f"standalone token (not a substring of e.g. '12'); got {reason!r}"
        )


class TestTimeoutClassification:
    """Component + seam coverage of the per-request-timeout → ``timeout``
    classification (issue #1784).

    A real ``asyncio.wait_for`` expiry inside Attempt C must increment the
    new 5th tuple slot (``timeout_count``) — NOT ``failed_count``. Before
    #1784 these landed in the generic non-404 ``failed`` bucket and
    ``partial_reason`` said the fetch "raised a non-404 error", sending
    users hunting for broken automations that don't exist when the real
    cause was batch concurrency queueing past the per-request timeout on a
    server that serializes config reads. The component tests drive the
    REAL ``wait_for`` path (patched-down timeout + slow fetch), not a
    hand-raised ``TimeoutError``, so a refactor that moves or unwraps
    ``wait_for`` still trips them.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # Bulk fetch fails (triggers Tier 3 per-id fallback).
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_automation_slow_fetch_classifies_as_timeout(
        self, mock_client, smart_tools
    ):
        """A per-id fetch that outlives INDIVIDUAL_CONFIG_TIMEOUT must
        count in the ``timeout`` slot, with ``failed`` staying 0."""
        automations = [
            {
                "entity_id": "automation.slow_one",
                "state": "on",
                "attributes": {"friendly_name": "Slow One", "id": "uid_slow_one"},
            },
            {
                "entity_id": "automation.slow_two",
                "state": "on",
                "attributes": {"friendly_name": "Slow Two", "id": "uid_slow_two"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _slow_per_id(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            # Healthy but slow — the serialized-server shape from #1784:
            # the request WOULD return 200, it just outlives the timeout.
            await asyncio.sleep(0.2)
            return {"id": url.rsplit("/", 1)[-1], "alias": "slow"}

        mock_client._request = AsyncMock(side_effect=_slow_per_id)

        with patch("ha_mcp.tools.smart_search._deep.INDIVIDUAL_CONFIG_TIMEOUT", 0.05):
            (
                matches,
                skipped_count,
                failed_count,
                yaml_skipped_count,
                timeout_count,
                _failed_sample,
            ) = await smart_tools._deep_search_automations(
                automations,
                {
                    "automation.slow_one": "uid_slow_one",
                    "automation.slow_two": "uid_slow_two",
                },
                query_lower="anything",
                exact_match=False,
            )
        assert timeout_count == 2, (
            f"both wait_for expiries must classify as timeout; got "
            f"timeout={timeout_count}, failed={failed_count}"
        )
        assert failed_count == 0, (
            f"timeouts must NOT count as generic failed; got failed={failed_count}"
        )
        assert yaml_skipped_count == 0
        assert skipped_count == 0
        assert matches == []
        assert _failed_sample is None, "timeouts must not capture a sample"

    @pytest.mark.asyncio
    async def test_script_slow_fetch_classifies_as_timeout(
        self, mock_client, smart_tools
    ):
        """Mirror for the script fetch surface (``get_script_config``)."""
        scripts = [
            {
                "entity_id": "script.slow_one",
                "state": "off",
                "attributes": {"friendly_name": "Slow One"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=scripts)

        async def _slow_script(sid: str) -> dict:
            await asyncio.sleep(0.2)
            return {"config": {"alias": "slow"}}

        mock_client.get_script_config = AsyncMock(side_effect=_slow_script)

        with patch("ha_mcp.tools.smart_search._deep.INDIVIDUAL_CONFIG_TIMEOUT", 0.05):
            (
                _matches,
                _skipped_count,
                failed_count,
                yaml_skipped_count,
                timeout_count,
                _failed_sample,
            ) = await smart_tools._deep_search_scripts(
                scripts,
                query_lower="anything",
                exact_match=False,
            )
        assert timeout_count == 1, (
            f"script wait_for expiry must classify as timeout; got "
            f"timeout={timeout_count}, failed={failed_count}"
        )
        assert failed_count == 0
        assert yaml_skipped_count == 0
        assert _failed_sample is None, "timeouts must not capture a sample"

    @pytest.mark.asyncio
    async def test_timeout_surfaces_partial_through_deep_search(
        self, mock_client, smart_tools
    ):
        """A timeout driven through public ``deep_search`` must set
        ``partial: True`` and word the reason as a timeout pointing at the
        concurrency knobs — NOT as "raised a non-404 error". Pins the
        5th-slot forward through ``_paginate_and_build_response`` →
        ``_apply_per_type_partial_flag`` that the component tests can't
        see (a regression leaving ``automation_timeout=0`` at its default
        would pass them)."""
        automations = [
            {
                "entity_id": "automation.slow_one",
                "state": "on",
                "attributes": {"friendly_name": "Slow One", "id": "uid_slow_one"},
            },
            {
                "entity_id": "automation.slow_two",
                "state": "on",
                "attributes": {"friendly_name": "Slow Two", "id": "uid_slow_two"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _slow_per_id(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            await asyncio.sleep(0.2)
            return {"id": url.rsplit("/", 1)[-1], "alias": "slow"}

        mock_client._request = AsyncMock(side_effect=_slow_per_id)

        with patch("ha_mcp.tools.smart_search._deep.INDIVIDUAL_CONFIG_TIMEOUT", 0.05):
            result = await smart_tools.deep_search(
                query="anything",
                search_types=["automation"],
                limit=10,
            )

        assert result["partial"] is True, (
            f"a per-request timeout must flag partial through deep_search; "
            f"got {result.get('partial')!r}"
        )
        reason = result["partial_reason"]
        assert "timed out" in reason, (
            f"partial_reason must word the gap as a timeout; got {reason!r}"
        )
        assert "non-404" not in reason, (
            f"timeouts must no longer be misreported as 'raised a non-404 "
            f"error'; got {reason!r}"
        )
        assert "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE" in reason
        assert "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT" in reason
        # The real count (2) must flow through the seam, not a hardcode.
        assert re.search(r"\b2 automation\(s\)", reason), (
            f"partial_reason must carry the real timeout count (2); got {reason!r}"
        )


class TestSceneTimeoutClassification:
    """Scene mirror of ``TestTimeoutClassification`` (issue #1784).

    Scenes are separate code with separate tuple positions: the scene
    fetcher lives in ``_scenes.py``, its timeout count rides the 6th slot
    of ``_deep_search_scenes``'s return, and ``deep_search`` maps it into
    ``scene_stats["timeout"]`` for ``_apply_scene_partial_flag``. Without
    these tests a regression that returned ``"failed"`` from the scene
    ``except`` clause or mis-mapped the 6th slot would pass the automation/
    script suites untouched. The fixture's failing WebSocket makes the
    registry walk fail (``registry_failed=True``), which routes Attempt C
    to attempt-all — the maximal per-id-fetch surface.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # Bulk fetch + registry walk fail (triggers Tier 3, attempt-all).
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @staticmethod
    def _scene_entities() -> list[dict]:
        return [
            {
                "entity_id": "scene.slow_one",
                "state": "scening",
                "attributes": {"friendly_name": "Slow One"},
            },
            {
                "entity_id": "scene.slow_two",
                "state": "scening",
                "attributes": {"friendly_name": "Slow Two"},
            },
        ]

    @pytest.mark.asyncio
    async def test_scene_slow_fetch_classifies_as_timeout(
        self, mock_client, smart_tools
    ):
        """A real ``wait_for`` expiry in the scene fetcher must land in the
        6th tuple slot (``timeout_count``), with ``failed`` staying 0."""
        scenes = self._scene_entities()
        mock_client.get_states = AsyncMock(return_value=scenes)

        async def _slow_scene(sid: str) -> dict:
            await asyncio.sleep(0.2)
            return {"config": {"name": "slow"}}

        mock_client.get_scene_config = AsyncMock(side_effect=_slow_scene)

        with patch("ha_mcp.tools.smart_search._scenes.INDIVIDUAL_CONFIG_TIMEOUT", 0.05):
            (
                results,
                failed_count,
                skipped_count,
                _integration_skipped,
                registry_failed,
                timeout_count,
                _failed_sample,
            ) = await smart_tools._deep_search_scenes(
                scenes,
                query_lower="anything",
                exact_match=False,
            )
        assert timeout_count == 2, (
            f"both scene wait_for expiries must classify as timeout; got "
            f"timeout={timeout_count}, failed={failed_count}"
        )
        assert failed_count == 0, (
            f"scene timeouts must NOT count as generic failed; got "
            f"failed={failed_count}"
        )
        assert skipped_count == 0
        assert registry_failed is True  # fixture kills the registry walk
        assert results == []
        assert _failed_sample is None, "scene timeouts must not capture a sample"

    @pytest.mark.asyncio
    async def test_scene_timeout_surfaces_partial_through_deep_search(
        self, mock_client, smart_tools
    ):
        """Scene timeouts driven through public ``deep_search`` must reach
        ``scene_stats["timeout"]`` and ``_apply_scene_partial_flag`` — pins
        the 6th-slot mapping in the aggregator that the component test
        cannot see."""
        scenes = self._scene_entities()
        mock_client.get_states = AsyncMock(return_value=scenes)

        async def _slow_scene(sid: str) -> dict:
            await asyncio.sleep(0.2)
            return {"config": {"name": "slow"}}

        mock_client.get_scene_config = AsyncMock(side_effect=_slow_scene)

        with patch("ha_mcp.tools.smart_search._scenes.INDIVIDUAL_CONFIG_TIMEOUT", 0.05):
            result = await smart_tools.deep_search(
                query="anything",
                search_types=["scene"],
                limit=10,
            )

        assert result["partial"] is True
        reason = result["partial_reason"]
        assert "timed out" in reason, (
            f"scene partial_reason must word the gap as a timeout; got {reason!r}"
        )
        assert "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE" in reason
        assert "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT" in reason
        assert re.search(r"\b2 scene\(s\)", reason), (
            f"partial_reason must carry the real scene timeout count (2); "
            f"got {reason!r}"
        )


class TestWrappedClientTimeoutClassification:
    """Client-side (httpx-layer) timeouts must also classify as ``timeout``.

    The REST client applies its own httpx timeout (``HA_TIMEOUT``, default
    30s) and ``_raw_request`` re-raises ``httpx.TimeoutException`` as
    ``HomeAssistantConnectionError`` — a *sibling* of
    ``HomeAssistantAPIError``, so it reaches the generic ``except
    Exception`` clause. When a user raises HAMCP_INDIVIDUAL_CONFIG_TIMEOUT
    past HA_TIMEOUT (following the partial-result advice), the httpx
    timeout fires first and arrives wrapped; ``is_timeout_error``'s
    ``__cause__`` check must route it to the ``timeout`` bucket, not
    ``failed`` (#1784).
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    def test_is_timeout_error_contract(self):
        """Direct pin of the helper: builtin/httpx timeouts and one-level
        ``__cause__`` wraps are timeouts; plain connection errors are not."""
        assert is_timeout_error(TimeoutError())
        assert is_timeout_error(httpx.ReadTimeout("slow"))
        wrapped = HomeAssistantConnectionError("Request timeout: slow")
        wrapped.__cause__ = httpx.ReadTimeout("slow")
        assert is_timeout_error(wrapped)
        assert not is_timeout_error(HomeAssistantConnectionError("refused"))
        assert not is_timeout_error(ValueError("nope"))

    @pytest.mark.asyncio
    async def test_wrapped_httpx_timeout_classifies_as_timeout(
        self, mock_client, smart_tools
    ):
        """A HomeAssistantConnectionError caused by httpx.ReadTimeout (the
        exact shape ``_raw_request`` produces via ``raise ... from e``)
        must count as ``timeout``, not ``failed``."""
        automations = [
            {
                "entity_id": "automation.capped",
                "state": "on",
                "attributes": {"friendly_name": "Capped", "id": "uid_capped"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _wrapped_timeout(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            try:
                raise httpx.ReadTimeout("timed out")
            except httpx.ReadTimeout as cause:
                raise HomeAssistantConnectionError(
                    "Request timeout: timed out"
                ) from cause

        mock_client._request = AsyncMock(side_effect=_wrapped_timeout)

        (
            _matches,
            _skipped_count,
            failed_count,
            yaml_skipped_count,
            timeout_count,
            _failed_sample,
        ) = await smart_tools._deep_search_automations(
            automations,
            {"automation.capped": "uid_capped"},
            query_lower="anything",
            exact_match=False,
        )
        assert timeout_count == 1, (
            f"wrapped httpx timeout must classify as timeout; got "
            f"timeout={timeout_count}, failed={failed_count}"
        )
        assert failed_count == 0
        assert yaml_skipped_count == 0
        assert _failed_sample is None, "wrapped timeouts must not capture a sample"

    @pytest.mark.asyncio
    async def test_plain_connection_error_still_classifies_as_failed(
        self, mock_client, smart_tools
    ):
        """A non-timeout HomeAssistantConnectionError (e.g. refused) must
        stay in the ``failed`` bucket — the timeout carve-out must not
        swallow real connectivity failures."""
        automations = [
            {
                "entity_id": "automation.down",
                "state": "on",
                "attributes": {"friendly_name": "Down", "id": "uid_down"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _refused(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            raise HomeAssistantConnectionError(
                "Failed to connect to Home Assistant: refused"
            )

        mock_client._request = AsyncMock(side_effect=_refused)

        (
            _matches,
            _skipped_count,
            failed_count,
            yaml_skipped_count,
            timeout_count,
            _failed_sample,
        ) = await smart_tools._deep_search_automations(
            automations,
            {"automation.down": "uid_down"},
            query_lower="anything",
            exact_match=False,
        )
        assert failed_count == 1
        assert timeout_count == 0
        assert yaml_skipped_count == 0
        assert _failed_sample == (
            "HomeAssistantConnectionError: Failed to connect to Home Assistant: refused"
        ), "failed-class connectivity errors must capture their sample"


class TestBudgetedFetchCountArithmetic:
    """Direct ``_individual_fetch_budgeted`` coverage of the counting seams
    introduced by the ``timeout`` class (#1784)."""

    @pytest.fixture
    def smart_tools(self):
        return _make_tools(MagicMock())

    @pytest.mark.asyncio
    async def test_mixed_classes_route_to_their_own_buckets(self, smart_tools):
        """One batch containing success + yaml_skipped + failed + timeout
        must land each id in exactly one bucket."""
        kinds = {
            "ok": ({"id": "ok"}, None),
            "yaml": (None, "yaml_skipped"),
            "boom": (None, "failed"),
            "slow": (None, "timeout"),
        }

        async def fetch_one(key: str):
            config, kind = kinds[key]
            return (key, config, kind)

        (
            configs,
            failed_count,
            skipped_count,
            yaml_skipped_count,
            timeout_count,
        ) = await smart_tools._individual_fetch_budgeted(
            list(kinds), fetch_one, budget=30.0, label="Test", plural="items"
        )
        assert list(configs) == ["ok"]
        assert failed_count == 1
        assert yaml_skipped_count == 1
        assert timeout_count == 1
        assert skipped_count == 0

    @pytest.mark.asyncio
    async def test_budget_exhaustion_skipped_excludes_timed_out_ids(self, smart_tools):
        """When the budget exhausts mid-run AND completed batches contained
        timeouts, ``skipped_count`` must subtract the timeout bucket —
        dropping the ``- timeout_count`` term would double-count timed-out
        ids (once as timeout, once as skipped) and report two overlapping
        partial fragments."""

        async def fetch_one(key: str):
            await asyncio.sleep(0.05)  # push elapsed past the budget
            if key == "t1":
                return (key, None, "timeout")
            return (key, {"id": key}, None)

        with patch("ha_mcp.tools.smart_search._fetch.INDIVIDUAL_FETCH_BATCH_SIZE", 2):
            (
                configs,
                failed_count,
                skipped_count,
                yaml_skipped_count,
                timeout_count,
            ) = await smart_tools._individual_fetch_budgeted(
                ["t1", "ok1", "never1", "never2"],
                fetch_one,
                budget=0.01,
                label="Test",
                plural="items",
            )
        # Batch 1 (t1, ok1) runs; elapsed ~0.05s > 0.01s budget, so batch 2
        # never launches. skipped = 4 total - 1 fetched - 1 timeout = 2.
        assert timeout_count == 1
        assert len(configs) == 1
        assert failed_count == 0
        assert yaml_skipped_count == 0
        assert skipped_count == 2, (
            f"skipped must exclude fetched AND timed-out ids; got "
            f"skipped={skipped_count}"
        )


class TestSummarizeFetchError:
    """Unit coverage of ``summarize_fetch_error`` — the one-line summary the
    generic ``failed`` bucket attaches to ``partial_reason`` (#1784
    follow-up: the opaque "raised a non-404 error" hid a trivially-
    diagnosable per-id 500 — a ``!secret`` reference in scripts.yaml —
    behind a debug-log dive)."""

    def test_api_error_strips_client_prefix_and_names_status(self):
        # The realistic per-id 500 body: aiohttp's generic placeholder (the
        # ``!secret`` cause is HA-log-only). The client's "API error: 500 - "
        # prefix is stripped so the status isn't stated twice.
        exc = HomeAssistantAPIError(
            "API error: 500 - 500 Internal Server Error",
            status_code=500,
        )
        assert summarize_fetch_error(exc) == "HTTP 500: 500 Internal Server Error"

    def test_api_error_without_prefix_keeps_message(self):
        exc = HomeAssistantAPIError("Script not found: foo", status_code=502)
        assert summarize_fetch_error(exc) == "HTTP 502: Script not found: foo"

    def test_api_error_status_none_falls_back_to_exception_form(self):
        """``status_code=None`` means we can't claim an HTTP code — render
        as a plain exception summary instead of ``HTTP None``."""
        exc = HomeAssistantAPIError("connection reset")
        assert summarize_fetch_error(exc) == ("HomeAssistantAPIError: connection reset")

    def test_generic_exception_names_type(self):
        assert (
            summarize_fetch_error(RuntimeError("generic explosion"))
            == "RuntimeError: generic explosion"
        )

    def test_empty_message_yields_bare_type(self):
        assert summarize_fetch_error(RuntimeError()) == "RuntimeError"

    def test_multiline_body_keeps_first_line_only(self):
        exc = HomeAssistantAPIError(
            "API error: 500 - 500 Internal Server Error\n"
            "Traceback (most recent call last):\n  boom",
            status_code=500,
        )
        assert summarize_fetch_error(exc) == "HTTP 500: 500 Internal Server Error"

    def test_long_message_truncated(self):
        out = summarize_fetch_error(RuntimeError("x" * 500))
        assert len(out) == 160
        assert out.endswith("…")


class TestFailedSampleThroughDeepSearch:
    """Component + seam coverage of the representative-error sample on the
    generic ``failed`` bucket (#1784 follow-up).

    ``_deep_search_automations`` / ``_deep_search_scripts`` return the
    sample in a new 6th tuple slot (scenes: 7th), ``deep_search`` forwards
    it via ``_paginate_and_build_response`` into
    ``_apply_per_type_partial_flag`` (scenes: ``scene_stats["failed_sample"]``
    into ``_apply_scene_partial_flag``), and the failed fragment carries it
    as an ``e.g.``. Without the sample the response can't explain WHAT
    raised: the follow-up report on #1784 was a box where every per-id
    script fetch 500s on a ``!secret`` reference in scripts.yaml —
    indistinguishable from any other failure without a debug-log dive."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # Bulk fetch fails (triggers Tier 3 per-id fallback).
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @staticmethod
    def _automation_entities() -> list[dict]:
        return [
            {
                "entity_id": "automation.secret_one",
                "state": "on",
                "attributes": {"friendly_name": "Secret One", "id": "uid_one"},
            },
            {
                "entity_id": "automation.secret_two",
                "state": "on",
                "attributes": {"friendly_name": "Secret Two", "id": "uid_two"},
            },
        ]

    @pytest.mark.asyncio
    async def test_automation_500_sample_lands_in_sixth_slot(
        self, mock_client, smart_tools
    ):
        """A per-id 500 must both count as ``failed`` AND surface its
        summary in the new 6th slot."""
        automations = self._automation_entities()
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _per_id_500(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            # aiohttp's generic production 500 body — the realistic sample for
            # the ``!secret`` scenario (the cause is HA-log-only).
            raise HomeAssistantAPIError(
                "API error: 500 - 500 Internal Server Error",
                status_code=500,
            )

        mock_client._request = AsyncMock(side_effect=_per_id_500)

        (
            _matches,
            _skipped_count,
            failed_count,
            _yaml_skipped_count,
            _timeout_count,
            failed_sample,
        ) = await smart_tools._deep_search_automations(
            automations,
            {"automation.secret_one": "uid_one", "automation.secret_two": "uid_two"},
            query_lower="anything",
            exact_match=False,
        )
        assert failed_count == 2
        assert failed_sample == "HTTP 500: 500 Internal Server Error"

    @pytest.mark.asyncio
    async def test_automation_generic_exception_sample_names_type(
        self, mock_client, smart_tools
    ):
        """The generic ``except Exception`` branch captures a
        ``Type: message`` summary."""
        automations = self._automation_entities()[:1]
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _per_id_boom(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            raise RuntimeError("generic explosion")

        mock_client._request = AsyncMock(side_effect=_per_id_boom)

        (
            _matches,
            _skipped_count,
            failed_count,
            _yaml_skipped_count,
            _timeout_count,
            failed_sample,
        ) = await smart_tools._deep_search_automations(
            automations,
            {"automation.secret_one": "uid_one"},
            query_lower="anything",
            exact_match=False,
        )
        assert failed_count == 1
        assert failed_sample == "RuntimeError: generic explosion"

    @pytest.mark.asyncio
    async def test_script_generic_exception_sample_names_type(
        self, mock_client, smart_tools
    ):
        """The script closure's generic ``except Exception`` branch
        (``_deep.py`` ``_fetch_script_config``, the non-``HomeAssistantAPIError``
        non-timeout path) captures a ``Type: message`` summary. Covers the
        branch the maintainer flagged as untested: unlike the 500 path it
        exercises a bare ``RuntimeError`` raised by ``get_script_config``."""
        scripts = [
            {
                "entity_id": "script.boom_one",
                "state": "off",
                "attributes": {"friendly_name": "Boom One"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=scripts)

        async def _script_boom(sid: str) -> dict:
            raise RuntimeError("generic explosion")

        mock_client.get_script_config = AsyncMock(side_effect=_script_boom)

        (
            _matches,
            _skipped_count,
            failed_count,
            _yaml_skipped_count,
            _timeout_count,
            failed_sample,
        ) = await smart_tools._deep_search_scripts(
            scripts,
            query_lower="anything",
            exact_match=False,
        )
        assert failed_count == 1
        assert failed_sample == "RuntimeError: generic explosion"

    @pytest.mark.asyncio
    async def test_automation_404_and_timeout_produce_no_sample(
        self, mock_client, smart_tools
    ):
        """Only the generic ``failed`` class captures a sample: 404s
        (yaml_skipped) and per-request timeouts have their own dedicated
        fragments and must leave the 6th slot at ``None``."""
        automations = self._automation_entities()
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _per_id_mixed(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            if url.endswith("uid_one"):
                raise HomeAssistantAPIError(
                    "API error: 404 - Not Found", status_code=404
                )
            await asyncio.sleep(0.2)
            return {}

        mock_client._request = AsyncMock(side_effect=_per_id_mixed)

        with patch("ha_mcp.tools.smart_search._deep.INDIVIDUAL_CONFIG_TIMEOUT", 0.05):
            (
                _matches,
                _skipped_count,
                failed_count,
                yaml_skipped_count,
                timeout_count,
                failed_sample,
            ) = await smart_tools._deep_search_automations(
                automations,
                {
                    "automation.secret_one": "uid_one",
                    "automation.secret_two": "uid_two",
                },
                query_lower="anything",
                exact_match=False,
            )
        assert failed_count == 0
        assert yaml_skipped_count == 1
        assert timeout_count == 1
        assert failed_sample is None

    @pytest.mark.asyncio
    async def test_script_500_sample_surfaces_through_deep_search(
        self, mock_client, smart_tools
    ):
        """The reporter's exact scenario: every per-id script fetch 500s on
        a ``!secret`` reference. The per-id config view raises a YAMLException
        that escapes to aiohttp, whose production 500 body is the generic
        ``500 Internal Server Error`` placeholder (the ``!secret`` detail goes
        to the HA log, never the HTTP body), so the rendered sample names the
        500 — not the cause — and the static HA-log hint carries the
        diagnosis. The fragment must carry the count, the representative
        error, AND that hint through the public entrypoint."""
        scripts = [
            {
                "entity_id": "script.secret_one",
                "state": "off",
                "attributes": {"friendly_name": "Secret One"},
            },
            {
                "entity_id": "script.secret_two",
                "state": "off",
                "attributes": {"friendly_name": "Secret Two"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=scripts)

        async def _script_500(sid: str) -> dict:
            # aiohttp's generic production 500 body — what the client actually
            # sees when the per-id config view's YAMLException escapes.
            raise HomeAssistantAPIError(
                "API error: 500 - 500 Internal Server Error",
                status_code=500,
            )

        mock_client.get_script_config = AsyncMock(side_effect=_script_500)

        result = await smart_tools.deep_search(
            query="anything",
            search_types=["script"],
            limit=10,
        )

        assert result["partial"] is True
        reason = result["partial_reason"]
        assert (
            "2 script(s) not scanned (per-id fetch raised a non-404 error; "
            "e.g. HTTP 500: 500 Internal Server Error)" in reason
        ), f"failed fragment must carry the representative error; got {reason!r}"
        assert "`!secret` reference in the config file HA loads" in reason, (
            f"HTTP-500 fragment must carry the static HA-log hint; got {reason!r}"
        )

    @pytest.mark.asyncio
    async def test_scene_500_sample_surfaces_through_deep_search(
        self, mock_client, smart_tools
    ):
        """Scene mirror: the sample rides ``scene_stats["failed_sample"]``
        into ``_apply_scene_partial_flag``'s failed fragment."""
        scenes = [
            {
                "entity_id": "scene.broken_one",
                "state": "scening",
                "attributes": {"friendly_name": "Broken One"},
            },
            {
                "entity_id": "scene.broken_two",
                "state": "scening",
                "attributes": {"friendly_name": "Broken Two"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=scenes)

        async def _scene_500(sid: str) -> dict:
            # aiohttp's generic production 500 body (see the script mirror).
            raise HomeAssistantAPIError(
                "API error: 500 - 500 Internal Server Error", status_code=500
            )

        mock_client.get_scene_config = AsyncMock(side_effect=_scene_500)

        result = await smart_tools.deep_search(
            query="anything",
            search_types=["scene"],
            limit=10,
        )

        assert result["partial"] is True
        reason = result["partial_reason"]
        assert (
            "2 scene(s) not scanned (per-id fetch raised; "
            "e.g. HTTP 500: 500 Internal Server Error)" in reason
        ), f"scene failed fragment must carry the representative error; got {reason!r}"
        assert "`!secret` reference in the config file HA loads" in reason, (
            f"HTTP-500 scene fragment must carry the static HA-log hint; got {reason!r}"
        )
        # The shared hint is endpoint-agnostic: a scene 500 has nothing to do
        # with scripts.yaml/automations.yaml, so those filenames must NOT
        # appear on the scene path (Codex review P2).
        assert "scripts.yaml" not in reason and "automations.yaml" not in reason, (
            f"scene hint must not name script/automation files; got {reason!r}"
        )

    @pytest.mark.asyncio
    async def test_only_first_failure_is_summarized(self, mock_client, smart_tools):
        """``record_first_failure`` keeps the first ``failed``-class sample:
        in the motivating "every per-id fetch 500s" case the first failure IS
        an HTTP 500 (the upgrade never fires), so ``summarize_fetch_error``
        runs once, not once per failure. All failures still COUNT."""
        scripts = [
            {
                "entity_id": f"script.secret_{n}",
                "state": "off",
                "attributes": {"friendly_name": f"Secret {n}"},
            }
            for n in range(5)
        ]
        mock_client.get_states = AsyncMock(return_value=scripts)

        async def _script_500(sid: str) -> dict:
            raise HomeAssistantAPIError(
                "API error: 500 - 500 Internal Server Error", status_code=500
            )

        mock_client.get_script_config = AsyncMock(side_effect=_script_500)

        with patch(
            "ha_mcp.tools.smart_search._fetch.summarize_fetch_error",
            wraps=summarize_fetch_error,
        ) as spy:
            (
                _matches,
                _skipped_count,
                failed_count,
                _yaml_skipped_count,
                _timeout_count,
                failed_sample,
            ) = await smart_tools._deep_search_scripts(
                scripts,
                query_lower="anything",
                exact_match=False,
            )

        assert failed_count == 5, "every failure must still be counted"
        assert failed_sample == "HTTP 500: 500 Internal Server Error"
        assert spy.call_count == 1, (
            f"guard must summarize only the first failure; got {spy.call_count}"
        )

    @pytest.mark.asyncio
    async def test_automation_500_sample_surfaces_through_deep_search(
        self, mock_client, smart_tools
    ):
        """Automation mirror of the script seam test: the sample and the
        HA-log hint ride the ``automation_failed_sample`` kwarg through
        ``deep_search`` → ``_paginate_and_build_response`` →
        ``_apply_per_type_partial_flag``. A forwarding regression on that
        kwarg alone would pass the component tests."""
        automations = self._automation_entities()
        mock_client.get_states = AsyncMock(return_value=automations)

        async def _per_id_500(method: str, url: str) -> dict:
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk fetch unavailable")
            raise HomeAssistantAPIError(
                "API error: 500 - 500 Internal Server Error", status_code=500
            )

        mock_client._request = AsyncMock(side_effect=_per_id_500)

        result = await smart_tools.deep_search(
            query="anything",
            search_types=["automation"],
            limit=10,
        )

        assert result["partial"] is True
        reason = result["partial_reason"]
        assert (
            "2 automation(s) not scanned (per-id fetch raised a non-404 error; "
            "e.g. HTTP 500: 500 Internal Server Error)" in reason
        ), f"automation failed fragment must carry the sample; got {reason!r}"
        assert "`!secret` reference in the config file HA loads" in reason, (
            f"HTTP-500 automation fragment must carry the HA-log hint; got {reason!r}"
        )

    @pytest.mark.asyncio
    async def test_scene_404_sample_surfaces_without_500_hint(
        self, mock_client, smart_tools
    ):
        """Scenes have no ``yaml_skipped`` branch (integration-managed scenes
        are pre-filtered upstream via the registry walk), so a scene per-id
        404 falls into the generic ``failed`` class and surfaces as an
        ``e.g. HTTP 404`` sample — unlike automations/scripts, whose 404s
        reclassify to their own fragment and never produce one. Pins the
        asymmetry the #1930 description disclosed, so a future scene 404
        branch can't change it silently; the 500 hint must not ride a 404."""
        scenes = [
            {
                "entity_id": "scene.yaml_defined",
                "state": "scening",
                "attributes": {"friendly_name": "YAML Defined"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=scenes)

        async def _scene_404(sid: str) -> dict:
            raise HomeAssistantAPIError("API error: 404 - Not Found", status_code=404)

        mock_client.get_scene_config = AsyncMock(side_effect=_scene_404)

        result = await smart_tools.deep_search(
            query="anything",
            search_types=["scene"],
            limit=10,
        )

        assert result["partial"] is True
        reason = result["partial_reason"]
        assert (
            "1 scene(s) not scanned (per-id fetch raised; "
            "e.g. HTTP 404: Not Found)" in reason
        ), f"scene 404 must surface as a failed-class sample; got {reason!r}"
        assert "in the Home Assistant log" not in reason, (
            f"the 500 hint must not ride a 404 sample; got {reason!r}"
        )

    @pytest.mark.asyncio
    async def test_500_sample_preferred_over_faster_non_500(
        self, mock_client, smart_tools
    ):
        """Mixed failures: failure order under ``asyncio.gather`` tracks
        latency, so a fast-failing non-500 outlier would otherwise claim the
        sample slot and suppress the 500 diagnosis hint. The 500 must own the
        sample regardless of which failure lands first."""
        scripts = [
            {
                "entity_id": "script.blip_one",
                "state": "off",
                "attributes": {"friendly_name": "Blip One"},
            },
            {
                "entity_id": "script.secret_two",
                "state": "off",
                "attributes": {"friendly_name": "Secret Two"},
            },
        ]
        mock_client.get_states = AsyncMock(return_value=scripts)

        async def _mixed_failure(sid: str) -> dict:
            if "blip" in sid:
                raise RuntimeError("fast connection blip")
            raise HomeAssistantAPIError(
                "API error: 500 - 500 Internal Server Error", status_code=500
            )

        mock_client.get_script_config = AsyncMock(side_effect=_mixed_failure)

        (
            _matches,
            _skipped_count,
            failed_count,
            _yaml_skipped_count,
            _timeout_count,
            failed_sample,
        ) = await smart_tools._deep_search_scripts(
            scripts,
            query_lower="anything",
            exact_match=False,
        )
        assert failed_count == 2, "both failures must still be counted"
        assert failed_sample == "HTTP 500: 500 Internal Server Error", (
            f"the HTTP 500 must own the sample slot; got {failed_sample!r}"
        )


class TestFailedSamplePreference:
    """``record_first_failure`` selection semantics: first failure wins the
    sample slot, except the first HTTP 500 upgrades a non-500 sample, once.
    Failure order under ``asyncio.gather`` tracks latency, so a fast-failing
    outlier (e.g. a connection blip) would otherwise claim the slot and
    suppress the ``http_500_diagnosis_hint`` diagnosis — the case the sample
    exists for (#1784 follow-up)."""

    @staticmethod
    def _http_500(body: str = "500 Internal Server Error") -> HomeAssistantAPIError:
        return HomeAssistantAPIError(f"API error: 500 - {body}", status_code=500)

    def test_first_failure_wins_absent_a_500(self):
        errs: list[str] = []
        record_first_failure(errs, RuntimeError("first"))
        record_first_failure(errs, ValueError("second"))
        assert errs == ["RuntimeError: first"]

    def test_first_http_500_upgrades_non_500_sample_once(self):
        errs: list[str] = []
        record_first_failure(errs, RuntimeError("fast blip"))
        record_first_failure(errs, self._http_500())
        assert errs == ["HTTP 500: 500 Internal Server Error"]
        # Later 500s and non-500s never replace the upgraded sample.
        record_first_failure(errs, self._http_500("a different body"))
        record_first_failure(errs, RuntimeError("late blip"))
        assert errs == ["HTTP 500: 500 Internal Server Error"]

    def test_non_500_http_error_does_not_upgrade(self):
        errs: list[str] = []
        record_first_failure(errs, RuntimeError("fast blip"))
        record_first_failure(
            errs,
            HomeAssistantAPIError("API error: 502 - Bad Gateway", status_code=502),
        )
        assert errs == ["RuntimeError: fast blip"]

    def test_at_most_two_summaries_per_pass(self):
        """One capture + one upgrade = two renders, no matter how many
        failures follow — preserves the N-1 economy the guard bought."""
        errs: list[str] = []
        with patch(
            "ha_mcp.tools.smart_search._fetch.summarize_fetch_error",
            wraps=summarize_fetch_error,
        ) as spy:
            record_first_failure(errs, RuntimeError("fast blip"))
            for _ in range(3):
                record_first_failure(errs, self._http_500())
            record_first_failure(errs, ValueError("late"))
        assert errs == ["HTTP 500: 500 Internal Server Error"]
        assert spy.call_count == 2, (
            f"expected one capture + one upgrade; got {spy.call_count}"
        )
