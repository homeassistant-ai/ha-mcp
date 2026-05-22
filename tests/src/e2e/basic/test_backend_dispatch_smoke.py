"""Verifies the conftest backend dispatch picks the backend CI asked for.

The three e2e CI lanes set env vars that ``conftest.ha_container_with_fresh_config``
reads to choose a backend:

| Lane                          | HAOS_TEST_IMAGE_PATH | HAOS_TEST_MODE | expected backend |
| ----------------------------- | -------------------- | -------------- | ---------------- |
| e2e-tests.yml (testcontainer) | unset                | unset          | ``container``    |
| haos-e2e-tests.yml (external) | set                  | unset          | ``haos``         |
| haos-e2e-inaddon-tests.yml    | set                  | ``inaddon``    | ``haos_inaddon`` |

Without an explicit assertion, the dispatch has silent-failure modes that
all leave CI green while running tests against the wrong backend:

1. ``HAOS_TEST_IMAGE_PATH`` set but ``is_haos_backend_selected()`` returns
   False (env-var name drift, import-time bug) — both HAOS lanes silently
   fall through to the testcontainer path.
2. ``HAOS_TEST_MODE=inaddon`` set but ``is_haos_inaddon_mode()`` reads a
   different name — inaddon lane silently runs the external dispatch,
   so ``mcp_client`` talks to the in-process FastMCP server instead of
   the addon's HTTP endpoint. The whole inaddon integration is untested.
3. Inaddon dispatch reached but ``addon_mcp_url`` never populated —
   downstream fixtures route to the wrong endpoint and surface as
   confusing errors later.

This file is placed under ``basic/`` (NOT ``haos_only/``) on purpose:
the auto-applied ``haos_only`` marker in ``conftest.pytest_collection_modifyitems``
would skip the test whenever ``is_haos_backend_selected()`` returns False,
which is exactly the silent-failure case (1) above. We want the test to
RUN on every lane and FAIL when the backend doesn't match the env.
"""

from __future__ import annotations

import os
from typing import Any


def test_backend_dispatch_matches_workflow_env(
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Conftest dispatch must pick the backend the workflow env implies.

    Runs unconditionally on every lane — assertion branches off the env
    vars to mirror conftest's own dispatch logic. Mismatch means the
    dispatch silently picked a different backend than CI asked for.
    """
    image_path = os.environ.get("HAOS_TEST_IMAGE_PATH")
    mode = os.environ.get("HAOS_TEST_MODE", "")
    backend = ha_container_with_fresh_config["backend"]

    if image_path and mode == "inaddon":
        # haos-e2e-inaddon-tests.yml lane
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
        # haos-e2e-tests.yml (external) lane
        assert backend == "haos", (
            f"Workflow set HAOS_TEST_IMAGE_PATH but dispatch picked "
            f"backend={backend!r}. The lane silently fell through to "
            f"the testcontainer path; tests are running against the "
            f"wrong HA instance."
        )
        # External HAOS sets the testcontainer keys to None.
        assert ha_container_with_fresh_config["container"] is None
        assert ha_container_with_fresh_config["port"] is None
        assert ha_container_with_fresh_config["config_path"] is None
        # addon_mcp_url is the inaddon-only routing key.
        assert ha_container_with_fresh_config["addon_mcp_url"] is None
    else:
        # e2e-tests.yml (testcontainer) lane
        assert backend == "container", (
            f"No HAOS env vars set, expected testcontainer backend, "
            f"got backend={backend!r}."
        )
        assert ha_container_with_fresh_config["container"] is not None
        assert ha_container_with_fresh_config["port"] is not None
