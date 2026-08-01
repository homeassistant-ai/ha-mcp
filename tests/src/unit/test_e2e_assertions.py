"""Unit tests for the e2e result-parsing and failure-classification contracts.

These pin three pieces of test infrastructure whose breakage is invisible from
the e2e suite itself, because every failure mode they guard degrades into a
*passing* run rather than a red one:

- ``parse_mcp_result``'s dict passthrough
  (``tests/src/e2e/utilities/assertions.py``). Hundreds of e2e call sites feed
  it results, and some of those never came from the client at all -- the
  ``_safe_tool_call`` wrappers return a marked dict. Losing the passthrough
  would replace the discrimination markers with a generic placeholder.
- The error-flag gate in the same helper. fastmcp's ``CallToolResult`` spells
  the flag ``is_error``; the raw MCP one spells it ``isError``. A gate that
  checks only one spelling is silently dead against the other transport, and
  a JSON error envelope decodes identically down the success branch, so
  nothing observable changes until the payload is not JSON.
- ``_hard_failures``
  (``tests/src/e2e/error_handling/test_network_errors.py``), which decides
  which concurrent results are regressions. Too strict and healthy bulk
  batches fail the suite; too loose and real tool errors ride through.

No Docker and no HA instance: every input here is a plain object.
"""

from __future__ import annotations

import json

from tests.src.e2e.error_handling.test_network_errors import _hard_failures
from tests.src.e2e.utilities.assertions import parse_mcp_result


class _TextBlock:
    """Minimal stand-in for an MCP text content block."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FastmcpResult:
    """Stand-in for fastmcp's ``CallToolResult`` (flag spelled ``is_error``).

    Deliberately a plain class rather than a ``MagicMock``: attribute
    auto-creation would make *every* flag spelling read truthy and hide
    exactly the bug these tests exist to catch.
    """

    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [_TextBlock(text)]
        self.structured_content = None
        self.is_error = is_error


class _RawMcpResult:
    """Stand-in for the raw MCP ``CallToolResult`` (flag spelled ``isError``)."""

    def __init__(self, text: str, error_flag: bool = False) -> None:
        self.content = [_TextBlock(text)]
        self.structured_content = None
        self.isError = error_flag


class TestParsedDictPassthrough:
    """An already-parsed dict comes back unchanged, markers intact."""

    def test_timed_out_wrapper_dict_is_returned_identically(self):
        wrapper_dict = {
            "success": False,
            "timed_out": True,
            "error": "Operation timed out after 10.0s",
        }
        parsed = parse_mcp_result(wrapper_dict)
        assert parsed is wrapper_dict
        assert parsed == wrapper_dict

    def test_tool_error_marker_and_structured_error_survive(self):
        """The marker AND the structured error body both have to survive.

        Callers discriminate on ``tool_error`` and then render the message
        out of the structured ``error`` dict, so flattening either one turns
        a hard failure into an unreadable one.
        """
        wrapper_dict = {
            "success": False,
            "tool_error": True,
            "error": {
                "code": "SERVICE_INVALID_ACTION",
                "message": "Invalid action 'invalid_action' for domain 'light'",
            },
        }
        parsed = parse_mcp_result(wrapper_dict)
        assert parsed == wrapper_dict
        assert parsed["tool_error"] is True
        assert parsed["error"]["code"] == "SERVICE_INVALID_ACTION"


class TestContentTextParsing:
    """A result carrying JSON in ``content[0].text`` parses to that body."""

    def test_json_text_block_parses_to_body(self):
        body = {
            "success": True,
            "entities": [{"entity_id": "light.bed_light", "state": "on"}],
        }
        assert parse_mcp_result(_FastmcpResult(json.dumps(body))) == body

    def test_non_json_text_block_falls_back_to_raw_response(self):
        assert parse_mcp_result(_FastmcpResult("not json at all")) == {
            "raw_response": "not json at all"
        }


_ERROR_ENVELOPE = {
    "success": False,
    "error": {
        "code": "VALIDATION_MISSING_PARAMETER",
        "message": "No operations provided",
    },
}


class TestErrorFlagRouting:
    """Both flag spellings route to the structured-error envelope."""

    def test_is_error_routes_to_parsed_envelope(self):
        result = _FastmcpResult(json.dumps(_ERROR_ENVELOPE), is_error=True)
        assert parse_mcp_result(result) == _ERROR_ENVELOPE

    def test_legacy_iserror_spelling_also_routes(self):
        result = _RawMcpResult(json.dumps(_ERROR_ENVELOPE), error_flag=True)
        assert parse_mcp_result(result) == _ERROR_ENVELOPE

    def test_is_error_with_non_json_body_takes_the_error_branch(self):
        """The spelling bug is only *observable* on a non-JSON body.

        Both branches ``json.loads`` a JSON envelope into the same dict, so a
        dead gate looks identical there. A non-JSON body separates them: the
        error branch yields ``{"success": False, "error": ...}``, the success
        branch yields ``{"raw_response": ...}``.
        """
        assert parse_mcp_result(_FastmcpResult("boom", is_error=True)) == {
            "success": False,
            "error": "boom",
        }

    def test_legacy_iserror_with_non_json_body_takes_the_error_branch(self):
        assert parse_mcp_result(_RawMcpResult("boom", error_flag=True)) == {
            "success": False,
            "error": "boom",
        }

    def test_unflagged_result_stays_on_the_success_branch(self):
        """A false flag must not route -- the gate reads the value, not the
        mere presence of the attribute."""
        assert parse_mcp_result(_FastmcpResult("boom")) == {"raw_response": "boom"}
        assert parse_mcp_result(_RawMcpResult("boom")) == {"raw_response": "boom"}


class TestHardFailureClassifier:
    """``_hard_failures`` flags regressions and only regressions."""

    def test_exception_instance_is_flagged(self):
        exc = RuntimeError("asyncio.gather captured this")
        assert _hard_failures([exc]) == [exc]

    def test_tool_error_dict_is_flagged(self):
        failure = {
            "success": False,
            "tool_error": True,
            "error": "Invalid action 'invalid_action' for domain 'light'",
        }
        assert _hard_failures([failure]) == [failure]

    def test_timed_out_dict_is_tolerated(self):
        tolerated = {
            "success": False,
            "timed_out": True,
            "error": "Operation timed out after 10.0s",
        }
        assert _hard_failures([tolerated]) == []

    def test_healthy_bulk_response_without_success_key_is_not_flagged(self):
        """A dispatched batch reports per-item counts and no top-level
        ``success``; truthiness-testing it would fail every healthy run."""
        healthy_bulk = {
            "total_operations": 1,
            "successful_commands": 1,
            "failed_commands": 0,
            "skipped_operations": 0,
            "execution_mode": "parallel",
            "operation_ids": ["op-1"],
            "results": [{"command_sent": True}],
        }
        assert _hard_failures([healthy_bulk]) == []

    def test_explicit_success_true_is_not_flagged(self):
        assert _hard_failures([{"success": True, "data": {}}]) == []

    def test_mixed_batch_returns_only_the_untolerated_failures(self):
        exc = RuntimeError("transport dropped")
        tool_error = {"success": False, "tool_error": True, "error": "boom"}
        tolerated = {"success": False, "timed_out": True, "error": "slow"}
        healthy_bulk = {"successful_commands": 2, "failed_commands": 0}
        results = [exc, tolerated, tool_error, healthy_bulk, {"success": True}]
        assert _hard_failures(results) == [exc, tool_error]
