"""ha_config_get_yaml on real Home Assistant Core: a file name with brackets.

``packages/svc[a].yaml`` is both a pattern and a legal file name, and as a
pattern it cannot match itself, so the tool lists the folder for both
readings. A file the caller named exactly keeps the strict contract (a failed
read raises); a file a pattern turned up only warns. Every reply comes from
the File & YAML Tools entry's own services.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._component_filesystem import config_dir, put
from ._symlink_support import symlink_or_skip
from ._tool_errors import error_of
from ._yaml_read_tool_core import (
    FAILS_TO_READ,
    NOT_YAML,
    NOT_YAML_ERROR,
    GetYaml,
    found,
    read_failed,
    yaml_tool,
)

REST = "rest:\n  a: 1\n"


@pytest.fixture
async def get_yaml(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> GetYaml:
    return await yaml_tool(hass, component_core_client)


async def _packages(hass: HomeAssistant, files: dict[str, str]) -> None:
    await put(hass, {f"packages/{name}": text for name, text in files.items()})


async def test_bracketed_name_finds_the_file_called_that(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """As a pattern ``svc[a].yaml`` matches only ``svca.yaml``, not itself."""
    await _packages(hass, {"svc[a].yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["files_searched"] == 1
    assert found(out) == ["packages/svc[a].yaml"]


async def test_bracketed_name_searches_the_file_and_the_sibling_it_matches(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """The sibling must not stand in for the file the caller named."""
    await _packages(hass, {"svc[a].yaml": REST, "svca.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["files_searched"] == 2
    assert found(out) == ["packages/svc[a].yaml", "packages/svca.yaml"]


async def test_only_a_bracketed_name_lists_the_folder_twice(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    get_yaml: GetYaml,
) -> None:
    await _packages(hass, {"svc[a].yaml": REST, "lights.yaml": REST})

    with patch.object(
        component_core_client, "call_service", wraps=component_core_client.call_service
    ) as calls:
        await get_yaml(yaml_path="rest", file="packages/nothing*.yaml")
        await get_yaml(yaml_path="rest", file="packages/svc[a].yaml")

    listed = [
        call.args[2]["pattern"]
        for call in calls.await_args_list
        if call.args[1] == "list_files"
    ]
    assert listed[0] == "nothing*.yaml"
    assert sorted(listed[1:]) == ["svc[[]a].yaml", "svc[a].yaml"]


@pytest.mark.parametrize(
    ("files", "file"),
    [
        pytest.param({"svc[a].yaml": NOT_YAML}, "packages/svc[a].yaml", id="alone"),
        pytest.param(
            {"svc[a].yaml": NOT_YAML, "svca.yaml": REST},
            "packages/svc[a].yaml",
            id="beside-a-sibling-the-class-matches",
        ),
        pytest.param(
            {"svc[a].yaml": NOT_YAML},
            "./packages/svc[a].yaml",
            id="spelled-through-the-current-folder",
        ),
    ],
)
async def test_named_bracketed_file_that_will_not_parse_raises(
    hass: HomeAssistant, get_yaml: GetYaml, files: dict[str, str], file: str
) -> None:
    """Strictness belongs to the file the caller named, whatever sits beside it."""
    await _packages(hass, files)

    with pytest.raises(ToolError) as exc:
        await get_yaml(yaml_path="rest", file=file)

    assert error_of(exc)["code"] == "CONFIG_INVALID"


async def test_named_bracketed_file_whose_read_fails_raises_beside_a_sibling(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """Reporting the sibling with a warning would answer a question nobody asked."""
    await _packages(hass, {"svc[a].yaml": FAILS_TO_READ, "svca.yaml": REST})

    with pytest.raises(ToolError, match="500"):
        await get_yaml(yaml_path="rest", file="packages/svc[a].yaml")


async def test_file_named_like_the_glob_is_still_a_glob_match(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """``*.yaml`` matches a file literally called ``*.yaml``; nobody named it,
    so its failure must not discard the matches already found."""
    await _packages(hass, {"*.yaml": NOT_YAML, "a.yaml": REST, "b.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/*.yaml")

    assert out["count"] == 2
    assert out["files_searched"] == 3
    assert out["warnings"] == [f"packages/*.yaml was not searched: {NOT_YAML_ERROR}."]


async def test_bracketed_folder_is_a_folder_name(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await put(hass, {"www/pack[a]ges/svc.yaml": REST, "www/packages/svc.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="www/pack[a]ges/svc.yaml")

    assert found(out) == ["www/pack[a]ges/svc.yaml"]
    assert out["files_searched"] == 1


async def test_missing_file_in_a_bracketed_folder_raises(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """A typo must not come back as a silent empty result."""
    await put(hass, {"www/packages/svc.yaml": REST})

    with pytest.raises(ToolError, match=r"File does not exist: www/pack\[a\]ges"):
        await get_yaml(yaml_path="rest", file="www/pack[a]ges/svc.yaml")


async def test_absent_bracketed_file_is_reported_not_implied(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """Without a word ``files_searched: 1`` reads as "your file has no such key"."""
    await _packages(hass, {"svca.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["files_searched"] == 1
    assert out["count"] == 1
    assert out["warnings"] == [
        "No file is literally named packages/svc[a].yaml; searched the 1 "
        "file(s) matching it as a pattern."
    ]
    # The warning is the resolver's; the one read succeeded.
    assert out["files_unreadable"] == 0


async def test_dangling_link_with_the_bracketed_name_is_a_missing_file(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """The literal lookup lists the link, so the named file is read and is not
    there."""
    await _packages(hass, {"svca.yaml": REST})
    folder = config_dir(hass) / "packages"
    symlink_or_skip(folder / "svc[a].yaml", folder / "gone.yaml")

    with pytest.raises(ToolError, match=r"File does not exist: packages/svc\[a\]"):
        await get_yaml(yaml_path="rest", file="packages/svc[a].yaml")


async def test_bracket_class_searches_every_file_it_matches(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await _packages(hass, {"svc1.yaml": REST, "svc2.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/svc[12].yaml")

    assert found(out) == ["packages/svc1.yaml", "packages/svc2.yaml"]
    assert out["warnings"] == [
        "No file is literally named packages/svc[12].yaml; searched the 2 "
        "file(s) matching it as a pattern."
    ]


async def test_unclosed_bracket_is_one_file(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """Both listings return it, and it is read once."""
    await _packages(hass, {"svc[a.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/svc[a.yaml")

    assert out["files_searched"] == 1
    assert found(out) == ["packages/svc[a.yaml"]
    assert "warnings" not in out


async def test_unclosed_bracket_file_whose_read_fails_raises(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """The caller named it exactly, so ``success: true, count: 0`` would lie."""
    await _packages(hass, {"svc[a.yaml": FAILS_TO_READ})

    with pytest.raises(ToolError, match="500"):
        await get_yaml(yaml_path="rest", file="packages/svc[a.yaml")


async def test_absent_file_warning_and_failed_read_warning_both_reach_the_caller(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """Only the failed read counts as an unreadable file."""
    await _packages(hass, {"svca.yaml": FAILS_TO_READ, "svcb.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/svc[ab].yaml")

    assert found(out) == ["packages/svcb.yaml"]
    assert out["files_searched"] == 2
    assert out["files_unreadable"] == 1
    absent, failed = out["warnings"]
    assert absent == (
        "No file is literally named packages/svc[ab].yaml; searched the 2 "
        "file(s) matching it as a pattern."
    )
    assert read_failed("packages/svca.yaml", failed)


async def test_bracketed_name_matching_nothing_says_so(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """Silence would make a typo look like a key that is simply not defined."""
    await _packages(hass, {"other.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["success"] is True
    assert out["files_searched"] == 0
    assert out["files_unreadable"] == 0
    assert out["warnings"] == [
        "No file is literally named packages/svc[a].yaml, and nothing matched "
        "it as a pattern either."
    ]


async def test_bracket_beside_a_star_names_no_file_so_nothing_is_missing(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    await _packages(hass, {"a1.yaml": REST, "b1.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/[ab]*.yaml")

    assert out["files_searched"] == 2
    assert "warnings" not in out


async def test_file_called_like_a_bracket_and_star_pattern_is_a_glob_match(
    hass: HomeAssistant, get_yaml: GetYaml
) -> None:
    """Under ``*`` even an exact hit was not named, so its failed read warns."""
    await _packages(hass, {"[ab]*.yaml": FAILS_TO_READ, "a1.yaml": REST})

    out = await get_yaml(yaml_path="rest", file="packages/[ab]*.yaml")

    assert found(out) == ["packages/a1.yaml"]
    assert out["files_searched"] == 2
    assert out["files_unreadable"] == 1
    (warning,) = out["warnings"]
    assert read_failed("packages/[ab]*.yaml", warning)
