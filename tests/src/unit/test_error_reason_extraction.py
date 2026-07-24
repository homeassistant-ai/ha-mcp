"""Unit tests for extract_structured_error_reason (tools/helpers.py).

The helper is the single extraction seam behind the settings UI's beta panel,
the legacy-backup degradation warnings, and the legacy read error mapping
(#1996): its branches decide whether a human message + actionable step is
surfaced or the caller falls back to its own generic prefix.
"""

import json

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.helpers import extract_structured_error_reason


class TestExtractStructuredErrorReason:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            # Message + no suggestions at all → bare message.
            ({"error": {"message": "Broken."}}, "Broken."),
            # Singular "suggestion" key (what create_error_response emits for
            # exactly one suggestion) → appended.
            (
                {"error": {"message": "Broken.", "suggestion": "Fix it."}},
                "Broken. Fix it.",
            ),
            # Plural list wins when present.
            (
                {
                    "error": {
                        "message": "Broken.",
                        "suggestion": "Fix it.",
                        "suggestions": ["First.", "Second."],
                    }
                },
                "Broken. First.",
            ),
            # "error" present but not a dict → not a structured envelope.
            ({"error": "oops"}, None),
            # Missing / empty / non-str message → no usable reason.
            ({"error": {"suggestions": ["x"]}}, None),
            ({"error": {"message": ""}}, None),
            ({"error": {"message": 42}}, None),
        ],
    )
    def test_payload_shapes(self, payload, expected):
        assert extract_structured_error_reason(ToolError(json.dumps(payload))) == (
            expected
        )

    def test_non_json_and_non_dict_json_return_none(self):
        assert extract_structured_error_reason(ToolError("plain text")) is None
        assert extract_structured_error_reason(ToolError("[1, 2]")) is None

    def test_real_serializations_round_trip(self):
        # Built via create_error_response so the singular/plural key emission
        # is the real one: one suggestion → singular key only, two → plural.
        single = json.dumps(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED, "Entry missing.", suggestions=["A."]
            )
        )
        assert extract_structured_error_reason(ToolError(single)) == "Entry missing. A."
        double = json.dumps(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "Entry missing.",
                suggestions=["A.", "B."],
            )
        )
        assert extract_structured_error_reason(ToolError(double)) == "Entry missing. A."
