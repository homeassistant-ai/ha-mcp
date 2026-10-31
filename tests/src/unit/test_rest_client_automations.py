"""Unit tests for REST client automation-related methods.

These tests verify error handling for automation configuration operations,
especially the 405 Method Not Allowed error for addon proxy limitations.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)


class TestDeleteAutomationConfig:
    """Tests for delete_automation_config error handling."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock HomeAssistantClient for testing."""
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            return client

    @pytest.mark.asyncio
    async def test_delete_automation_success(self, mock_client):
        """Successful automation deletion should return success response."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")

        result = await mock_client.delete_automation_config(
            "automation.test_automation"
        )

        assert result["identifier"] == "automation.test_automation"
        assert result["unique_id"] == "test_unique_id"
        assert result["operation"] == "deleted"
        mock_client._request.assert_called_once_with(
            "DELETE", "/config/automation/config/test_unique_id"
        )

    @pytest.mark.asyncio
    async def test_deleting_a_missing_automation_reports_not_found(self, mock_client):
        """Core answers a DELETE of a missing id with 400 "Resource not found";
        reported as a 400 the caller reads it as a malformed request."""
        mock_client._resolve_automation_id = AsyncMock(return_value="nonexistent_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 400 - Resource not found",
                status_code=400,
                response_data={"message": "Resource not found"},
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.nonexistent")

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_rejection_other_than_not_found_stays_a_400(self, mock_client):
        """Only Core's not-found text means the automation is missing."""
        mock_client._resolve_automation_id = AsyncMock(return_value="some_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 400 - Key malformed",
                status_code=400,
                response_data={"message": "Key malformed: expected str"},
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("some_id")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_automation_405_addon_proxy_limitation(self, mock_client):
        """405 error should raise HomeAssistantAPIError with helpful message.

        This tests the fix for issue #414 where automations cannot be deleted
        via the API when running ha-mcp as a Home Assistant add-on because
        the Supervisor ingress proxy blocks DELETE HTTP method.
        """
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        error = exc_info.value
        assert error.status_code == 405

        # Verify the error message is helpful
        error_message = str(error)
        assert "cannot delete" in error_message.lower()

        # Verify it mentions the addon proxy limitation
        assert "add-on" in error_message.lower()
        assert "supervisor" in error_message.lower()
        assert "delete" in error_message.lower()

        # Verify it provides workarounds
        assert "workaround" in error_message.lower()
        assert "pip" in error_message.lower() or "docker" in error_message.lower()
        assert "delete_" in error_message.lower()  # Prefix suggestion
        assert "home assistant ui" in error_message.lower()

    @pytest.mark.asyncio
    async def test_delete_automation_other_error_propagates(self, mock_client):
        """Other API errors should propagate unchanged."""
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 500 - Internal Server Error",
                status_code=500,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_delete_automation_generic_exception_propagates(self, mock_client):
        """Non-API exceptions should propagate."""
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(side_effect=RuntimeError("Unexpected error"))

        with pytest.raises(RuntimeError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        assert "Unexpected error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_delete_automation_with_unique_id_directly(self, mock_client):
        """Should work with unique_id passed directly (not entity_id)."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._resolve_automation_id = AsyncMock(return_value="direct_unique_id")

        result = await mock_client.delete_automation_config("direct_unique_id")

        assert result["identifier"] == "direct_unique_id"
        assert result["unique_id"] == "direct_unique_id"
        assert result["operation"] == "deleted"


class TestUpsertAutomationConfigIdMismatch:
    """Guard against silent overwrite when ``identifier`` and ``config['id']``
    disagree (#1404).

    Home Assistant's automation storage uses the inner ``config['id']`` as the
    primary key. If the MCP server POSTs to ``/config/automation/config/{X}``
    with a body carrying ``id=Y``, HA stores the body under ``Y`` and the
    automation that previously owned ``Y`` is silently overwritten — while the
    response reports success for ``X``. The guard converts that silent data
    loss into a structured 400 error.
    """

    @pytest.fixture
    def mock_client(self):
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            return client

    @pytest.mark.asyncio
    async def test_update_with_mismatched_inner_id_is_rejected(self, mock_client):
        """identifier resolves to AAA but config carries id=BBB → reject."""
        mock_client._resolve_automation_id = AsyncMock(return_value="AAA")
        mock_client._request = AsyncMock()

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.upsert_automation_config(
                {"id": "BBB", "alias": "x", "trigger": [], "action": []},
                identifier="automation.foo",
            )

        assert exc_info.value.status_code == 400
        msg = str(exc_info.value)
        assert "Mismatched" in msg
        assert "'AAA'" in msg
        assert "'BBB'" in msg
        # Critical: the offending POST never reaches HA.
        mock_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_with_matching_inner_id_proceeds(self, mock_client):
        """identifier and config.id both resolve to the same unique_id → ok.

        Asserts URL and body id both equal the resolved unique_id so a future
        refactor that mutates one without the other (the exact failure mode
        this PR fixes) would regress.
        """
        mock_client._resolve_automation_id = AsyncMock(return_value="AAA")
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._poll_for_automation_entity = AsyncMock(return_value=None)

        result = await mock_client.upsert_automation_config(
            {"id": "AAA", "alias": "x", "trigger": [], "action": []},
            identifier="automation.foo",
            operation="updated",
        )

        assert result["unique_id"] == "AAA"
        assert result["operation"] == "updated"
        mock_client._request.assert_called_once()
        method, url = mock_client._request.call_args.args
        assert method == "POST"
        assert url == "/config/automation/config/AAA"
        assert mock_client._request.call_args.kwargs["json"]["id"] == "AAA"

    @pytest.mark.asyncio
    async def test_update_with_int_vs_str_id_equivalence_passes(self, mock_client):
        """``config['id']`` as int matching the resolved str id is treated as
        a match. HA accepts both shapes and stringifies on storage, so the
        guard's ``str(...)`` coercion is intentional — pin it so a future
        tightening to strict-type compare is a deliberate decision."""
        mock_client._resolve_automation_id = AsyncMock(return_value="1234")
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._poll_for_automation_entity = AsyncMock(return_value=None)

        result = await mock_client.upsert_automation_config(
            {"id": 1234, "alias": "x", "trigger": [], "action": []},
            identifier="automation.foo",
        )

        assert result["unique_id"] == "1234"
        mock_client._request.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_with_empty_string_inner_id_is_rejected(self, mock_client):
        """``config['id']=''`` is a real agent-slip pattern (templating with an
        empty variable). It is not None, so the guard fires and rejects."""
        mock_client._resolve_automation_id = AsyncMock(return_value="AAA")
        mock_client._request = AsyncMock()

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.upsert_automation_config(
                {"id": "", "alias": "x", "trigger": [], "action": []},
                identifier="automation.foo",
            )

        assert exc_info.value.status_code == 400
        mock_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_without_inner_id_proceeds(self, mock_client):
        """No inner id → existing code path injects unique_id, no guard fires."""
        mock_client._resolve_automation_id = AsyncMock(return_value="AAA")
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.upsert_automation_config(
            {"alias": "x", "trigger": [], "action": []},
            identifier="automation.foo",
        )

        assert result["unique_id"] == "AAA"
        # Sent body should carry the resolved id.
        sent_config = mock_client._request.call_args.kwargs["json"]
        assert sent_config["id"] == "AAA"

    @pytest.mark.asyncio
    async def test_create_with_inner_id_is_rejected(self, mock_client):
        """identifier=None + config['id']=X → reject (would silently overwrite
        X if it exists; would create with caller-chosen id if it doesn't —
        both are agent-slip patterns the guard converts to a loud error)."""
        mock_client._request = AsyncMock()

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.upsert_automation_config(
                {"id": "BBB", "alias": "x", "trigger": [], "action": []},
                identifier=None,
            )

        assert exc_info.value.status_code == 400
        msg = str(exc_info.value)
        assert "Cannot create" in msg
        assert "'BBB'" in msg
        assert "Omit 'id'" in msg
        mock_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_without_inner_id_proceeds(self, mock_client):
        """Normal create path: no inner id, fresh timestamp assigned."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._poll_for_automation_entity = AsyncMock(
            return_value="automation.new"
        )

        result = await mock_client.upsert_automation_config(
            {"alias": "x", "trigger": [], "action": []},
            identifier=None,
        )

        assert result["operation"] == "created"
        assert result["entity_id"] == "automation.new"


class TestUpsertAutomationResolvedShortCircuit:
    """``_resolved=True`` skips the redundant ``_resolve_automation_id`` lookup
    on ``upsert_automation_config`` (issue #1813 Phase 0 item #6).

    The tool pre-resolves the identifier during its hash-verify fetch and
    threads the resolved unique_id through, so the second resolve inside the
    upsert is pure waste. These tests pin that ``_resolved=True`` uses the
    passed id directly and never consults the resolver, while the default path
    still resolves — and that the #1404 mismatch guard is unaffected either way.
    """

    @pytest.fixture
    def mock_client(self):
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            return client

    @pytest.mark.asyncio
    async def test_resolved_skips_lookup(self, mock_client):
        """``_resolved=True`` uses ``identifier`` directly as the unique_id and
        does not consult the entity_id→unique_id resolver."""
        mock_client._resolve_automation_id = AsyncMock()
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.upsert_automation_config(
            {"alias": "x", "trigger": [], "action": []},
            identifier="AAA",
            _resolved=True,
        )

        assert result["unique_id"] == "AAA"
        mock_client._resolve_automation_id.assert_not_called()
        method, url = mock_client._request.call_args.args
        assert url == "/config/automation/config/AAA"
        # The resolved id is injected into the body just like the default path.
        assert mock_client._request.call_args.kwargs["json"]["id"] == "AAA"

    @pytest.mark.asyncio
    async def test_default_still_resolves(self, mock_client):
        """Contrast: without ``_resolved`` the entity_id→unique_id resolver is
        consulted (the pre-#1813 behaviour on a raw identifier)."""
        mock_client._resolve_automation_id = AsyncMock(return_value="AAA")
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.upsert_automation_config(
            {"alias": "x", "trigger": [], "action": []},
            identifier="automation.foo",
        )

        assert result["unique_id"] == "AAA"
        mock_client._resolve_automation_id.assert_awaited_once_with("automation.foo")

    @pytest.mark.asyncio
    async def test_resolved_still_enforces_id_mismatch_guard(self, mock_client):
        """``_resolved=True`` must not bypass the #1404 mismatch guard: a config
        ``id`` disagreeing with the threaded unique_id is still rejected before
        any POST reaches HA."""
        mock_client._resolve_automation_id = AsyncMock()
        mock_client._request = AsyncMock()

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.upsert_automation_config(
                {"id": "BBB", "alias": "x", "trigger": [], "action": []},
                identifier="AAA",
                _resolved=True,
            )

        assert exc_info.value.status_code == 400
        assert "Mismatched" in str(exc_info.value)
        # The resolver is never touched and the offending POST never fires.
        mock_client._resolve_automation_id.assert_not_called()
        mock_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_path_ignores_resolved_flag(self, mock_client):
        """On the create path (``identifier is None``) ``_resolved`` is a no-op —
        a fresh timestamp unique_id is still generated, resolver untouched."""
        mock_client._resolve_automation_id = AsyncMock()
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._poll_for_automation_entity = AsyncMock(
            return_value="automation.new"
        )

        result = await mock_client.upsert_automation_config(
            {"alias": "x", "trigger": [], "action": []},
            identifier=None,
            _resolved=True,
        )

        assert result["operation"] == "created"
        mock_client._resolve_automation_id.assert_not_called()


class TestUpsertAutomationOperation:
    """The caller says whether a write creates or updates (#2903)."""

    @pytest.fixture
    def mock_client(self):
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client._request = AsyncMock(return_value={"result": "ok"})
            client._poll_for_automation_entity = AsyncMock(
                return_value="automation.fresh"
            )
            return client

    @pytest.mark.asyncio
    async def test_a_fresh_explicit_id_is_reported_created_and_its_entity_found(
        self, mock_client
    ):
        """Guessing from the id reported a new automation as updated and
        never looked for its entity."""
        result = await mock_client.upsert_automation_config(
            {"alias": "x"}, "fresh_id", _resolved=True, operation="created"
        )

        assert result["operation"] == "created"
        assert result["entity_id"] == "automation.fresh"
        mock_client._poll_for_automation_entity.assert_awaited_once_with("fresh_id")

    @pytest.mark.asyncio
    async def test_without_an_operation_none_is_reported(self, mock_client):
        """A caller that did not read the target (a backup restore) gets no
        guessed operation."""
        result = await mock_client.upsert_automation_config(
            {"alias": "x"}, "some_id", _resolved=True
        )

        assert "operation" not in result
        mock_client._poll_for_automation_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_cores_rejection_text_survives_the_rewrap(self, mock_client):
        """The tool leads its error with Core's text, read from the body."""
        body = {"message": "Message malformed: Invalid trigger 'x' specified"}
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 400 - ...", status_code=400, response_data=body
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.upsert_automation_config(
                {"alias": "x"}, "some_id", _resolved=True, operation="updated"
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.response_data == body


def _resolver_client(
    state_error: Exception | None = None,
    *,
    registry: dict | Exception | None = None,
    state: dict | None = None,
) -> HomeAssistantClient:
    """A client whose registry answer and automation state read are given."""
    client = HomeAssistantClient.__new__(HomeAssistantClient)
    client.send_websocket_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=registry
        if isinstance(registry, Exception)
        else [registry or {"success": False, "error": "not found"}]
    )
    client.get_entity_state = AsyncMock(  # type: ignore[method-assign]
        side_effect=state_error, return_value=state
    )
    return client


@pytest.mark.asyncio
async def test_a_disabled_automation_resolves_through_the_registry() -> None:
    """A disabled automation has no state; resolving by state reported it
    missing, so it could not be read or removed."""
    client = _resolver_client(
        registry={"success": True, "result": {"unique_id": "1700000000000"}},
        state_error=HomeAssistantAPIError("missing", status_code=404),
    )

    assert await client._resolve_automation_id("automation.kitchen") == "1700000000000"
    client.get_entity_state.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "registry",
    [{"success": False, "error": "not found"}, HomeAssistantConnectionError("down")],
    ids=["registry-miss", "registry-unreachable"],
)
async def test_the_state_id_resolves_when_the_registry_does_not(
    registry: dict | Exception,
) -> None:
    """The state's ``id`` attribute is Core's own value, so it stays the
    fallback rather than a guess."""
    client = _resolver_client(
        registry=registry, state={"attributes": {"id": "1700000000000"}}
    )

    assert await client._resolve_automation_id("automation.kitchen") == "1700000000000"


@pytest.mark.asyncio
async def test_a_config_id_needs_no_lookup() -> None:
    client = _resolver_client()

    assert await client._resolve_automation_id("1700000000000") == "1700000000000"
    client.send_websocket_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        HomeAssistantConnectionError("down"),
        HomeAssistantAPIError("server error", status_code=500),
    ],
    ids=["connection", "server-error"],
)
async def test_a_failed_state_read_is_not_reported_as_a_missing_automation(
    error: Exception,
) -> None:
    """Reported as not found, a backup skips the automation silently and an
    agent may create a duplicate of it."""
    client = _resolver_client(error)

    with pytest.raises(type(error)) as exc_info:
        await client._resolve_automation_id("automation.kitchen")
    assert exc_info.value is error


class TestPollForAutomationEntity:
    """Tests for ``_poll_for_automation_entity`` (issues #1152, #1380, #1395).

    Since #1395, this method is a thin wrapper around
    ``wait_for_automation_entity_by_unique_id``. The bulk of the
    discovery semantics — WS subscribe/sample/wait, REST fallback,
    event-filter shape — is covered by ``test_wait_helpers.py``. The
    tests here lock the delegation contract: the method passes the
    unique_id through, swallows transient HA errors, and propagates
    programming bugs.

    Patch path: ``_poll_for_automation_entity`` does a *local* import of
    ``wait_for_automation_entity_by_unique_id`` from
    ``ha_mcp.tools.ws_waiters``. Patching that name before each call
    means the local import resolves to the patched function.
    """

    HELPER_PATH = "ha_mcp.tools.ws_waiters.wait_for_automation_entity_by_unique_id"

    @pytest.fixture
    def mock_client(self):
        """Create a mock HomeAssistantClient for testing."""
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            return client

    def test_poll_budget_shape(self):
        """Pin the upper-bound budget. Preserves the 6s ceiling PR #1384
        tuned for the legacy cadence loop — exceeding it would change the
        ``ha_config_set_automation`` latency contract."""
        assert HomeAssistantClient._POLL_BUDGET_S == 6.0

    @pytest.mark.asyncio
    async def test_poll_returns_entity_id_from_helper(self, mock_client):
        """When the helper resolves a match, the discovered entity_id is
        returned verbatim and the unique_id / timeout are forwarded
        through."""
        captured: dict[str, object] = {}

        async def fake_helper(client, unique_id, *, timeout):
            captured["client"] = client
            captured["unique_id"] = unique_id
            captured["timeout"] = timeout
            return "automation.test_target"

        with patch(self.HELPER_PATH, new=fake_helper):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result == "automation.test_target"
        assert captured["client"] is mock_client
        assert captured["unique_id"] == "unique_42"
        assert captured["timeout"] == HomeAssistantClient._POLL_BUDGET_S

    @pytest.mark.asyncio
    async def test_poll_returns_none_when_helper_times_out(self, mock_client):
        """Helper ``None`` (budget exhausted) surfaces as
        ``entity_not_verified=True`` upstream — verified here as the
        ``None`` return contract."""

        async def fake_helper(client, unique_id, *, timeout):
            return None

        with patch(self.HELPER_PATH, new=fake_helper):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result is None

    @pytest.mark.asyncio
    async def test_poll_swallows_homeassistant_error(self, mock_client):
        """Transient HA errors from the helper yield ``None`` rather than
        propagating — preserves the pre-#1395 contract so the caller
        records ``entity_not_verified=True`` and continues. Mirrors
        ``_POLLING_TRANSIENT_ERRORS`` in ``wait_helpers.py``."""

        async def fake_helper(client, unique_id, *, timeout):
            raise HomeAssistantAPIError("transient")

        with patch(self.HELPER_PATH, new=fake_helper):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result is None

    @pytest.mark.asyncio
    async def test_poll_propagates_typeerror_for_unexpected_errors(self, mock_client):
        """Programming bugs (TypeError, KeyError, AttributeError, …) must
        propagate — they are not transient, and swallowing them would mask
        the bug as ``entity_not_verified=True``. Locks the narrowed
        ``except HomeAssistantError`` clause: a future widen-back to
        ``except Exception`` would fail this test."""

        async def fake_helper(client, unique_id, *, timeout):
            raise TypeError("bug")

        with (
            patch(self.HELPER_PATH, new=fake_helper),
            pytest.raises(TypeError, match="bug"),
        ):
            await mock_client._poll_for_automation_entity("unique_42")
