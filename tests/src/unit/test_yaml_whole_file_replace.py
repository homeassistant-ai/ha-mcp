"""Unit tests for the whole-file ``replace_file`` action of edit_yaml_config.

``replace_file`` restores a pre-#1579 ``.bak`` wholesale: it bypasses the
per-key merge and writes the supplied content verbatim, while reusing the same
path allowlist, YAML validation, atomic write, and config check as a normal
edit (#1579).

Core sets the File & YAML Tools entry up in a real ``hass`` whose config
directory is the test's ``tmp_path``, and runs its own config check after
every write.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.config import async_check_ha_config_file
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ha_mcp_tools import (
    SERVICE_EDIT_YAML_CONFIG,
)
from custom_components.ha_mcp_tools.const import DOMAIN

from ._edit_yaml import EditYaml, make_edit_yaml


@pytest.fixture
async def edit_yaml(hass: HomeAssistant) -> EditYaml:
    """Call ``ha_mcp_tools.edit_yaml_config`` the way ha-mcp does."""
    return await make_edit_yaml(hass)


def _replace_file(file: str, content: str) -> dict[str, Any]:
    return {
        "file": file,
        "action": "replace_file",
        "yaml_path": "",
        "content": content,
    }


class TestReplaceFileAction:
    """handle_edit_yaml_config(action="replace_file") whole-file restore."""

    async def test_replaces_whole_configuration_yaml_verbatim(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """The entire file is overwritten with the supplied content byte-for-byte,
        comments preserved — keys absent from the new content are gone."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("default_config:\nold_key: 1\n")

        # Core's config check finds nothing to load in this content.
        new_content = "# restored\nhomeassistant:\n  name: Restored\n"
        result = await edit_yaml(_replace_file("configuration.yaml", new_content))

        assert result["success"] is True, result
        assert result["action"] == "replace_file"
        # ``written`` is the shared write-path discriminator (never a preview).
        assert result["written"] is True
        assert result["post_action"] == "restart_required"
        assert result["config_check"] == "ok"
        # Verbatim — comment kept, old_key dropped.
        assert cfg.read_text() == new_content
        assert "old_key" not in cfg.read_text()

    async def test_writes_packages_file(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """packages/*.yaml is in the write allowlist and restorable."""
        pkg = tmp_path / "packages" / "lights.yaml"
        pkg.parent.mkdir(parents=True)
        pkg.write_text("light: []\n")

        result = await edit_yaml(_replace_file("packages/lights.yaml", "switch: []\n"))

        assert result["success"] is True, result
        assert pkg.read_text() == "switch: []\n"

    async def test_accepts_include_dir_merge_named_tag(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """The documented ``!include_dir_merge_named`` tag must validate and
        restore through replace_file. Regression guard for the tag missing
        from the YAML loader registry, which made ``make_yaml().load()`` raise
        ``ConstructorError`` (surfaced as "Invalid YAML content")."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("default_config:\n")

        new_content = (
            "default_config:\nfrontend:\n  themes: !include_dir_merge_named themes/\n"
        )
        result = await edit_yaml(_replace_file("configuration.yaml", new_content))

        assert result["success"] is True, result
        # The tag survived verbatim in the restored file.
        assert "!include_dir_merge_named themes/" in cfg.read_text()

    async def test_requires_content(self, edit_yaml: EditYaml) -> None:
        result = await edit_yaml(
            {"file": "configuration.yaml", "action": "replace_file", "yaml_path": ""}
        )
        assert result["success"] is False
        assert "content" in result["error"]

    async def test_rejects_non_mapping_root(self, edit_yaml: EditYaml) -> None:
        """A list/scalar root is not a valid config file."""
        result = await edit_yaml(_replace_file("configuration.yaml", "- a\n- b\n"))
        assert result["success"] is False
        assert "mapping" in result["error"]

    async def test_rejects_invalid_yaml(self, edit_yaml: EditYaml) -> None:
        result = await edit_yaml(
            _replace_file("configuration.yaml", "a: [unterminated\n")
        )
        assert result["success"] is False
        assert "Invalid YAML" in result["error"]

    async def test_rejects_disallowed_path(self, edit_yaml: EditYaml) -> None:
        """Whole-file replace is confined to the same allowlist as a normal edit
        (configuration.yaml / packages/*.yaml / themes/*.yaml) — www/ is out."""
        result = await edit_yaml(_replace_file("www/sneaky.yaml", "a: 1\n"))
        assert result["success"] is False
        assert "not allowed" in result["error"]

    async def test_rejects_path_traversal(self, edit_yaml: EditYaml) -> None:
        result = await edit_yaml(_replace_file("../secrets.yaml", "a: 1\n"))
        assert result["success"] is False
        assert "traversal" in result["error"].lower()

    @pytest.mark.usefixtures("edit_yaml")
    async def test_unauthorized_without_token(
        self, tmp_path: Path, hass: HomeAssistant
    ) -> None:
        """No caller token → structured unauthorized, no write."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("default_config:\n")

        result = await hass.services.async_call(
            DOMAIN,
            SERVICE_EDIT_YAML_CONFIG,
            _replace_file("configuration.yaml", "template: []\n"),
            blocking=True,
            return_response=True,
        )

        assert result is not None
        assert result["success"] is False
        assert result.get("error_code") == "unauthorized"
        # File untouched.
        assert cfg.read_text() == "default_config:\n"

    async def test_surfaces_config_check_errors(
        self, tmp_path: Path, hass: HomeAssistant, edit_yaml: EditYaml
    ) -> None:
        """A failing config check is reported but the (already atomic) write stands."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("default_config:\n")
        invalid = "homeassistant:\n  latitude: north\n"

        result = await edit_yaml(_replace_file("configuration.yaml", invalid))

        assert result["success"] is True
        assert result["config_check"] == "errors"
        # Core's own verdict on the written file, passed through unchanged.
        assert result["config_check_errors"] == await async_check_ha_config_file(hass)
        assert cfg.read_text() == invalid

    async def test_surfaces_config_check_unavailable_on_failure(
        self, tmp_path: Path, edit_yaml: EditYaml, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config check that cannot run surfaces as "unavailable" while the
        (already atomic) write still stands. Behavioral guard for the #1660
        locus — the swallowed-exception branch that the source-string guard
        below cannot catch.

        Core's check reports a broken file as errors rather than raising, so the
        failure is raised here.
        """
        monkeypatch.setattr(
            "custom_components.ha_mcp_tools.async_check_ha_config_file",
            AsyncMock(side_effect=HomeAssistantError("check boom")),
        )
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("default_config:\n")

        result = await edit_yaml(_replace_file("configuration.yaml", "template: []\n"))

        assert result["success"] is True
        assert result["config_check"] == "unavailable", result
        assert "check boom" in result.get("config_check_error", ""), result
        assert cfg.read_text() == "template: []\n"
