"""The storage-helper types ha-mcp manages match the installed frontend (#2632).

The frontend's ``HELPER_DOMAINS`` (``src/panels/config/helpers/const.ts``) is
the list of storage helpers Home Assistant's helper settings offer; ha-mcp keeps
a copy, plus zone, person and tag, which have their own frontend pages. Read
from the frontend bundle inside the Home Assistant container, so the nightly
beta lane fails when a frontend release adds or drops a helper type.
"""

import json
from typing import Any

import pytest

from ha_mcp.tools.config_helpers.schemas import SIMPLE_HELPER_TYPES

pytestmark = pytest.mark.container_only

# Every distinct domain-name array in the bundle that opens with
# "input_boolean"; HELPER_DOMAINS is the only one.
_READ_HELPER_DOMAINS = r"""
import json, pathlib, re
import hass_frontend
root = pathlib.Path(hass_frontend.where()) / "frontend_latest"
pattern = re.compile(rb'\["input_boolean"(?:,"[a-z_]+")+\]')
found = {m for p in root.glob("*.js") for m in pattern.findall(p.read_bytes())}
print(json.dumps(sorted(json.loads(m) for m in found)))
"""

# Storage collections with their own frontend pages, managed by the helper tools.
_OWN_PAGE_TYPES = {"zone", "person", "tag"}


def test_helper_types_match_the_installed_frontend(
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """A helper type the frontend adds is a type ha-mcp cannot yet route."""
    container = ha_container_with_fresh_config["container"]
    result = container.get_wrapped_container().exec_run(
        ["python3", "-c", _READ_HELPER_DOMAINS]
    )
    output = (result.output or b"").decode("utf-8", "replace")
    assert result.exit_code == 0, f"reading the frontend bundle failed: {output[:500]}"

    arrays = json.loads(output)
    assert len(arrays) == 1, (
        f"expected one HELPER_DOMAINS array in the frontend bundle, found {arrays}"
    )
    assert set(arrays[0]) == SIMPLE_HELPER_TYPES - _OWN_PAGE_TYPES
