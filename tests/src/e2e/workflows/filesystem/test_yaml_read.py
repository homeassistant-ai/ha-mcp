"""
End-to-End tests for the read-only YAML fragment lookup (ha_config_get_yaml).

This test suite validates:
- Registration without the YAML-*editing* feature flag (reading is not editing)
- Single-file fragment reads addressed by file + yaml_path
- Cross-file key discovery through a glob, against the NON-default packages
  folder the e2e config binds (``custom_packages``, see initial_test_state/
  configuration.yaml) — which is exactly the runtime-detection path
- A glob that matches several defining files, and one that matches a file the
  component refuses to read (warned about, search continues)
- HA tags surviving unresolved into both the text and the parsed view
- secrets.yaml reading back masked through those views
- The get → set round-trip that is the point of the feature

These tests require the ha_mcp_tools custom component to be installed in Home
Assistant. ha_config_get_yaml hangs on ``enable_filesystem_tools`` (it returns
config-file contents) but NOT on ENABLE_YAML_CONFIG_EDITING; that flag is
enabled here only to SEED a package file to then discover.

Tests are designed for the Docker Home Assistant test environment.
"""

import logging
import os
from typing import Any

import pytest

from ...utilities.assertions import MCPAssertions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOOL_NAME = "ha_config_get_yaml"
SET_TOOL = "ha_config_set_yaml"
READ_TOOL = "ha_read_file"
FEATURE_FLAG = "ENABLE_YAML_CONFIG_EDITING"

# initial_test_state/configuration.yaml binds packages under this NON-default
# folder on purpose (#1854), so a glob over it only resolves if the component
# detects the configured folder at runtime rather than assuming "packages".
PACKAGES_DIR = "custom_packages"

# Staged pre-boot by conftest._seed_non_yaml_package_file — the file a
# `custom_packages/*` glob must skip rather than fail on.
NON_YAML_FILE = f"{PACKAGES_DIR}/_e2e_not_yaml.md"


def _require_seeded_backend(container_info: dict) -> None:
    """Skip unless the non-YAML package file was staged into the config_path.

    ``_seed_non_yaml_package_file`` runs on the shared testcontainer setup path,
    used by the ``container`` and ``embedded`` backends; the HAOS backends boot a
    pre-baked qcow2 that carries no such seed. Nothing else can create the file —
    package folders are readable but never writable through the file tools — so
    the test skips rather than seeding at runtime.
    """
    if container_info.get("backend") not in ("container", "embedded"):
        pytest.skip(
            "the non-YAML package file is staged pre-boot into the container "
            "config_path (testcontainer / embedded backends only)"
        )


@pytest.fixture(scope="module")
def yaml_editing_enabled(ha_container_with_fresh_config):
    """Enable YAML editing for the module — only needed to seed a package."""
    os.environ[FEATURE_FLAG] = "true"
    yield
    os.environ.pop(FEATURE_FLAG, None)


async def _set_yaml_confirmed(mcp: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Drive ha_config_set_yaml's default-on two-step confirm flow to a write.

    Local to this module: a sibling test file's module-level helper is not
    importable as a fixture, and this only needs the write half.
    """
    data = await mcp.call_tool_success(SET_TOOL, args)
    if data.get("preview"):
        data = await mcp.call_tool_success(
            SET_TOOL, {**args, "confirm_token": data["confirm_token"]}
        )
    return data


@pytest.mark.filesystem
class TestYamlReadAvailability:
    """ha_config_get_yaml is registered regardless of the *editing* flag."""

    async def test_registered_without_editing_flag(self, mcp_client):
        """The read tool must be present even with YAML editing off.

        That independence is the reason it lives in its own module — reading a
        fragment is not an edit. (It does hang on enable_filesystem_tools; the
        unit suite pins that gate, which env changes here cannot exercise
        against an already-running server.)
        """
        original = os.environ.pop(FEATURE_FLAG, None)
        try:
            tools = await mcp_client.list_tools()
            assert TOOL_NAME in {t.name for t in tools}, (
                f"{TOOL_NAME} must register without {FEATURE_FLAG}"
            )
        finally:
            if original:
                os.environ[FEATURE_FLAG] = original


@pytest.mark.filesystem
class TestYamlReadSingleFile:
    """Fragment reads addressed exactly like ha_config_set_yaml."""

    async def test_reads_key_from_configuration_yaml(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME, {"yaml_path": "http", "file": "configuration.yaml"}
            )

        assert data["count"] == 1
        assert data["files_searched"] == 1
        match = data["matches"][0]
        assert match["file"] == "configuration.yaml"
        assert match["yaml_path"] == "http"
        # The http block of initial_test_state/configuration.yaml.
        assert "use_x_forwarded_for" in match["content"]

    async def test_absent_key_is_an_empty_result_not_an_error(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {"yaml_path": "no_such_key_here", "file": "configuration.yaml"},
            )

        assert data["success"] is True
        assert data["matches"] == []
        assert data["count"] == 0
        # The file WAS searched — distinguishes this from a glob that matched
        # no files at all.
        assert data["files_searched"] == 1

    async def test_include_content_false_omits_bodies(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "http",
                    "file": "configuration.yaml",
                    "include_content": False,
                },
            )

        assert data["count"] == 1
        assert "content" not in data["matches"][0]

    async def test_include_parsed_returns_structured_data(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "http",
                    "file": "configuration.yaml",
                    "include_parsed": True,
                },
            )

        parsed = data["matches"][0]["parsed"]
        assert isinstance(parsed, dict)
        assert parsed["use_x_forwarded_for"] is True


@pytest.mark.filesystem
class TestYamlReadTagPreservation:
    """HA tags reach the caller unresolved — the property that keeps a
    ``!secret`` from ever being dereferenced by this read path."""

    async def test_include_tag_not_resolved(self, mcp_client):
        """configuration.yaml has ``automation: !include automations.yaml``.

        Both views must show the tag as written. If either ever resolved the
        include, the same machinery would resolve ``!secret`` to plaintext.
        """
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "automation",
                    "file": "configuration.yaml",
                    "include_parsed": True,
                },
            )

        match = data["matches"][0]
        assert "!include automations.yaml" in match["content"]
        assert match["parsed"] == "!include automations.yaml"


@pytest.mark.filesystem
class TestReadFileYamlPath:
    """`ha_read_file` forwards yaml_path for the single-file case."""

    async def test_yaml_path_returns_subtree(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                READ_TOOL, {"path": "configuration.yaml", "yaml_path": "http"}
            )

        assert "use_x_forwarded_for" in data["subtree"]

    async def test_yaml_path_extracts_from_the_untailed_file(self, mcp_client):
        """tail_lines must not truncate the text the key is extracted from.

        `http:` sits near the top of configuration.yaml, so a tail of a few
        lines excludes it entirely, and the retained tail is not valid YAML on
        its own. Extracting from the tailed text would report the key as
        absent; the subtree must still come back, while `content` stays tailed.
        """
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                READ_TOOL,
                {"path": "configuration.yaml", "tail_lines": 3, "yaml_path": "http"},
            )

        assert data["subtree"] is not None, (
            "yaml_path must extract from the full file, not the tailed text"
        )
        assert "use_x_forwarded_for" in data["subtree"]
        # content stays truncated: tailing is a display concern only.
        assert len(data["content"].split("\n")) <= 3


@pytest.mark.filesystem
class TestSecretsMasking:
    """secrets.yaml reads back masked through the yaml_path views too.

    The unit half (test_custom_component_filesystem.py::
    TestReadFileSecretsMaskingOrder) pins the ordering that makes this hold;
    this is the behavioural half against the real component.
    """

    async def test_subtree_and_parsed_are_masked(self, mcp_client):
        """initial_test_state/secrets.yaml holds ``some_password: welcome``."""
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "some_password",
                    "file": "secrets.yaml",
                    "include_parsed": True,
                },
            )

        match = data["matches"][0]
        assert "welcome" not in match["content"]
        assert "[MASKED]" in match["content"]
        assert match["parsed"] == "[MASKED]"


@pytest.mark.filesystem
class TestYamlReadRoundTrip:
    """#1788's core promise: a match feeds straight back into set_yaml."""

    async def test_read_content_is_accepted_by_set_yaml(
        self, mcp_client, yaml_editing_enabled
    ):
        """Seed a tag-bearing key, read it, hand the content back to set_yaml.

        The read `content` carries an unresolved ``!secret``. If set_yaml
        rejected or mangled tag-bearing subtree text, the feature would be
        broken end-to-end with nothing else failing — the two tools would
        simply not compose.
        """
        target = f"{PACKAGES_DIR}/_e2e_roundtrip.yaml"
        async with MCPAssertions(mcp_client) as mcp:
            await _set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "shell_command",
                    "action": "add",
                    "file": target,
                    "content": "e2e_roundtrip_probe: !secret some_password\n",
                },
            )

            read = await mcp.call_tool_success(
                TOOL_NAME, {"yaml_path": "shell_command", "file": target}
            )
            match = read["matches"][0]
            assert "!secret some_password" in match["content"], (
                "the tag must survive the read for the round-trip to mean anything"
            )

            # The whole point: file + yaml_path + content address the same
            # fragment for the editor, unchanged.
            await _set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": match["yaml_path"],
                    "action": "replace",
                    "file": match["file"],
                    "content": match["content"],
                },
            )

            after = await mcp.call_tool_success(
                TOOL_NAME, {"yaml_path": "shell_command", "file": target}
            )

        # Round-tripped through the editor without mangling the tag.
        assert after["matches"][0]["content"] == match["content"]
        assert "!secret some_password" in after["matches"][0]["content"]


@pytest.mark.filesystem
class TestYamlReadGlobDiscovery:
    """The issue's core ask: find which file defines a key."""

    async def test_glob_discovers_defining_file(self, mcp_client, yaml_editing_enabled):
        """Seed two package files, then discover the one defining the key.

        Globs ``custom_packages/*.yaml`` — the folder is bound via
        ``!include_dir_named custom_packages``, so listing it works only
        because the component resolves the configured packages folder.
        """
        async with MCPAssertions(mcp_client) as mcp:
            await _set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "command_line",
                    "action": "add",
                    "file": f"{PACKAGES_DIR}/_e2e_read_hit.yaml",
                    "content": "- sensor:\n    name: e2e_read_probe\n    command: echo 1\n",
                },
            )
            await _set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "shell_command",
                    "action": "add",
                    "file": f"{PACKAGES_DIR}/_e2e_read_miss.yaml",
                    "content": "e2e_read_noop: echo 2\n",
                },
            )

            data = await mcp.call_tool_success(
                TOOL_NAME,
                {"yaml_path": "command_line", "file": f"{PACKAGES_DIR}/*.yaml"},
            )

        # Only the file that defines command_line matches, but both were read.
        assert data["count"] == 1
        assert data["files_searched"] >= 2
        match = data["matches"][0]
        assert match["file"] == f"{PACKAGES_DIR}/_e2e_read_hit.yaml"
        assert "e2e_read_probe" in match["content"]
        # file + yaml_path address the same fragment for ha_config_set_yaml.
        assert match["yaml_path"] == "command_line"

    async def test_glob_returns_every_defining_file(
        self, mcp_client, yaml_editing_enabled
    ):
        """Two files defining the same key both come back — #1788's core shape.

        "Which file defines this key?" has more than one answer whenever a key is
        split across packages, and the caller needs each ``file`` to address the
        right fragment for an edit.
        """
        first = f"{PACKAGES_DIR}/_e2e_multi_a.yaml"
        second = f"{PACKAGES_DIR}/_e2e_multi_b.yaml"
        async with MCPAssertions(mcp_client) as mcp:
            for target, marker in ((first, "a"), (second, "b")):
                await _set_yaml_confirmed(
                    mcp,
                    {
                        "yaml_path": "notify",
                        "action": "add",
                        "file": target,
                        "content": (
                            f"- name: e2e_multi_{marker}\n"
                            "  platform: command_line\n"
                            f"  command: echo {marker}\n"
                        ),
                    },
                )

            data = await mcp.call_tool_success(
                TOOL_NAME, {"yaml_path": "notify", "file": f"{PACKAGES_DIR}/*.yaml"}
            )

        assert data["count"] == 2
        # Sorted by file, so each match's content pairs with its own file.
        assert [m["file"] for m in data["matches"]] == [first, second]
        assert "e2e_multi_a" in data["matches"][0]["content"]
        assert "e2e_multi_b" in data["matches"][1]["content"]

    async def test_unreadable_file_warns_without_sinking_the_search(
        self, mcp_client, ha_container_with_fresh_config, yaml_editing_enabled
    ):
        """``custom_packages/*`` matches a non-YAML file; the search survives it.

        The behavioural half of the warn-and-continue path the unit suite pins
        against mocks: the refusal has to come from the real component, whose
        package-dir read rule requires ``.yaml``. The skipped file is named in
        ``warnings`` rather than silently dropped — a skip reported as "key not
        here" would be a wrong answer to "which file defines it?".
        """
        _require_seeded_backend(ha_container_with_fresh_config)
        hit = f"{PACKAGES_DIR}/_e2e_read_warn.yaml"
        async with MCPAssertions(mcp_client) as mcp:
            await _set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "group",
                    "action": "add",
                    "file": hit,
                    "content": "e2e_warn_probe:\n  entities:\n    - sun.sun\n",
                },
            )

            data = await mcp.call_tool_success(
                TOOL_NAME, {"yaml_path": "group", "file": f"{PACKAGES_DIR}/*"}
            )

        # The sibling match survives the unreadable file.
        assert data["count"] == 1
        assert data["matches"][0]["file"] == hit
        assert "e2e_warn_probe" in data["matches"][0]["content"]

        # Named once, as a skip carrying the component's refusal — not as a
        # stray mention inside some other warning.
        skipped = [w for w in data.get("warnings", []) if NON_YAML_FILE in w]
        assert len(skipped) == 1, data.get("warnings")
        assert skipped[0].startswith(
            f"{NON_YAML_FILE} was not searched: Path not allowed."
        ), skipped
        # Both the skipped file and the match were targets of the search.
        assert data["files_searched"] >= 2
