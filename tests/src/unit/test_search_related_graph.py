"""Unit tests for the ``search/related`` reference-graph merge in deep_search.

Discussion #2258: on a large install the per-id config fetch is serialized by
Home Assistant (the endpoint takes a lock and re-parses the whole YAML per
request), so the wall-clock budget expires with a random slice of automations
never scanned. Worse, a YAML-defined automation returns 404 from
``/config/automation/config/<id>`` at ANY budget, so the legacy path can never
read it.

For an entity_id-shaped query, Home Assistant's own ``search/related`` command
answers "which automations/scripts/scenes reference this entity" from its
in-memory graph in one frame. These tests pin that the graph result is merged
into the config buckets so those two blind spots stop producing false negatives,
and that the merge never degrades what already worked.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.smart_search._graph import is_entity_id_shaped

QUERY = "climate.master_4"


def _make_tools(client):
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


def _automation(slug: str, name: str, uid: str) -> dict:
    return {
        "entity_id": f"automation.{slug}",
        "state": "on",
        "attributes": {"friendly_name": name, "id": uid},
    }


def _related_response(**buckets: list[str]) -> dict:
    """Shape ``send_websocket_message`` returns for a successful WS command."""
    return {"success": True, "result": dict(buckets)}


def _bucket_entity_ids(response: dict, bucket: str) -> list[str]:
    return [r.get("entity_id") for r in response.get(bucket, [])]


def _record(response: dict, bucket: str, entity_id: str) -> dict | None:
    for rec in response.get(bucket, []):
        if rec.get("entity_id") == entity_id:
            return rec
    return None


class TestEntityIdShapeDetection:
    """The graph only fires for a query that is an entity_id, per HA's slug rule."""

    @pytest.mark.parametrize(
        "value",
        [
            "climate.master_4",
            "sensor.kitchen_temp",
            "binary_sensor.kitchen_door",
        ],
    )
    def test_accepts_valid_entity_ids(self, value):
        assert is_entity_id_shaped(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "kitchen_temp",  # no domain separator: a free-text term
            "climate",
            "Climate.Master_4",  # uppercase is not a slug
            "climate..master",
            "cli__mate.master",  # a double underscore is invalid anywhere,
            "binary_sensor.a__b",  # ...object_id included (HA anchors the guard at the start)
            "climate._master",  # object_id may not start with an underscore
            "climate.master_",  # ...nor end with one
            "climate.master.4",
            "",
        ],
    )
    def test_rejects_everything_else(self, value):
        assert is_entity_id_shaped(value) is False


class TestGraphMerge:
    """Automations HA knows reference the entity are reported even when unreadable."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.send_websocket_message = AsyncMock(
            return_value=_related_response(automation=["automation.morning_routine"])
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_graph_hit_reported_when_its_config_is_never_readable(
        self, mock_client, smart_tools
    ):
        """A referencing automation whose config cannot be fetched is still found.

        Without the graph merge the user asks "which automations use
        climate.master_4", every config fetch fails, and the answer is an
        empty list that reads as "nothing uses it" -- the false negative that
        makes a rename look safe when it is not.
        """
        automations = [
            _automation("morning_routine", "Morning Routine", "uid_morning"),
            _automation("evening_lights", "Evening Lights", "uid_evening"),
        ]
        mock_client.get_states = AsyncMock(return_value=automations)
        mock_client._request = AsyncMock(side_effect=Exception("fetch unavailable"))

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        assert _bucket_entity_ids(response, "automations") == [
            "automation.morning_routine"
        ]
        record = _record(response, "automations", "automation.morning_routine")
        assert record["match_in_references"] is True
        assert record["match_in_config"] is False

    @pytest.mark.asyncio
    async def test_yaml_defined_automation_is_found(self, mock_client, smart_tools):
        """A YAML automation (per-id 404) referencing the entity is reported.

        The per-id config endpoint only exposes UI-storage automations, so a
        YAML-defined one is invisible to config-body search at any budget.
        Before the graph merge it could never be found at all.
        """
        mock_client.get_states = AsyncMock(
            return_value=[
                _automation("morning_routine", "Morning Routine", "uid_morning")
            ]
        )
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Resource not found", 404
            )
        )

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        assert "automation.morning_routine" in _bucket_entity_ids(
            response, "automations"
        )

    @pytest.mark.asyncio
    async def test_graph_and_body_hit_collapse_into_one_record(
        self, mock_client, smart_tools
    ):
        """An automation found both ways is one record carrying both flags.

        Reporting it twice would double-count it in total_matches and make an
        agent think two different automations reference the entity.
        """
        mock_client.get_states = AsyncMock(
            return_value=[
                _automation("morning_routine", "Morning Routine", "uid_morning")
            ]
        )

        async def _request(method: str, url: str):
            return {
                "id": "uid_morning",
                "trigger": [{"platform": "state", "entity_id": QUERY}],
            }

        mock_client._request = AsyncMock(side_effect=_request)

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        assert _bucket_entity_ids(response, "automations") == [
            "automation.morning_routine"
        ]
        record = _record(response, "automations", "automation.morning_routine")
        assert record["match_in_references"] is True
        assert record["match_in_config"] is True

    @pytest.mark.asyncio
    async def test_graph_failure_leaves_the_search_working(
        self, mock_client, smart_tools
    ):
        """A dead transport must not break the body scan.

        The WebSocket can be down while Home Assistant's REST API is fine. The
        user must still get the config-body answer rather than an error.
        (A rejected-command reply is a different branch, covered by
        ``TestUnsupportedInstance``.)
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("Unknown command.")
        )
        mock_client.get_states = AsyncMock(
            return_value=[
                _automation("morning_routine", "Morning Routine", "uid_morning")
            ]
        )

        async def _request(method: str, url: str):
            return {
                "id": "uid_morning",
                "trigger": [{"platform": "state", "entity_id": QUERY}],
            }

        mock_client._request = AsyncMock(side_effect=_request)

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        record = _record(response, "automations", "automation.morning_routine")
        assert record is not None
        assert record["match_in_config"] is True
        assert record["match_in_references"] is False

    @pytest.mark.asyncio
    async def test_free_text_query_never_calls_the_graph(
        self, mock_client, smart_tools
    ):
        """A non-entity_id query must not spend a WebSocket frame on the graph.

        ``search/related`` only accepts an item id; sending it a free-text term
        would be a guaranteed-useless round trip on every ordinary search.
        """
        mock_client.get_states = AsyncMock(
            return_value=[
                _automation("morning_routine", "Morning Routine", "uid_morning")
            ]
        )
        mock_client._request = AsyncMock(side_effect=Exception("fetch unavailable"))

        await smart_tools.deep_search(
            query="kitchen_temp", search_types=["automation"], limit=20
        )

        sent_types = [
            call.args[0].get("type")
            for call in mock_client.send_websocket_message.call_args_list
            if call.args
        ]
        assert "search/related" not in sent_types


class TestGraphDeprioritization:
    """The fetch budget goes to configs whose reference status is still unknown."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.send_websocket_message = AsyncMock(
            return_value=_related_response(automation=["automation.flagged"])
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_unflagged_configs_are_fetched_before_flagged_ones(
        self, mock_client, smart_tools
    ):
        """Configs HA already confirmed as referencing the entity are fetched last.

        A flagged automation is already a confirmed match, so fetching its body
        teaches nothing about match status. An unflagged one may hide a
        template-only reference the graph cannot see, so under budget pressure
        it is the one that must be read. Fetching flagged bodies first would
        starve exactly the configs deep_search exists to scan (#879).
        """
        automations = [
            _automation("flagged", "Flagged", "uid_flagged"),
            _automation("unflagged_a", "Unflagged A", "uid_a"),
            _automation("unflagged_b", "Unflagged B", "uid_b"),
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        fetched: list[str] = []

        async def _request(method: str, url: str):
            uid = url.rsplit("/", maxsplit=1)[-1]
            fetched.append(uid)
            return {"id": uid}

        mock_client._request = AsyncMock(side_effect=_request)

        with patch("ha_mcp.tools.smart_search._fetch.INDIVIDUAL_FETCH_BATCH_SIZE", 1):
            await smart_tools.deep_search(
                query=QUERY, search_types=["automation"], limit=20
            )

        assert fetched.index("uid_flagged") > fetched.index("uid_a")
        assert fetched.index("uid_flagged") > fetched.index("uid_b")

    @pytest.mark.asyncio
    async def test_a_budget_for_one_fetch_spends_it_on_the_unflagged_config(
        self, mock_client, smart_tools
    ):
        """When the budget affords ONE config read, the unflagged one is read.

        HA's graph skips templated entity ids, so a config-body scan is the
        only thing that can find ``{{ states('climate.master_4') }}``. The
        flagged automation is already a confirmed match without its body, so
        reading it instead would burn the entire budget learning nothing and
        lose the templated reference outright -- #879's defect, re-created.
        """
        automations = [
            _automation("flagged", "Flagged", "uid_flagged"),
            _automation("templated", "Templated", "uid_templated"),
        ]
        mock_client.get_states = AsyncMock(return_value=automations)

        fetched: list[str] = []

        async def _request(method: str, url: str):
            uid = url.rsplit("/", maxsplit=1)[-1]
            fetched.append(uid)
            if uid == "uid_templated":
                return {
                    "id": uid,
                    "condition": [
                        {
                            "condition": "template",
                            "value_template": "{{ states('" + QUERY + "') > 20 }}",
                        }
                    ],
                }
            return {"id": uid}

        mock_client._request = AsyncMock(side_effect=_request)

        # Batch size 1 plus an injected clock, so "the budget affords exactly
        # one fetch" is exact rather than a race: the budget check reads 0s on
        # the first batch and 10s (past the 5s budget) on every one after.
        # Scoped to _fetch's own module reference so no real timing is touched.
        readings = iter([0.0, 0.0])

        class _FakeClock:
            @staticmethod
            def perf_counter() -> float:
                return next(readings, 10.0)

        with (
            patch("ha_mcp.tools.smart_search._fetch.INDIVIDUAL_FETCH_BATCH_SIZE", 1),
            patch("ha_mcp.tools.smart_search._fetch.time", _FakeClock),
        ):
            response = await smart_tools.deep_search(
                query=QUERY,
                search_types=["automation"],
                limit=20,
                config_time_budget=5,
            )

        assert fetched == ["uid_templated"]
        record = _record(response, "automations", "automation.templated")
        assert record is not None, "the templated reference was lost"
        assert record["match_in_config"] is True
        assert record["match_in_references"] is False


class TestGraphHonesty:
    """What the response claims about completeness must match what it did.

    This tool is used to decide whether renaming or deleting an entity is
    safe, so an answer that reads as "nothing else references this" when the
    graph was skipped, unreadable, or filtered is the worst failure available.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(
            return_value=[_automation("morning_routine", "Morning Routine", "uid_m")]
        )
        # Every config unreadable, so the body scan is always incomplete and
        # the scope sentence is eligible to appear.
        client._request = AsyncMock(side_effect=Exception("fetch unavailable"))
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @staticmethod
    def _reason(response: dict) -> str:
        return response.get("partial_reason") or ""

    @pytest.mark.asyncio
    async def test_no_completeness_claim_when_the_graph_could_not_be_reached(
        self, mock_client, smart_tools
    ):
        """A dead graph must not leave the response claiming full coverage.

        Without this, a WebSocket outage silently downgrades the answer to a
        body-scan-only result while the prose still says every plain reference
        was reported.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("socket down")
        )

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        reason = self._reason(response)
        assert "reference graph was consulted" not in reason
        assert "could not be consulted" in reason

    @pytest.mark.asyncio
    async def test_unreadable_graph_answer_is_not_treated_as_no_references(
        self, mock_client, smart_tools
    ):
        """A reply whose shape we cannot parse must not become 'nothing matched'.

        An empty success and an unreadable answer are opposite evidence. If
        HA's payload shape ever changes, collapsing the two would assert full
        coverage on the basis of zero understood bytes.
        """
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"frobnicate": ["x"]}}
        )

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        reason = self._reason(response)
        assert "could not be consulted" in reason
        assert "reference graph was consulted" not in reason

    @pytest.mark.asyncio
    async def test_empty_graph_answer_does_license_the_scope_claim(
        self, mock_client, smart_tools
    ):
        """HA answering "nothing references this" is real evidence, unlike a failure.

        The mirror of the two tests above: if an empty answer were also treated
        as unusable, the scope sentence would never appear and the feature
        would tell the user nothing on the common clean case.
        """
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {}}
        )

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        assert "reference graph was consulted" in self._reason(response)

    @pytest.mark.asyncio
    async def test_the_scope_claim_is_bounded_by_visibility(
        self, mock_client, smart_tools
    ):
        """The completeness sentence must not vouch for concealed records.

        The visibility scrub runs after the graph merge and can drop a record
        this sentence would otherwise say is counted. Without the caveat the
        response asserts a complete reference list while silently withholding
        part of it.
        """
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {}}
        )

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        scope = self._reason(response)
        assert "reference graph was consulted" in scope
        assert "visibility" in scope, (
            "the scope claim does not bound itself by what the caller can see"
        )

    @pytest.mark.asyncio
    async def test_consumers_ha_reports_but_we_do_not_model_are_disclosed(
        self, mock_client, smart_tools
    ):
        """A group containing the entity breaks on rename and must be disclosed.

        ha_search has no group/person match shape, but discarding them AND
        claiming the reference list is complete turns a scoping decision into a
        false negative. Disclosed as a COUNT, never as ids: under enforce mode
        the outbound scan refuses any response carrying a hidden entity_id, so
        naming them would fail the whole search and leak what that mode exists
        to conceal.
        """
        mock_client.send_websocket_message = AsyncMock(
            return_value=_related_response(
                automation=["automation.morning_routine"],
                group=["group.downstairs"],
            )
        )

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        reason = self._reason(response)
        assert "1 group(s)" in reason
        assert "group.downstairs" not in reason, (
            "a concealed-surface id was named in partial_reason"
        )

    @pytest.mark.asyncio
    async def test_surfaces_excluded_by_search_types_are_disclosed(
        self, mock_client, smart_tools
    ):
        """References on a surface the caller filtered out must still be named.

        The caller narrowed the search; they did not ask to be told the answer
        is exhaustive while known hits are withheld.
        """
        mock_client.send_websocket_message = AsyncMock(
            return_value=_related_response(
                automation=["automation.morning_routine"],
                scene=["scene.movie_night"],
            )
        )

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["automation"], limit=20
        )

        assert "scenes" in self._reason(response)


class TestGraphHitsRespectVisibility:
    """Reference-graph hits must not become a way around the visibility filter."""

    @pytest.mark.asyncio
    async def test_a_hidden_automation_named_by_the_graph_is_still_concealed(self):
        """Enforce mode must strip a graph hit exactly as it strips a body hit.

        The graph names an automation without reading its config, so a graph
        record is built from data the config-body scrub never saw. If the merge
        ever moved after the scrub, these records would sail past the filter
        and hand the agent the entity_id of an automation the operator
        concealed -- the one thing enforce mode exists to prevent.
        """
        import re

        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(
            return_value=[
                _automation("security_arm", "Security Arm", "uid_sec"),
                _automation("morning_routine", "Morning Routine", "uid_m"),
            ]
        )
        client._request = AsyncMock(side_effect=Exception("fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            return_value=_related_response(
                automation=["automation.security_arm", "automation.morning_routine"]
            )
        )
        tools = _make_tools(client)

        hidden = re.compile(r"automation\.security_arm")
        with patch(
            "ha_mcp.visibility.enforcement.active_hidden_regex",
            AsyncMock(return_value=hidden),
        ):
            response = await tools.deep_search(
                query=QUERY, search_types=["automation"], limit=20
            )

        found = _bucket_entity_ids(response, "automations")
        assert "automation.security_arm" not in found, (
            "a concealed automation reached the caller through a graph hit"
        )
        assert "automation.morning_routine" in found


class TestNonAutomationGraphRecords:
    """The scene and script record builders, which the automation path skips."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client._request = AsyncMock(side_effect=Exception("fetch unavailable"))
        client.get_script_config = AsyncMock(side_effect=Exception("unavailable"))
        client.get_scene_config = AsyncMock(side_effect=Exception("unavailable"))
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_graph_scene_hit_carries_its_storage_key_not_its_slug(
        self, mock_client, smart_tools
    ):
        """A renamed scene's scene_id must be the key its tools index on.

        HA derives a scene's entity_id from its name and never re-keys storage,
        so for a renamed scene the slug is not the storage key. Handing the
        slug to ha_config_get_scene or ha_config_delete_scene finds the wrong
        scene or none at all, and a graph hit is exactly the case where no
        config body was read to correct it.
        """
        mock_client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "scene.led_desk_strip_night_light",
                    "state": "scening",
                    "attributes": {"friendly_name": "LED Desk Strip Night Light"},
                }
            ]
        )
        mock_client.send_websocket_message = AsyncMock(
            side_effect=lambda message: (
                {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "scene.led_desk_strip_night_light",
                            "unique_id": "night_light_led_desk_strip",
                            "platform": "homeassistant",
                        }
                    ],
                }
                if message.get("type") == "config/entity_registry/list"
                else _related_response(scene=["scene.led_desk_strip_night_light"])
            )
        )

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["scene"], limit=20
        )

        record = _record(response, "scenes", "scene.led_desk_strip_night_light")
        assert record is not None, "graph scene hit was not reported"
        assert record["match_in_references"] is True
        assert record["scene_id"] == "night_light_led_desk_strip"

    @pytest.mark.asyncio
    async def test_graph_script_hit_carries_its_script_id(
        self, mock_client, smart_tools
    ):
        """A script graph hit needs the id ha_config_get_script takes.

        Without it the agent has an entity_id and no way to open the script it
        was just told references the entity.
        """
        mock_client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "script.good_night",
                    "state": "off",
                    "attributes": {"friendly_name": "Good Night"},
                }
            ]
        )
        mock_client.send_websocket_message = AsyncMock(
            return_value=_related_response(script=["script.good_night"])
        )

        response = await smart_tools.deep_search(
            query=QUERY, search_types=["script"], limit=20
        )

        record = _record(response, "scripts", "script.good_night")
        assert record is not None, "graph script hit was not reported"
        assert record["script_id"] == "good_night"

    @pytest.mark.asyncio
    async def test_graph_confirmed_scripts_are_fetched_last(
        self, mock_client, smart_tools
    ):
        """The script side keys deprioritization differently from automations.

        Scripts key on the entity slug, automations on a registry unique_id.
        A wrong key here does not raise: unmatched ids are simply never
        deprioritized, so the budget silently goes back to re-reading configs
        whose match is already settled.
        """
        mock_client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "script.flagged",
                    "state": "off",
                    "attributes": {"friendly_name": "Flagged"},
                },
                {
                    "entity_id": "script.unflagged",
                    "state": "off",
                    "attributes": {"friendly_name": "Unflagged"},
                },
            ]
        )
        mock_client.send_websocket_message = AsyncMock(
            return_value=_related_response(script=["script.flagged"])
        )

        fetched: list[str] = []

        async def _get_script_config(script_id: str) -> dict:
            fetched.append(script_id)
            return {"config": {"alias": script_id}}

        mock_client.get_script_config = AsyncMock(side_effect=_get_script_config)

        with patch("ha_mcp.tools.smart_search._fetch.INDIVIDUAL_FETCH_BATCH_SIZE", 1):
            await smart_tools.deep_search(
                query=QUERY, search_types=["script"], limit=20
            )

        assert fetched.index("flagged") > fetched.index("unflagged")


class TestUnsupportedInstance:
    """Suppression of a rejected search/related, and its blast radius."""

    @staticmethod
    def _rejection() -> dict:
        return {
            "success": False,
            "error": "Command failed: Unknown command.",
            "error_code": "unknown_command",
        }

    @pytest.mark.asyncio
    async def test_one_rejection_does_not_disable_the_graph(self):
        """A single unknown_command may just mean HA is still starting.

        HA registers search/related during setup but accepts WebSocket clients
        before every integration has loaded, and the most safety-critical
        moment to consult the graph is right after a restart. Latching off the
        first rejection would silently skip the graph for the whole window.
        """
        from ha_mcp.tools.smart_search import _graph

        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value=self._rejection())
        _graph.reset_unsupported_cache(client)

        assert await _graph.fetch_related_buckets(client, QUERY) is None
        assert await _graph.fetch_related_buckets(client, QUERY) is None
        assert client.send_websocket_message.await_count == 2
        _graph.reset_unsupported_cache(client)

    @pytest.mark.asyncio
    async def test_repeated_rejection_stops_the_frames(self):
        """Confirmed-unsupported installs must stop paying a rejected frame.

        Each rejection makes the transport log an ERROR, so retrying forever
        reproduces the log-spam regression of #1889.
        """
        from ha_mcp.tools.smart_search import _graph

        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value=self._rejection())
        _graph.reset_unsupported_cache(client)

        for _ in range(3):
            await _graph.fetch_related_buckets(client, QUERY)

        assert client.send_websocket_message.await_count == 2
        _graph.reset_unsupported_cache(client)

    @pytest.mark.asyncio
    async def test_a_success_clears_the_strike_count(self):
        """One blip then a good answer must not accumulate toward suppression.

        Otherwise two unrelated hiccups hours apart would latch the graph off.
        """
        from ha_mcp.tools.smart_search import _graph

        client = MagicMock()
        _graph.reset_unsupported_cache(client)
        client.send_websocket_message = AsyncMock(
            side_effect=[
                self._rejection(),
                {"success": True, "result": {}},
                self._rejection(),
                self._rejection(),
            ]
        )

        for _ in range(4):
            await _graph.fetch_related_buckets(client, QUERY)

        assert client.send_websocket_message.await_count == 4
        _graph.reset_unsupported_cache(client)


class TestTransientGraphFailures:
    """A momentary error must not be mistaken for an unsupported install."""

    @pytest.mark.asyncio
    async def test_transient_errors_never_arm_suppression(self):
        """Two unrelated blips must not disable the graph for five minutes.

        Only a rejection meaning "this command does not exist" is evidence
        about the instance. Counting timeouts toward the same threshold would
        silently drop dependency discovery after any two hiccups.
        """
        from ha_mcp.tools.smart_search import _graph

        client = MagicMock()
        _graph.reset_unsupported_cache(client)
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "Timeout waiting for response"}
        )

        for _ in range(3):
            assert await _graph.fetch_related_buckets(client, QUERY) is None

        assert client.send_websocket_message.await_count == 3
        _graph.reset_unsupported_cache(client)

    @pytest.mark.asyncio
    async def test_rejection_without_a_structured_code_still_arms(self):
        """Older or proxied replies carry the message but no error_code.

        If only the structured code were honoured, such an instance would pay
        a rejected frame, and its ERROR log line, on every single search.
        """
        from ha_mcp.tools.smart_search import _graph

        client = MagicMock()
        _graph.reset_unsupported_cache(client)
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "Command failed: Unknown command."}
        )

        for _ in range(3):
            await _graph.fetch_related_buckets(client, QUERY)

        assert client.send_websocket_message.await_count == 2
        _graph.reset_unsupported_cache(client)

    @pytest.mark.asyncio
    async def test_suppression_lifts_once_its_window_expires(self):
        """An install that gains the search integration must recover on its own.

        The cache is keyed by a client that outlives HA restarts and upgrades,
        so without expiry a suppression armed before an upgrade would outlast
        the condition that caused it.
        """
        from ha_mcp.tools.smart_search import _graph

        client = MagicMock()
        _graph.reset_unsupported_cache(client)
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: Unknown command.",
                "error_code": "unknown_command",
            }
        )

        now = [1000.0]
        with patch.object(_graph.time, "monotonic", lambda: now[0]):
            for _ in range(3):
                await _graph.fetch_related_buckets(client, QUERY)
            assert client.send_websocket_message.await_count == 2
            now[0] += _graph._UNSUPPORTED_TTL_S + 1
            await _graph.fetch_related_buckets(client, QUERY)

        assert client.send_websocket_message.await_count == 3
        _graph.reset_unsupported_cache(client)


class TestGraphApplicability:
    """ "Not applicable" and "tried and failed" are different answers."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(return_value=[])
        client._request = AsyncMock(return_value=[])
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {}}
        )
        return client

    @pytest.mark.asyncio
    async def test_a_search_of_only_non_graph_surfaces_is_not_partial(
        self, mock_client
    ):
        """Skipping a frame the graph could not have answered is not a loss.

        The graph speaks only to automations, scripts and scenes, so a helper-
        or dashboard-only search never sends one. Reporting that as "the
        reference graph could not be consulted" marks a complete search
        ``partial``, which trains an agent to distrust a result that is in fact
        exhaustive -- and to re-run it, paying the whole scan again.
        """
        tools = _make_tools(mock_client)

        response = await tools.deep_search(
            query=QUERY, search_types=["helper"], limit=10
        )

        assert "could not be consulted" not in (response.get("partial_reason") or "")
        assert not response.get("partial"), (
            f"complete search reported partial: {response.get('partial_reason')}"
        )


class TestGraphAnswerGuards:
    """Two ways a graph answer can be wrong without being an error."""

    @pytest.mark.asyncio
    async def test_a_bucket_of_unreadable_elements_is_not_a_successful_empty(self):
        """Elements we cannot read must not pass as "no references here".

        If HA (or a proxy) ever changed the payload to objects rather than id
        strings, filtering them out while reporting success would let the
        response claim every plain reference was counted, on the strength of a
        bucket it had entirely discarded.
        """
        from ha_mcp.tools.smart_search import _graph

        client = MagicMock()
        _graph.reset_unsupported_cache(client)
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"automation": [{"entity_id": "automation.foo"}]},
            }
        )

        assert await _graph.fetch_related_buckets(client, QUERY) is None
        _graph.reset_unsupported_cache(client)

    @pytest.mark.asyncio
    async def test_an_empty_bucket_is_still_a_readable_answer(self):
        """The mirror: HA legitimately reporting an empty list is not a failure.

        Without this, tightening the guard above would turn every clean search
        into "the graph could not be consulted".
        """
        from ha_mcp.tools.smart_search import _graph

        client = MagicMock()
        _graph.reset_unsupported_cache(client)
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"automation": []}}
        )

        result = await _graph.fetch_related_buckets(client, QUERY)
        assert result is not None
        assert result.buckets == {}
        _graph.reset_unsupported_cache(client)

    @pytest.mark.asyncio
    async def test_the_graph_lookup_is_bounded_by_its_own_short_timeout(self):
        """An optional lookup must not stall the scan it exists to improve.

        It is awaited before any config fetch starts, so inheriting the client's
        30s command default would hold every entity_id search behind an
        unresponsive graph, potentially past the caller's own tool timeout.
        """
        from ha_mcp.tools.smart_search import _graph

        client = MagicMock()
        _graph.reset_unsupported_cache(client)
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {}}
        )

        await _graph.fetch_related_buckets(client, QUERY)

        sent = client.send_websocket_message.await_args.args[0]
        assert sent["_wait_timeout"] == _graph._GRAPH_TIMEOUT_S
        assert sent["_wait_timeout"] < 30, "must be shorter than the client default"
        _graph.reset_unsupported_cache(client)


class TestSceneOnlySearchGetsTheScopeSentence:
    """Scenes are a graph bucket, so their incompleteness must reach the gate."""

    @pytest.mark.asyncio
    async def test_a_scene_only_search_with_failed_configs_still_scopes_the_unknown(
        self,
    ):
        """A failed scene scan must still say what the graph accounted for.

        The scope sentence was gated on automation and script counters only, so
        a scene-only search whose configs all failed reported "scenes not
        scanned" with no statement that every plain reference was covered
        anyway. The caller is left assuming the reference list is as broken as
        the scan.
        """
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "scene.movie_night",
                    "state": "scening",
                    "attributes": {"friendly_name": "Movie Night"},
                }
            ]
        )
        client.get_scene_config = AsyncMock(side_effect=RuntimeError("REST 500"))
        client.send_websocket_message = AsyncMock(
            side_effect=lambda message: (
                {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "scene.movie_night",
                            "unique_id": "movie_night",
                            "platform": "homeassistant",
                        }
                    ],
                }
                if message.get("type") == "config/entity_registry/list"
                else _related_response(scene=["scene.movie_night"])
            )
        )
        tools = _make_tools(client)

        response = await tools.deep_search(
            query=QUERY, search_types=["scene"], limit=20
        )

        reason = response.get("partial_reason") or ""
        assert "reference graph was consulted" in reason, (
            f"scene incompleteness did not reach the scope gate: {reason!r}"
        )
