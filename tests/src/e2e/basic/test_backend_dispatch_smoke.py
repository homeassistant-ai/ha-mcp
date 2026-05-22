"""Multi-layer smoke tests for backend dispatch correctness.

The three e2e CI lanes set env vars that ``conftest.ha_container_with_fresh_config``
reads to choose a backend:

| Lane                          | HAOS_TEST_IMAGE_PATH | HAOS_TEST_MODE | expected backend |
| ----------------------------- | -------------------- | -------------- | ---------------- |
| e2e-tests.yml (testcontainer) | unset                | unset          | ``container``    |
| haos-e2e-tests.yml (external) | set                  | unset          | ``haos``         |
| haos-e2e-inaddon-tests.yml    | set                  | ``inaddon``    | ``haos_inaddon`` |

Three layers of guard, each catching a different silent-failure mode:

1. ``test_backend_dispatch_matches_workflow_env`` — asserts the ``backend``
   field that conftest sets matches what the workflow env vars imply.
   Catches dispatch falling through to the wrong code path.

2. ``test_supervisor_addon_tool_behavior_matches_backend`` — calls
   ``ha_get_addon`` (which only works when a real Supervisor is present)
   and asserts success-vs-failure matches the claimed backend. Catches
   the case where conftest reports ``backend=X`` but actually a different
   HA instance is running (e.g. mock Supervisor on testcontainer making
   addon calls succeed when the real backend has no Supervisor at all,
   or HAOS reporting itself as container).

3. ``test_session_collected_test_count_above_floor`` — asserts the
   session collected at least the baseline number of tests. Catches
   collection-time regressions where a test file fails to import and
   pytest silently drops tens of tests.

This file is placed under ``basic/`` (NOT ``haos_only/``) on purpose: the
auto-applied ``haos_only`` marker would skip these whenever
``is_haos_backend_selected()`` returns False, which is exactly the
silent-failure case we want to catch. ``basic/`` has no auto-applied
markers so the tests run unconditionally on every lane.
"""

from __future__ import annotations

import os
from typing import Any

from ..utilities.assertions import safe_call_tool

# Floor for total collected tests across all lanes. As of 2026-05-22 each
# lane collects ~913 tests (just differing skip mix per mode). Set well
# below current value to allow normal test-add/remove churn while still
# catching the case where ~50+ tests vanish from collection.
_COLLECTION_FLOOR = 850


def test_backend_dispatch_matches_workflow_env(
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Conftest dispatch must pick the backend the workflow env implies.

    Runs unconditionally on every lane — branches off env vars to mirror
    conftest's own dispatch logic. Mismatch means the dispatch silently
    picked a different backend than CI asked for.
    """
    image_path = os.environ.get("HAOS_TEST_IMAGE_PATH")
    mode = os.environ.get("HAOS_TEST_MODE", "")
    backend = ha_container_with_fresh_config["backend"]

    if image_path and mode == "inaddon":
        assert backend == "haos_inaddon", (
            f"Workflow set HAOS_TEST_IMAGE_PATH + HAOS_TEST_MODE=inaddon "
            f"but dispatch picked backend={backend!r}. The inaddon "
            f"integration is NOT being exercised by this run."
        )
        addon_mcp_url = ha_container_with_fresh_config.get("addon_mcp_url")
        assert addon_mcp_url and addon_mcp_url.startswith("http"), (
            f"haos_inaddon backend reported but addon_mcp_url is "
            f"{addon_mcp_url!r}. mcp_client fixtures will route to the "
            f"wrong endpoint."
        )
        assert ha_container_with_fresh_config["container"] is None
    elif image_path:
        assert backend == "haos", (
            f"Workflow set HAOS_TEST_IMAGE_PATH but dispatch picked "
            f"backend={backend!r}. The lane silently fell through to "
            f"the testcontainer path; tests are running against the "
            f"wrong HA instance."
        )
        assert ha_container_with_fresh_config["container"] is None
        assert ha_container_with_fresh_config["port"] is None
        assert ha_container_with_fresh_config["config_path"] is None
        assert ha_container_with_fresh_config["addon_mcp_url"] is None
    else:
        assert backend == "container", (
            f"No HAOS env vars set, expected testcontainer backend, "
            f"got backend={backend!r}."
        )
        assert ha_container_with_fresh_config["container"] is not None
        assert ha_container_with_fresh_config["port"] is not None


async def test_supervisor_addon_tool_behavior_matches_backend(
    mcp_client: Any,
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Behavioral cross-check: ``ha_get_addon`` must succeed on HAOS, fail on container.

    Stronger guarantee than the dispatch-field check: conftest could
    self-report ``backend=container`` but actually have HAOS running
    (or vice versa). A real Supervisor only exists on HAOS — the HA
    Core testcontainer has no Supervisor service running. So:

    - HAOS external + inaddon: ``ha_get_addon`` returns a populated
      addons list (the bake installs several addons).
    - testcontainer: ``ha_get_addon`` returns ``success=False`` because
      the Supervisor proxy endpoint is unreachable.

    The asymmetry of this check makes it impossible for one backend to
    impersonate the other while keeping this test green.
    """
    backend = ha_container_with_fresh_config["backend"]
    result = await safe_call_tool(mcp_client, "ha_get_addon", {})

    if backend in ("haos", "haos_inaddon"):
        assert result.get("success") is True, (
            f"ha_get_addon failed on {backend} backend; Supervisor must "
            f"be running. Result: {result!r}"
        )
        # list_addons returns ``{"success": True, "addons": [...], "summary": {...}}``
        # — ``addons`` is a top-level key, not nested under ``data``.
        addons = result.get("addons") or []
        assert isinstance(addons, list) and len(addons) > 0, (
            f"Expected installed addons on {backend} (the bake installs "
            f"Node-RED, ESPHome, AppDaemon, dev addon, etc.); got "
            f"{addons!r}"
        )
    else:
        # testcontainer has no Supervisor → ha_get_addon must fail
        assert result.get("success") is False, (
            f"ha_get_addon unexpectedly succeeded on {backend} backend. "
            f"Testcontainer has no Supervisor service; success here "
            f"means we're actually running on HAOS but conftest reported "
            f"backend={backend!r}. Result: {result!r}"
        )


def test_session_collected_test_count_above_floor(request: Any) -> None:
    """Session collected at least ``_COLLECTION_FLOOR`` tests.

    Catches collection-time regressions: a test file fails to import,
    pytest collects tens of fewer tests, the suite stays green with
    reduced coverage. Collection count is mode-independent (all lanes
    collect the same items, mode only changes pass/skip mix).
    """
    total = len(request.session.items)
    assert total >= _COLLECTION_FLOOR, (
        f"Only {total} tests collected, expected >= {_COLLECTION_FLOOR}. "
        f"A test file likely failed to import, dropping coverage. Check "
        f"for collection errors in the pytest output."
    )
