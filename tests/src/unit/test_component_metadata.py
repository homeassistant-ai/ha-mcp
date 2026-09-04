"""Static compatibility checks for the HACS custom component."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]


def test_hacs_requires_supported_home_assistant_version() -> None:
    """The floor must be one every manifest requirement can resolve on.

    Core installs a custom integration's requirements under its own
    ``package_constraints.txt``, so a requirement the constraints contradict
    fails to install and the integration does not load at all -- tools-only
    entries included, since requirements are installed before the entry type
    is known.

    Across all 106 release tags from 2024.11.0 to 2026.9.0, ``2026.7.0`` is
    the first whose pin -- ``voluptuous-openapi==0.4.1`` -- the manifest
    requirement resolves against; every earlier release pins a lower version,
    and 2026.9.0 is the only one with no such pin at all, Core having moved to
    ``probatio``. The floor sits deliberately one release above that minimum
    rather than at it (#2361).
    """
    metadata = json.loads((_REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))

    # The runtime floor the config flow enforces for the in-process server
    # entry must not admit a Core the integration cannot load on. const.py is
    # loaded by path: importing the package would pull in homeassistant.
    const_path = _REPO_ROOT / "custom_components" / "ha_mcp_tools" / "const.py"
    spec = importlib.util.spec_from_file_location("ha_mcp_tools_const", const_path)
    assert spec is not None and spec.loader is not None
    const = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(const)
    assert metadata["homeassistant"] == const.MIN_EMBEDDED_HOME_ASSISTANT_VERSION

    assert metadata["homeassistant"] == "2026.8.0"


def test_manifest_declares_the_pre_probatio_schema_converter() -> None:
    """The ingest layer is a declared requirement, not an accident of Core.

    ``llm_api._schema_converter`` prefers ``voluptuous_openapi`` and falls back
    to ``probatio.from_openapi``, whose OpenAPI codec cannot express the node
    it builds for an integer -- a numeric parameter then reaches the agent as a
    string, an empty schema or a plain number. Core 2026.9 stopped shipping
    voluptuous-openapi, so from there the manifest requirement is the only
    thing that installs it: dropping the requirement as an unused dependency
    would put that fallback back in the path with every test still green
    (#2361).

    Asserted beside the HACS floor above because the two constrain each other:
    Core resolves this requirement against the constraints of the oldest
    release the floor admits.
    """
    manifest = json.loads(
        (_REPO_ROOT / "custom_components" / "ha_mcp_tools" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert "voluptuous-openapi>=0.4.1" in manifest["requirements"]
