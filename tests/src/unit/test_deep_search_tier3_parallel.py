"""Unit tests for parallel Attempt C config fetching in deep_search.

Validates that when bulk config fetches fail (Attempts A & B), Attempt C
fetches configs in parallel batches without name-score prioritization. This
ensures entities referenced only inside automation/script conditions/actions
(not in the name) are still found. Regression test for #879.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.smart_search import SmartSearchTools


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
    directly and assert on their returned 4-tuple, pinning the
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
    4-tuple; these tests pin the wiring *between* that return and the
    response — ``deep_search`` unpacks the 4th slot (``_deep.py`` :130-133
    / :151-154) and forwards it (:225-231) to ``_paginate_and_build_response``
    → ``_apply_per_type_partial_flag``. A regression that unpacked the slot
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
