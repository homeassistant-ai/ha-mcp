"""ha_config_get_yaml's glob search on real Home Assistant Core.

A glob first asks the File & YAML Tools entry which files Core's own YAML
loader saw define the key (``find_yaml_key``), then lists the folder for the
files it did not see. The files sit in Core's config directory, and every reply
comes from the entry's real services.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_yaml_read import YamlReadTools

from ._component_filesystem import config_dir, put
from ._file_services import make_file_service
from ._symlink_support import symlink_or_skip
from ._tool_errors import error_of

GetYaml = Callable[..., Awaitable[dict[str, Any]]]

PACKAGES = "homeassistant:\n  packages: !include_dir_named packages\n"
NO_PACKAGES = "homeassistant:\n  name: Test home\n"
# Core cannot load this, so the entry cannot say which files define a key.
UNLOADABLE = "template: !include missing.yaml\n"
TEMPLATE = "template:\n  - trigger: []\n"


@pytest.fixture
async def get_yaml(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> GetYaml:
    await make_file_service(hass)
    return YamlReadTools(component_core_client).ha_config_get_yaml


async def _dangling_link(hass: HomeAssistant, name: str) -> None:
    """An entry the folder listing cannot stat, so the listing fails."""
    link = config_dir(hass) / name
    await hass.async_add_executor_job(symlink_or_skip, link, link.parent / "gone")


def _files(result: dict[str, Any]) -> list[str]:
    return [match["file"] for match in result["matches"]]


def _listing_failed(warning: str) -> bool:
    return warning.startswith("Glob expansion failed: ") and warning.endswith(
        "Files matching the pattern may be missing from this result."
    )


async def test_glob_finds_a_key_in_a_package_subfolder(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """The folder listing is one level deep; Core's loader sees the subfolder."""
    await put(
        hass, {"configuration.yaml": PACKAGES, "packages/nested/mcp.yaml": TEMPLATE}
    )

    out = await get_yaml(yaml_path="template", file="packages/*.yaml")

    assert _files(out) == ["packages/nested/mcp.yaml"]
    assert out["files_searched"] == 1


async def test_glob_leaves_out_files_outside_the_pattern(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """configuration.yaml defines the key too, but the caller asked for packages."""
    await put(
        hass,
        {"configuration.yaml": PACKAGES + TEMPLATE, "packages/mcp.yaml": TEMPLATE},
    )

    out = await get_yaml(yaml_path="template", file="packages/*.yaml")

    assert _files(out) == ["packages/mcp.yaml"]


async def test_bracket_in_a_folder_name_does_not_select_a_sibling_folder(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(
        hass,
        {
            "configuration.yaml": "homeassistant:\n  packages: !include_dir_named conf\n",
            "conf/pack[a]ges/svc.yaml": TEMPLATE,
            "conf/packages/other.yaml": TEMPLATE,
        },
    )

    out = await get_yaml(yaml_path="template", file="conf/pack[a]ges/*.yaml")

    assert _files(out) == ["conf/pack[a]ges/svc.yaml"]


async def _unlistable_packages(hass: HomeAssistant, *, named_file: bool) -> None:
    """Core loads ``svc.yaml`` (and ``[.s]vc.yaml``), and skips the dot file
    ``.vc.yaml``, which the class ``[.s]`` also matches and cannot be listed."""
    files = {"configuration.yaml": PACKAGES, "packages/svc.yaml": TEMPLATE}
    if named_file:
        files["packages/[.s]vc.yaml"] = TEMPLATE
    await put(hass, files)
    await _dangling_link(hass, "packages/.vc.yaml")


async def test_bracketed_file_core_loaded_is_searched_when_the_listing_fails(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """A file named ``[.s]vc.yaml`` cannot match itself as a pattern; keep it."""
    await _unlistable_packages(hass, named_file=True)

    out = await get_yaml(yaml_path="template", file="packages/[.s]vc.yaml")

    assert _files(out) == ["packages/[.s]vc.yaml", "packages/svc.yaml"]


async def test_bracketed_file_core_loaded_is_not_reported_unsearched(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await _unlistable_packages(hass, named_file=True)

    out = await get_yaml(yaml_path="template", file="packages/[.s]vc.yaml")

    (warning,) = out["warnings"]
    assert _listing_failed(warning)


async def test_listing_failure_names_the_bracketed_file_it_could_not_search(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """A named file must not vanish behind a generic listing failure."""
    await _unlistable_packages(hass, named_file=False)

    out = await get_yaml(yaml_path="template", file="packages/[.s]vc.yaml")

    assert _files(out) == ["packages/svc.yaml"]
    listing, unsearched = out["warnings"]
    assert _listing_failed(listing)
    assert (
        unsearched
        == "packages/[.s]vc.yaml was not searched because its listing failed."
    )


async def test_glob_reads_the_folder_when_core_loaded_no_file_with_the_key(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(hass, {"configuration.yaml": NO_PACKAGES, "packages/a.yaml": TEMPLATE})

    out = await get_yaml(yaml_path="template", file="packages/*.yaml")

    assert _files(out) == ["packages/a.yaml"]
    assert "warnings" not in out


async def test_glob_keeps_a_file_core_did_not_load_beside_the_ones_it_did(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """Core loads only ``packages/loaded``; the stray file is still searched."""
    await put(
        hass,
        {
            "configuration.yaml": (
                "homeassistant:\n  packages: !include_dir_named packages/loaded\n"
            ),
            "packages/loaded/zgood.yaml": TEMPLATE,
            "packages/a-unloaded.yaml": TEMPLATE,
        },
    )

    out = await get_yaml(yaml_path="template", file="packages/*.yaml")

    assert _files(out) == ["packages/a-unloaded.yaml", "packages/loaded/zgood.yaml"]


async def test_root_glob_keeps_what_core_found_when_the_root_cannot_be_listed(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """The entry refuses to list the config root; that must not drop the match."""
    await put(hass, {"configuration.yaml": TEMPLATE})

    out = await get_yaml(yaml_path="template", file="*.yaml")

    assert _files(out) == ["configuration.yaml"]
    (warning,) = out["warnings"]
    assert _listing_failed(warning)
    assert "Path not allowed" in warning


async def test_root_glob_that_core_found_nothing_for_warns_instead_of_failing(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(hass, {"configuration.yaml": NO_PACKAGES})

    out = await get_yaml(yaml_path="sensor", file="*.yaml")

    assert out["success"] is True
    assert out["matches"] == []
    (warning,) = out["warnings"]
    assert _listing_failed(warning)
    assert "Path not allowed" in warning


async def test_glob_warns_that_packages_may_be_missing_when_core_cannot_load(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(hass, {"configuration.yaml": UNLOADABLE, "packages/a.yaml": TEMPLATE})

    out = await get_yaml(yaml_path="template", file="packages/*.yaml")

    assert _files(out) == ["packages/a.yaml"]
    assert any(
        "packages or subfolders may be missing" in warning
        for warning in out["warnings"]
    )


async def test_glob_fails_when_core_cannot_load_and_the_folder_cannot_be_listed(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """Without Core's answer the listing is the only route, so its failure raises."""
    await put(hass, {"configuration.yaml": UNLOADABLE})

    with pytest.raises(ToolError, match="Path not allowed"):
        await get_yaml(yaml_path="rest", file="*.yaml")


async def test_named_bracketed_file_core_loaded_raises_on_a_parse_error(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """A duplicate key loads in Core but not in the round-trip parser; the
    caller named this file, so the failure must not soften to a warning."""
    await put(
        hass,
        {
            "configuration.yaml": PACKAGES,
            "packages/svc[a].yaml": "rest:\n  a: 1\nrest:\n  b: 2\n",
        },
    )

    with pytest.raises(ToolError) as exc:
        await get_yaml(yaml_path="rest", file="packages/svc[a].yaml")

    assert error_of(exc)["code"] == "CONFIG_INVALID"


async def test_a_key_merged_in_from_a_folder_is_flagged_and_readable_locally(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """The package writes ``shell_command`` and a folder supplies its keys:
    the dotted path is in no single file, which the result has to say. The
    key is readable from its own file under its local name."""
    await put(
        hass,
        {
            "configuration.yaml": PACKAGES,
            "packages/merged.yaml": "shell_command: !include_dir_merge_named ../themes\n",
            "themes/commands.yaml": "mcp_secret: echo hi\n",
        },
    )

    out = await get_yaml(yaml_path="shell_command.mcp_secret", file="packages/*.yaml")

    assert out["matches"] == []
    assert any(
        warning.startswith("packages/merged.yaml is annotated as defining")
        for warning in out["warnings"]
    )
    local = await get_yaml(yaml_path="mcp_secret", file="themes/*.yaml")
    assert _files(local) == ["themes/commands.yaml"]


async def test_failed_lookup_of_a_bracketed_name_reports_it_unsearched(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """The literal ``svc[a].yaml`` cannot be listed; the class match
    ``svca.yaml`` from the same expansion is dropped with it."""
    await put(
        hass,
        {"configuration.yaml": NO_PACKAGES, "packages/svca.yaml": "rest:\n  a: 1\n"},
    )
    await _dangling_link(hass, "packages/svc[a].yaml")

    out = await get_yaml(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["success"] is True
    assert out["matches"] == []
    listing, unsearched = out["warnings"]
    assert _listing_failed(listing)
    assert (
        unsearched
        == "packages/svc[a].yaml was not searched because its listing failed."
    )
