"""Unit tests for the dashboard-resolver helpers in tools_config_dashboards.

The helpers under test (`_should_lazy_resolve`, `_resolve_dashboard`,
`_lazy_resolve_and_retry`) hold the substring-trigger contract and the
two-call-site resolver glue that the rest of the dual-accept identifier
design rests on. End-to-end tests would only catch a regression here on
the right HA-version axis; these unit tests pin the contract independent
of HA wording stability.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_config_dashboards import (
    _LAZY_RESOLVE_TRIGGER,
    _lazy_resolve_and_retry,
    _resolve_dashboard,
    _should_lazy_resolve,
)

# -----------------------------------------------------------------------------
# Fixtures / helpers
# -----------------------------------------------------------------------------


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return client


def _trigger_response(missing_id: str = "anything") -> dict:
    """Build the WS error envelope HA emits when ``lovelace/config`` is
    called with an identifier it does not recognise. Includes the literal
    trigger substring."""
    return {
        "success": False,
        "error": {
            "message": f"{_LAZY_RESOLVE_TRIGGER}: {missing_id}",
            "code": "config_not_found",
        },
    }


def _success_response(payload: dict | None = None) -> dict:
    return {"success": True, "result": payload or {"views": []}}


# -----------------------------------------------------------------------------
# _should_lazy_resolve — substring contract
# -----------------------------------------------------------------------------


class TestShouldLazyResolve:
    """The substring trigger is the only signal available at the tool
    layer. Pin the contract so an HA-side wording change is caught here
    rather than degrading to "lazy fallback never fires" silently."""

    def test_exact_trigger_message(self):
        assert _should_lazy_resolve(_LAZY_RESOLVE_TRIGGER) is True

    def test_trigger_with_identifier_suffix(self):
        # The HA emit form is f"Unknown config specified: {url_path}".
        assert _should_lazy_resolve("Unknown config specified: my_dashboard") is True

    def test_trigger_embedded_in_longer_message(self):
        assert (
            _should_lazy_resolve("Command failed: Unknown config specified: foo")
            is True
        )

    def test_unrelated_message_does_not_match(self):
        assert _should_lazy_resolve("Some other error") is False
        assert _should_lazy_resolve("") is False

    def test_legitimate_empty_dashboard_message_does_not_match(self):
        # HA emits "No config found." for genuinely empty (un-initialised)
        # dashboards — must NOT trigger a lazy retry, otherwise the
        # caller's empty-state path is hidden.
        assert _should_lazy_resolve("No config found.") is False


# -----------------------------------------------------------------------------
# _resolve_dashboard — registry lookup
# -----------------------------------------------------------------------------


class TestResolveDashboard:
    async def test_match_by_url_path(self, fake_client):
        fake_client.send_websocket_message.return_value = {
            "result": [
                {"url_path": "my-dash", "id": "my_dash"},
                {"url_path": "other", "id": "other_id"},
            ]
        }
        result = await _resolve_dashboard(fake_client, "my-dash")
        assert result == {"url_path": "my-dash", "id": "my_dash"}

    async def test_match_by_internal_id(self, fake_client):
        fake_client.send_websocket_message.return_value = {
            "result": [{"url_path": "my-dash", "id": "my_dash"}]
        }
        result = await _resolve_dashboard(fake_client, "my_dash")
        assert result == {"url_path": "my-dash", "id": "my_dash"}

    async def test_response_as_bare_list(self, fake_client):
        # Older HA versions / different response shapes return the list
        # directly rather than wrapped in {"result": ...}.
        fake_client.send_websocket_message.return_value = [
            {"url_path": "my-dash", "id": "my_dash"}
        ]
        result = await _resolve_dashboard(fake_client, "my_dash")
        assert result == {"url_path": "my-dash", "id": "my_dash"}

    async def test_no_match_returns_none(self, fake_client):
        fake_client.send_websocket_message.return_value = {
            "result": [{"url_path": "my-dash", "id": "my_dash"}]
        }
        assert await _resolve_dashboard(fake_client, "nonexistent") is None

    async def test_malformed_shape_logs_warning_and_returns_none(
        self, fake_client, caplog
    ):
        # Neither dict-with-result nor list — could be a future HA shape
        # change or an error envelope. Must surface as a logger.warning,
        # not silently degrade to "always no match".
        fake_client.send_websocket_message.return_value = "unexpected string"
        with caplog.at_level(
            logging.WARNING, logger="ha_mcp.tools.tools_config_dashboards"
        ):
            result = await _resolve_dashboard(fake_client, "anything")
        assert result is None
        assert any("unexpected shape" in rec.message for rec in caplog.records), (
            f"expected an 'unexpected shape' warning; got {caplog.records}"
        )

    async def test_missing_url_path_in_match_returns_none(self, fake_client):
        # Malformed registry entry where the matching dashboard is
        # missing one of the required fields. Must skip / return None
        # rather than forwarding empty strings to delete_dashboard.
        fake_client.send_websocket_message.return_value = {
            "result": [{"id": "my_dash"}]  # url_path missing entirely
        }
        assert await _resolve_dashboard(fake_client, "my_dash") is None

    async def test_empty_id_in_match_returns_none(self, fake_client):
        fake_client.send_websocket_message.return_value = {
            "result": [{"url_path": "my-dash", "id": ""}]
        }
        assert await _resolve_dashboard(fake_client, "my-dash") is None


# -----------------------------------------------------------------------------
# _lazy_resolve_and_retry — composition + no-op axes
# -----------------------------------------------------------------------------


class TestLazyResolveAndRetry:
    async def test_success_response_short_circuits(self, fake_client):
        ws_data = {"type": "lovelace/config", "url_path": "anything"}
        response = _success_response()
        new_url, new_response = await _lazy_resolve_and_retry(
            fake_client, "anything", ws_data, response
        )
        assert (new_url, new_response) == ("anything", response)
        # No WS call — short-circuit must not pay the round-trip.
        fake_client.send_websocket_message.assert_not_called()

    async def test_empty_url_path_short_circuits(self, fake_client):
        # Default-dashboard path: caller passes None for url_path.
        ws_data = {"type": "lovelace/config"}
        response = _trigger_response()
        new_url, new_response = await _lazy_resolve_and_retry(
            fake_client, None, ws_data, response
        )
        assert new_url is None
        assert new_response is response
        fake_client.send_websocket_message.assert_not_called()

    async def test_non_trigger_failure_short_circuits(self, fake_client):
        # Failure response, but with a different error message. Must NOT
        # invoke the resolver — that would surface a synthetic resolver
        # error instead of the real HA error to the caller.
        ws_data = {"type": "lovelace/config", "url_path": "x"}
        response = {
            "success": False,
            "error": {"message": "permission denied"},
        }
        new_url, new_response = await _lazy_resolve_and_retry(
            fake_client, "x", ws_data, response
        )
        assert (new_url, new_response) == ("x", response)
        fake_client.send_websocket_message.assert_not_called()

    async def test_resolver_no_match_returns_original_response(self, fake_client):
        # Trigger fired, resolver runs, but the registry has no match —
        # original failure response wins so the caller's existing error
        # path runs against the real HA error.
        fake_client.send_websocket_message.side_effect = [
            {"result": []},  # resolver: empty list, no match
        ]
        ws_data = {"type": "lovelace/config", "url_path": "ghost"}
        original = _trigger_response("ghost")
        new_url, new_response = await _lazy_resolve_and_retry(
            fake_client, "ghost", ws_data, original
        )
        assert new_url == "ghost"
        assert new_response is original

    async def test_resolver_exception_falls_through(self, fake_client, caplog):
        # Resolver raises (timeout, network blip). Must NOT escape; must
        # log at WARNING and fall through to the original response so
        # the caller's existing error path surfaces the real HA error.
        fake_client.send_websocket_message.side_effect = ConnectionError("ws gone")
        ws_data = {"type": "lovelace/config", "url_path": "x"}
        original = _trigger_response("x")
        with caplog.at_level(
            logging.WARNING, logger="ha_mcp.tools.tools_config_dashboards"
        ):
            new_url, new_response = await _lazy_resolve_and_retry(
                fake_client, "x", ws_data, original
            )
        assert (new_url, new_response) == ("x", original)
        assert any("Lazy resolver failed" in rec.message for rec in caplog.records)

    async def test_happy_path_resolves_and_retries(self, fake_client):
        # Trigger fires, resolver finds the canonical url_path, retry
        # succeeds with new url_path on the WS data dict.
        fake_client.send_websocket_message.side_effect = [
            {  # resolver
                "result": [{"url_path": "my-dash", "id": "my_dash"}]
            },
            _success_response({"views": [{"cards": []}]}),  # retry
        ]
        ws_data = {"type": "lovelace/config", "url_path": "my_dash", "force": True}
        original = _trigger_response("my_dash")
        new_url, new_response = await _lazy_resolve_and_retry(
            fake_client, "my_dash", ws_data, original
        )
        assert new_url == "my-dash"
        assert new_response["success"] is True

        # Caller's ws_data dict must NOT be mutated — the retry uses a
        # shallow copy. Verify both the contract and that the retry call
        # carried the canonical url_path.
        assert ws_data["url_path"] == "my_dash", (
            "_lazy_resolve_and_retry mutated the caller's ws_data dict"
        )
        retry_call = fake_client.send_websocket_message.call_args_list[1]
        assert retry_call.args[0]["url_path"] == "my-dash"
        assert retry_call.args[0]["type"] == "lovelace/config"
