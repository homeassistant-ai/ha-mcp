"""Unit tests for the exception → structured-error classifier in tools/helpers.py.

Pins the contract that domain-specific 404 wire formats from HA's WebSocket
bridge get classified as RESOURCE_NOT_FOUND (or ENTITY_NOT_FOUND when an
``entity_id`` is in context) rather than falling through to the generic
SERVICE_CALL_FAILED bucket.

Issue #1297 dashboards-classifier extension: HA Core's lovelace/config
response for a missing dashboard arrives as
``HomeAssistantCommandError("Command failed: Unknown config specified:
<url_path>")``. The classifier's 404 branch must catch the
``unknown config specified`` substring alongside the existing ``not found``
and ``404`` substrings, otherwise the message clears the schema-validation
gate (no schema markers, no ``expected <type>`` regex hit), misses the
not-found branch, and falls into the ``command failed:`` SERVICE_CALL_FAILED
fallback — losing the not-found signal the agent retry path branches on.
"""

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools.helpers import exception_to_structured_error


class TestUnknownConfigSpecifiedClassification:
    """``Unknown config specified`` WS-bridge string → RESOURCE_NOT_FOUND."""

    def test_dashboard_404_via_ws_bridge_classified_as_resource_not_found(self):
        """HA Core's standard wording (mixed case) must classify as 404,
        not SERVICE_CALL_FAILED. The classifier lowercases input via
        ``exception_to_structured_error``, so the substring check matches
        regardless of upstream casing."""
        err = HomeAssistantCommandError(
            "Command failed: Unknown config specified: my-dash"
        )
        result = exception_to_structured_error(err, raise_error=False)

        assert result["success"] is False
        assert result["error"]["code"] == "RESOURCE_NOT_FOUND", (
            f"Expected RESOURCE_NOT_FOUND, got: {result['error'].get('code')}. "
            "Regression: dashboards 404s falling back into SERVICE_CALL_FAILED."
        )

    def test_unknown_config_specified_without_command_failed_prefix(self):
        """Pins that the not-found classification fires on the substring
        alone — without the ``Command failed: `` prefix that the schema
        gate keys on. Guards against a future refactor that ties the
        substring check to the prefix and silently breaks any caller
        path that surfaces the wording outside the WS-bridge wrapping."""
        err = HomeAssistantCommandError("Unknown config specified: my-dash")
        result = exception_to_structured_error(err, raise_error=False)

        assert result["error"]["code"] == "RESOURCE_NOT_FOUND", (
            f"Expected RESOURCE_NOT_FOUND without 'Command failed:' prefix, "
            f"got: {result['error'].get('code')}"
        )

    def test_unknown_config_specified_all_lowercase_input(self):
        """Pins the ``str(error).lower()`` normalization step in
        ``exception_to_structured_error``. The substring check is
        case-insensitive by construction (input is lowercased before
        the elif chain), so a fully-lowercase input must still match.
        Guards against a future change that drops the normalization
        step and re-introduces case-sensitivity on the wire format."""
        err = HomeAssistantCommandError(
            "command failed: unknown config specified: my-dash"
        )
        result = exception_to_structured_error(err, raise_error=False)

        assert result["error"]["code"] == "RESOURCE_NOT_FOUND", (
            f"Expected RESOURCE_NOT_FOUND on all-lowercase input, "
            f"got: {result['error'].get('code')}"
        )

    def test_dashboard_404_preserves_message_for_agent_diagnostics(self):
        """Caller needs the original message visible so the agent can extract
        the offending url_path for a retry/lookup. Pins that the
        details/message fields carry the full upstream wording."""
        err = HomeAssistantCommandError(
            "Command failed: Unknown config specified: unifi-root"
        )
        result = exception_to_structured_error(err, raise_error=False)

        haystack = (
            (result["error"].get("message") or "")
            + " "
            + str(result["error"].get("details") or "")
        )
        assert "unifi-root" in haystack, (
            f"Original url_path missing from result error fields: {result['error']}"
        )

    def test_unknown_config_specified_with_entity_id_context_promotes_to_entity_not_found(
        self,
    ):
        """When caller supplies ``entity_id`` in context (atypical for
        dashboards but symmetric with the existing ``not found``/``404``
        branches), the result upgrades to ENTITY_NOT_FOUND. Pins that
        the new substring lands on the same context-promotion code path,
        not an isolated dashboards-specific branch."""
        err = HomeAssistantCommandError(
            "Command failed: Unknown config specified: my-dash"
        )
        result = exception_to_structured_error(
            err,
            context={"entity_id": "automation.foo"},
            raise_error=False,
        )

        assert result["error"]["code"] == "ENTITY_NOT_FOUND", (
            f"Expected ENTITY_NOT_FOUND under entity_id context, got: "
            f"{result['error'].get('code')}"
        )
