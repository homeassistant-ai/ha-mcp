"""End-to-end test for tool security policies middleware (#966).

Currently a placeholder — full e2e requires a testcontainers Home Assistant
instance plus an MCP client driving the middleware over HTTP. Once the e2e
suite gains a fixture for the tool security policies pipeline (settings UI
routes mounted + middleware registered against the same FastMCP), this test
should be filled in. The integration coverage at
``tests/src/unit/policy/test_integration.py`` exercises the in-process
block/approve/recall loop in the meantime.

Run via ``cd tests && uv run pytest src/e2e/policy/``.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="e2e scaffold — requires HA testcontainer + policy fixture")
def test_blocked_call_then_approve_then_recall() -> None:
    """User policy gates ``ha_call_service`` for ``domain=lock``.

    Expected flow once implemented:

    1. ``PUT /api/policy/config`` enabling a rule on ``ha_call_service``
       with predicate ``args.service_data.entity_id startswith "lock."``
       (or ``args.domain == "lock"`` depending on the canonical schema).
    2. Call ``ha_call_service`` with a matching ``entity_id`` — expect a
       ``ToolError`` whose payload carries error code
       ``USER_APPROVAL_REQUIRED`` and an approval ``token``.
    3. ``POST /api/policy/approve`` with that token.
    4. Re-call ``ha_call_service`` with the same arguments — expect
       success (the remember-cache short-circuits the gate for the
       configured window, OR the queued approval is consumed once).
    5. Re-call with *different* arguments — expect
       ``USER_APPROVAL_REQUIRED`` again, confirming strict args binding
       (the gate does not blanket-approve every future call to the tool).
    """
