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

import pytest

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


class TestClassifyByMessageBranches:
    """Mutation-test coverage for every elif branch of ``_classify_by_message``
    in ``src/ha_mcp/tools/helpers.py``. Each test exercises exactly one branch
    so reordering arms, renaming a substring, or dropping an elif is caught
    against the classifier directly — not via whichever tool happens to
    surface the branch through an integration path.

    Branches covered:

    1. Plain ``"not found"`` substring without ``unknown config specified``
       and without ``entity_id`` context → ``RESOURCE_NOT_FOUND``
    2. Plain ``"404"`` substring → ``RESOURCE_NOT_FOUND``
    3. Plain ``"not found"`` WITH ``entity_id`` context → ``ENTITY_NOT_FOUND``
       (symmetric case to
       ``test_unknown_config_specified_with_entity_id_context_promotes_to_entity_not_found``)
    4. ``"timeout"`` message-substring path → ``TIMEOUT_OPERATION``
       (separately from the typed ``TimeoutError`` dispatch in
       ``_classify_exception``)
    5. ``"connection"``/``"connect"`` message-substring path →
       ``CONNECTION_FAILED`` (separately from the typed
       ``HomeAssistantConnectionError`` dispatch). Also indirectly pinned
       at
       ``test_tool_error_signaling.py::TestErrorCodeMapping::test_connection_error_in_message_maps_correctly``
       via plain ``Exception("Connection refused")`` falling through to
       ``_classify_by_message`` — duplicate-covered here for self-contained
       mutation coverage in this class.
    6. Schema markers under ``Command failed:`` prefix → ``VALIDATION_FAILED``
       (Supervisor ``vol.Invalid`` wire format, issue #993).
    7. Auth phrases (``unauthorized`` / ``authentication`` / ``invalid
       token`` / ``access denied``) + ``401`` numeric signal →
       ``AUTH_INVALID_TOKEN`` (issue #993).
    8. ``command failed:`` SERVICE_CALL_FAILED fallback (no schema markers,
       no ``expected <type>`` regex hit)
    9. Final ``else`` → ``INTERNAL_ERROR``

    Precedence/gate behaviour (``command failed:`` outer gate, ``authorized_keys``
    false-positive guard) and typed-exception dispatch via
    ``_classify_exception`` (HomeAssistantAuthError, HomeAssistantConnectionError,
    HomeAssistantCommandError) live in
    ``test_tool_error_signaling.py::TestSchemaAndAuthClassification`` — those
    exercise the type-dispatch layer and the precedence gates, not the
    message-substring elif chain.
    """

    @pytest.mark.parametrize(
        "error_str,expected_code",
        [
            # 1. Plain "not found" substring (no "unknown config specified"
            #    marker, no entity_id context → bare RESOURCE_NOT_FOUND).
            ("entity not found: light.foo", "RESOURCE_NOT_FOUND"),
            # 2. Plain "404" substring.
            ("HTTP 404", "RESOURCE_NOT_FOUND"),
            # 4. Message-substring timeout path. Uses a plain Exception so
            #    the typed TimeoutError case in _classify_exception is
            #    bypassed and routing falls into _classify_by_message.
            ("request timeout after 30s", "TIMEOUT_OPERATION"),
            # 5. Message-substring connection path. Plain Exception again
            #    so the typed HomeAssistantConnectionError dispatch in
            #    _classify_exception is bypassed.
            ("connection refused", "CONNECTION_FAILED"),
            # 6. command failed: fallback (no schema markers, no
            #    "expected <type>" regex, not auth, not 404) →
            #    SERVICE_CALL_FAILED. Mirrors the 4xx fallback in
            #    _classify_api_status.
            ("command failed: websocket dispatch error", "SERVICE_CALL_FAILED"),
            # 7. Final else: an unmapped message reaches the catch-all
            #    INTERNAL_ERROR sink, not a different bucket via accidental
            #    substring overlap.
            ("totally unexpected gibberish", "INTERNAL_ERROR"),
        ],
        ids=[
            "plain_not_found",
            "plain_404",
            "timeout_substring",
            "connection_substring",
            "command_failed_fallback",
            "else_internal_error",
        ],
    )
    def test_classify_by_message_routes_to_expected_code(
        self, error_str, expected_code
    ):
        """Each test message exercises exactly one elif branch in
        ``_classify_by_message`` that has no other direct pin."""
        result = exception_to_structured_error(Exception(error_str), raise_error=False)
        assert result["error"]["code"] == expected_code, (
            f"input {error_str!r} routed to {result['error'].get('code')!r}, "
            f"expected {expected_code!r}"
        )

    # 3. Plain "not found" WITH entity_id context: the symmetric case to
    #    ``test_unknown_config_specified_with_entity_id_context_promotes_to_entity_not_found``.
    #    The new ``unknown config specified`` substring lands on the same
    #    context-promotion code path; this test pins that the OLDER ``not
    #    found`` substring still promotes — a regression here would mean
    #    the promotion got tied to the new substring alone.
    def test_plain_not_found_with_entity_id_context_promotes_to_entity_not_found(
        self,
    ):
        """Plain ``not found`` + entity_id context → ENTITY_NOT_FOUND.

        Pins the context-promotion path for the long-standing ``not found``
        substring (predates #1345's ``unknown config specified`` addition).
        Drop the ``or "not found" in error_str`` clause from the elif and
        the not-found path is skipped entirely; the result drops to
        ``INTERNAL_ERROR`` via the final else.
        """
        result = exception_to_structured_error(
            Exception("entity light.living_room not found"),
            context={"entity_id": "light.living_room"},
            raise_error=False,
        )

        assert result["error"]["code"] == "ENTITY_NOT_FOUND", (
            f"Expected ENTITY_NOT_FOUND under entity_id context with "
            f"plain 'not found' substring, got: {result['error'].get('code')}"
        )

    # --- Schema markers: vol.Invalid messages under "Command failed:" prefix ---

    SCHEMA_MARKER_MESSAGES: tuple[tuple[str, str], ...] = (
        # marker id, full message
        ("missing_option", "Command failed: Missing option 'authorized_keys' in ssh"),
        ("extra_keys", "Command failed: extra keys not allowed @ data['foo']"),
        ("unknown_secret", "Command failed: Unknown secret 'api_key'"),
        ("unknown_type", "Command failed: Unknown type 'timedelta'"),
        (
            "expected_a",
            "Command failed: expected a string for dictionary value @ data['host']",
        ),
        ("expected_str", "Command failed: expected str for 'name'"),
        ("expected_int", "Command failed: expected int for 'port'"),
        ("expected_bool", "Command failed: expected bool"),
        ("expected_dict", "Command failed: expected dict"),
        ("expected_list", "Command failed: expected list of strings"),
        ("expected_float", "Command failed: expected float value"),
        ("expected_type", "Command failed: expected type 'str'"),
        ("expected_one_of", "Command failed: expected one of ['a', 'b', 'c']"),
    )

    @pytest.mark.parametrize(
        "marker_id,message",
        SCHEMA_MARKER_MESSAGES,
        ids=[m[0] for m in SCHEMA_MARKER_MESSAGES],
    )
    def test_schema_marker_classified_as_validation_failed(self, marker_id, message):
        """Each vol.Invalid marker under "Command failed:" routes to VALIDATION_FAILED.

        Mutation-testing-style coverage: drop any marker from the source
        tuple in helpers.py and the corresponding parametrized case fails.
        """
        exc = HomeAssistantCommandError(message)
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "VALIDATION_FAILED", (
            f"marker {marker_id!r} did not route to VALIDATION_FAILED"
        )

    # --- Auth branch: phrase list + 401 numeric signal ---

    AUTH_PHRASE_MESSAGES: tuple[tuple[str, str], ...] = (
        ("unauthorized", "unauthorized: invalid bearer token"),
        ("authentication", "authentication required"),
        ("invalid_token", "token rejected: invalid token format"),
        ("access_denied", "access denied for user"),
    )

    @pytest.mark.parametrize(
        "phrase_id,message",
        AUTH_PHRASE_MESSAGES,
        ids=[m[0] for m in AUTH_PHRASE_MESSAGES],
    )
    def test_auth_phrase_classified(self, phrase_id, message):
        """Each auth phrase routes to AUTH_INVALID_TOKEN.

        Mutation-testing coverage: drop any phrase from the source tuple
        in helpers.py and the corresponding case fails.
        """
        exc = Exception(message)
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "AUTH_INVALID_TOKEN", (
            f"phrase {phrase_id!r} did not route to AUTH_INVALID_TOKEN"
        )

    def test_401_status_still_classified_as_auth(self):
        """401 numeric signal in error text remains an auth error."""
        exc = Exception("Server returned 401")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "AUTH_INVALID_TOKEN"
