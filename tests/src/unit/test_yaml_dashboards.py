"""Unit tests for YAML-mode dashboard validators in ha_mcp_tools.

The handler tests call the service on a real ``hass`` whose config directory
is the test's ``tmp_path``.
"""

import json
from pathlib import Path

import pytest
import yaml
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools.const import (
    DASHBOARD_URL_PATH_PATTERN,
    RESERVED_DASHBOARD_URL_PATHS,
)

from ._edit_yaml import EditYaml, make_edit_yaml


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core's config directory is this test's ``tmp_path``."""
    return hass_tmp_config_dir


@pytest.fixture
async def edit_yaml(hass: HomeAssistant) -> EditYaml:
    """Call ``ha_mcp_tools.edit_yaml_config`` the way ha-mcp does."""
    return await make_edit_yaml(hass)


class TestDashboardUrlPathPattern:
    """url_path must match HA's lovelace dashboard rules: lowercase, hyphenated."""

    @pytest.mark.parametrize(
        "url_path",
        [
            "energy-dashboard",
            "my-dashboard",
            "main-view",
            "a-b",
            "long-multi-segment-path",
            "with-123-numbers",
        ],
    )
    def test_accepts_valid_url_paths(self, url_path):
        assert DASHBOARD_URL_PATH_PATTERN.fullmatch(url_path), url_path

    @pytest.mark.parametrize(
        "url_path",
        [
            "",  # empty
            "no_underscore",  # underscores not allowed
            "NoUpper",  # uppercase not allowed
            "single",  # must contain a hyphen
            "-leading-hyphen",  # cannot start with hyphen
            "trailing-hyphen-",  # cannot end with hyphen
            "double--hyphen",  # no consecutive hyphens
            "has space",  # no spaces
            "has/slash",  # no slashes
            "has.dot",  # no dots
            "..",  # path traversal-ish
        ],
    )
    def test_rejects_invalid_url_paths(self, url_path):
        assert not DASHBOARD_URL_PATH_PATTERN.fullmatch(url_path), url_path


class TestReservedDashboardUrlPaths:
    """Reserved url_paths used by HA core dashboards must be excluded."""

    def test_includes_lovelace(self):
        assert "lovelace" in RESERVED_DASHBOARD_URL_PATHS

    def test_includes_core_dashboard_routes(self):
        for name in (
            "overview",
            "map",
            "logbook",
            "history",
            "energy",
            "developer-tools",
            "config",
            "profile",
            "media-browser",
            "todo",
            "calendar",
        ):
            assert name in RESERVED_DASHBOARD_URL_PATHS, name

    def test_is_frozenset(self):
        assert isinstance(RESERVED_DASHBOARD_URL_PATHS, frozenset)


async def _write_error(
    edit_yaml: EditYaml,
    yaml_path: str,
    *,
    file: str = "configuration.yaml",
    content: str = "x: 1\n",
    extra_allowed_keys: list[str] | None = None,
) -> str | None:
    """The error ``edit_yaml_config`` answers for an add, or None once it wrote."""
    result = await edit_yaml(
        {
            "file": file,
            "action": "add",
            "yaml_path": yaml_path,
            "content": content,
            "extra_allowed_keys": extra_allowed_keys or [],
        }
    )
    return None if result["success"] else str(result["error"])


def _allowed_keys(error: str | None) -> set[str]:
    """The keys a generic "not in the allowed list" refusal names."""
    assert error is not None and "Allowed keys: " in error, error
    listed = error.split("Allowed keys: ", 1)[1].split(". For YAML-mode", 1)[0]
    return set(listed.split(", "))


# Valid content per packages-only key (automation/scene are lists, script a map).
_PACKAGES_CONTENT = {
    "automation": "- id: x\n  alias: x\n  trigger: []\n  action: []\n",
    "script": "my_script:\n  sequence: []\n",
    "scene": "- id: x\n  name: x\n  entities: {}\n",
}


class TestDashboardFilename:
    """`filename:` value in a YAML-mode dashboard entry must stay under dashboards/."""

    @staticmethod
    async def _error(edit_yaml: EditYaml, filename: str) -> str | None:
        entry = f"mode: yaml\ntitle: x\nfilename: {json.dumps(filename)}\n"
        return await _write_error(
            edit_yaml, "lovelace.dashboards.fn-check", content=entry
        )

    @pytest.mark.parametrize(
        "filename",
        [
            "dashboards/main.yaml",
            "dashboards/sub/nested.yaml",
            "dashboards/energy-2026.yaml",
        ],
    )
    async def test_accepts_valid_filenames(
        self, edit_yaml: EditYaml, filename: str
    ) -> None:
        err = await self._error(edit_yaml, filename)
        assert err is None, f"{filename} should be valid, got: {err}"

    @pytest.mark.parametrize(
        "filename",
        [
            "../secrets.yaml",
            "/etc/passwd",
            "dashboards/../secrets.yaml",
            "dashboards/main.yml",  # wrong extension
            "main.yaml",  # not under dashboards/
            "www/dashboard.yaml",  # other allowlist dir, not dashboards
            "",
            "dashboards/",
            "dashboards/main",  # no extension
        ],
    )
    async def test_rejects_invalid_filenames(
        self, edit_yaml: EditYaml, filename: str
    ) -> None:
        err = await self._error(edit_yaml, filename)
        assert err is not None, f"{filename} should be rejected"
        assert "filename" in err


class TestYamlPathShapes:
    """yaml_path must be either a single allowed key OR
    a 3-segment dotted path 'lovelace.dashboards.<url_path>'."""

    @pytest.mark.parametrize(
        ("yaml_path", "says"),
        [
            # Genuinely unknown, not denylisted – a YAML_KEY_DENYLIST member
            # gets the categorical deny message instead of this one.
            ("zigbee2mqtt", "not in the allowed list"),
            ("lovelace", ""),
            ("lovelace.mode", "lovelace.dashboards.<url_path>"),
            ("lovelace.dashboards", ""),
            ("lovelace.dashboards.foo.bar", ""),
            ("lovelace.dashboards.UPPER", ""),
            ("homeassistant.customize", ""),
        ],
    )
    async def test_rejects_unsupported_yaml_path(
        self, edit_yaml: EditYaml, yaml_path: str, says: str
    ) -> None:
        err = await _write_error(edit_yaml, yaml_path)
        assert err is not None
        assert says in err


class TestPackagesOnlyKeys:
    """PACKAGES_ONLY_YAML_KEYS (automation/script/scene) gating.

    Accepted only in a packages file; in configuration.yaml the rejection
    must point at the storage-mode tools, not just dump the generic
    allowlist.
    """

    @pytest.mark.parametrize("key", ["automation", "script", "scene"])
    async def test_accepts_packages_only_key_in_a_package(
        self, edit_yaml: EditYaml, key: str
    ) -> None:
        err = await _write_error(
            edit_yaml,
            key,
            file=f"packages/{key}.yaml",
            content=_PACKAGES_CONTENT[key],
        )
        assert err is None

    @pytest.mark.parametrize("key", ["automation", "script", "scene"])
    async def test_rejects_packages_only_key_in_configuration_yaml(
        self, edit_yaml: EditYaml, key: str
    ) -> None:
        err = await _write_error(edit_yaml, key, content=_PACKAGES_CONTENT[key])
        assert err is not None
        assert "packages/*.yaml" in err
        # Spell each tool name out individually — an agent reading the
        # rejection sees the combined slash-form as one malformed name.
        assert "ha_config_set_automation" in err
        assert "ha_config_set_script" in err
        assert "ha_config_set_scene" in err

    async def test_union_list_in_generic_error_in_a_package(
        self, edit_yaml: EditYaml
    ) -> None:
        # Unknown single key inside a package file: error must enumerate both
        # ALLOWED_YAML_KEYS and PACKAGES_ONLY_YAML_KEYS so the user sees the
        # full surface they can pick from. The probe key must be genuinely
        # unknown – a YAML_KEY_DENYLIST member takes the deny branch instead
        # and never reaches this generic dump.
        err = await _write_error(edit_yaml, "zigbee2mqtt", file="packages/x.yaml")
        assert {"automation", "script", "scene"} <= _allowed_keys(err)

    async def test_generic_error_excludes_packages_only_in_configuration_yaml(
        self, edit_yaml: EditYaml
    ) -> None:
        # Unknown single key against configuration.yaml: allowlist must NOT
        # include PACKAGES_ONLY keys — listing them would mislead the user
        # into thinking they can rename their key to one of those.
        err = await _write_error(edit_yaml, "zigbee2mqtt")
        assert not {"automation", "script", "scene"} & _allowed_keys(err)


class TestExtraAllowedKeys:
    """Operator-configured extra write keys and the deny floor (#1887).

    ``extra_allowed_keys`` widens what a single-segment yaml_path may name;
    ``YAML_KEY_DENYLIST`` is the floor that widening can never cross.
    """

    @pytest.mark.parametrize("file", ["configuration.yaml", "packages/extra.yaml"])
    async def test_accepts_extra_key_in_both_file_kinds(
        self, edit_yaml: EditYaml, file: str
    ) -> None:
        # The filed case (alert2) reads from a package fragment, but the
        # setting is not packages-scoped – configuration.yaml counts too.
        err = await _write_error(
            edit_yaml, "alert2", file=file, extra_allowed_keys=["alert2"]
        )
        assert err is None

    @pytest.mark.parametrize("key", ["automation", "script", "scene"])
    async def test_extra_key_does_not_lift_packages_only_restriction(
        self, edit_yaml: EditYaml, key: str
    ) -> None:
        """An extra key must not make a packages-only key writable in
        configuration.yaml (#1887).

        Those keys have storage-mode equivalents and are confined to
        packages/*.yaml so the two collections cannot collide. If the extra-key
        setting lifted that, an operator could reach configuration.yaml with
        automation/script/scene while the per-key toggle governing them is off,
        since that toggle only gates packages targets.
        """
        err = await _write_error(
            edit_yaml, key, content=_PACKAGES_CONTENT[key], extra_allowed_keys=[key]
        )
        assert err is not None
        assert "packages/*.yaml" in err
        assert f"ha_config_set_{key}" in err

    @pytest.mark.parametrize("key", ["automation", "script", "scene"])
    async def test_packages_only_key_listed_as_extra_still_reaches_packages(
        self, edit_yaml: EditYaml, key: str
    ) -> None:
        """The restriction above must not narrow the packages path itself:
        inside packages/*.yaml these keys stay acceptable when the operator
        lists them too."""
        err = await _write_error(
            edit_yaml,
            key,
            file=f"packages/{key}.yaml",
            content=_PACKAGES_CONTENT[key],
            extra_allowed_keys=[key],
        )
        assert err is None

    async def test_generic_error_lists_extra_keys(self, edit_yaml: EditYaml) -> None:
        # An operator who added a key must see it in the allowed-keys dump,
        # otherwise the error contradicts their own configuration.
        err = await _write_error(edit_yaml, "nosuchkey", extra_allowed_keys=["alert2"])
        assert "alert2" in _allowed_keys(err)

    @pytest.mark.parametrize("file", ["configuration.yaml", "packages/x.yaml"])
    @pytest.mark.parametrize("key", ["homeassistant", "http", "frontend", "lovelace"])
    async def test_denylist_key_rejected_even_when_extra_allowed(
        self, edit_yaml: EditYaml, key: str, file: str
    ) -> None:
        # The floor must win over the operator's own list – this is the
        # whole point of it being non-operator-extendable.
        err = await _write_error(edit_yaml, key, file=file, extra_allowed_keys=[key])
        assert err is not None
        assert "never be edited" in err
        # The operator configured this key on purpose; without this sentence
        # the message never answers "why is my key being ignored".
        assert "cannot be lifted" in err

    async def test_denylist_error_is_distinct_from_generic_allowlist_dump(
        self, edit_yaml: EditYaml
    ) -> None:
        # A denied key must not be answered with "here are the keys you may
        # use instead" – that reads as "pick another key", hiding that this
        # one is categorically refused.
        deny_err = await _write_error(edit_yaml, "http")
        generic_err = await _write_error(edit_yaml, "nosuchkey")
        assert deny_err is not None and generic_err is not None
        assert deny_err != generic_err
        assert "not in the allowed list" not in deny_err


class TestYamlKeyDenylistContract:
    """Invariants the deny floor relies on (#1887)."""

    def test_denylist_disjoint_from_allow_sets(self):
        # The service checks the denylist first, so an overlap would silently
        # make an allowed key unreachable rather than failing loudly here.
        from custom_components.ha_mcp_tools.const import (
            ALLOWED_YAML_KEYS,
            PACKAGES_ONLY_YAML_KEYS,
            YAML_KEY_DENYLIST,
        )

        overlap = YAML_KEY_DENYLIST & (ALLOWED_YAML_KEYS | PACKAGES_ONLY_YAML_KEYS)
        assert not overlap, f"denylisted keys also marked allowed: {overlap}"

    async def test_caller_extra_keys_drop_denylisted_entries(
        self, edit_yaml: EditYaml
    ) -> None:
        """A denylisted or empty extra key never widens the allowed list a
        refusal names."""
        err = await _write_error(
            edit_yaml,
            "nosuchkey",
            extra_allowed_keys=["alert2", "http", "", "homeassistant"],
        )
        allowed = _allowed_keys(err)
        assert "alert2" in allowed
        assert not {"http", "", "homeassistant"} & allowed

    async def test_caller_extra_keys_default_empty(self, edit_yaml: EditYaml) -> None:
        from custom_components.ha_mcp_tools.const import ALLOWED_YAML_KEYS

        err = await _write_error(edit_yaml, "nosuchkey")
        assert _allowed_keys(err) == set(ALLOWED_YAML_KEYS)


class TestPostActionTableContract:
    """PACKAGES_ONLY_YAML_KEYS and YAML_KEY_POST_ACTIONS must stay in sync.

    If a future PR adds a key to PACKAGES_ONLY_YAML_KEYS without a matching
    reload_service entry, callers would see the default restart_required
    response — a regression we should catch at unit-test time.
    """

    def test_packages_only_keys_have_post_actions(self):
        from custom_components.ha_mcp_tools.const import (
            PACKAGES_ONLY_YAML_KEYS,
            YAML_KEY_POST_ACTIONS,
        )

        missing = PACKAGES_ONLY_YAML_KEYS - set(YAML_KEY_POST_ACTIONS)
        assert not missing, (
            f"PACKAGES_ONLY_YAML_KEYS missing YAML_KEY_POST_ACTIONS entries: {missing}"
        )

    def test_packages_only_disjoint_from_allowed(self):
        """ALLOWED_YAML_KEYS and PACKAGES_ONLY_YAML_KEYS must not overlap.

        ``_parse_and_validate_yaml_path`` checks ``ALLOWED_YAML_KEYS``
        first, then the ``is_package and key in PACKAGES_ONLY_YAML_KEYS``
        branch. If a future change accidentally lands a packages-only
        key (``automation`` / ``script`` / ``scene``) into
        ``ALLOWED_YAML_KEYS`` as well, the gating branch becomes dead
        code and that key would silently land in ``configuration.yaml``.
        """
        from custom_components.ha_mcp_tools.const import (
            ALLOWED_YAML_KEYS,
            PACKAGES_ONLY_YAML_KEYS,
        )

        overlap = ALLOWED_YAML_KEYS & PACKAGES_ONLY_YAML_KEYS
        assert not overlap, f"sets must be disjoint; overlap: {overlap}"

    @pytest.mark.parametrize(
        ("key", "expected_service"),
        [
            ("automation", "automation.reload"),
            ("script", "script.reload"),
            ("scene", "scene.reload"),
        ],
    )
    def test_packages_only_reload_services(self, key, expected_service):
        from custom_components.ha_mcp_tools.const import YAML_KEY_POST_ACTIONS

        entry = YAML_KEY_POST_ACTIONS[key]
        assert entry["post_action"] == "reload_available"
        assert entry["reload_service"] == expected_service


class TestHandleEditYamlConfigDashboards:
    """Integration of dashboard branch into handle_edit_yaml_config."""

    async def test_register_yaml_mode_dashboard(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """A new lovelace.dashboards.<url_path> entry is added under that key only."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("default_config:\n")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "lovelace.dashboards.energy-dash",
                "content": (
                    "mode: yaml\n"
                    "title: Energy\n"
                    "filename: dashboards/energy.yaml\n"
                    "show_in_sidebar: true\n"
                ),
                "backup": False,
            }
        )
        assert result["success"] is True, result
        text = cfg.read_text()
        assert "lovelace:" in text
        assert "dashboards:" in text
        assert "energy-dash:" in text
        assert "default_config:" in text
        # Must NOT introduce lovelace.mode
        assert "mode: yaml" in text  # appears under the dashboard entry though
        # Make sure 'mode:' isn't a sibling of 'dashboards:' under 'lovelace:'
        # (i.e., lovelace key should only contain 'dashboards')
        parsed = yaml.safe_load(text)
        assert set(parsed["lovelace"].keys()) == {"dashboards"}

    async def test_rejects_filename_traversal(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "lovelace.dashboards.bad-dash",
                "content": ("mode: yaml\ntitle: Bad\nfilename: ../secrets.yaml\n"),
                "backup": False,
            }
        )
        assert result["success"] is False
        assert "filename" in result["error"]

    async def test_rejects_reserved_url_path(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "lovelace.dashboards.lovelace",
                "content": "mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
                "backup": False,
            }
        )
        assert result["success"] is False
        assert "reserved" in result["error"].lower()

    async def test_remove_dashboard_entry_only(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """`remove` only deletes the targeted dashboard, not the whole lovelace key."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(
            "lovelace:\n"
            "  dashboards:\n"
            "    energy-dash:\n"
            "      mode: yaml\n"
            "      title: Energy\n"
            "      filename: dashboards/energy.yaml\n"
            "    weather-dash:\n"
            "      mode: yaml\n"
            "      title: Weather\n"
            "      filename: dashboards/weather.yaml\n"
        )

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "remove",
                "yaml_path": "lovelace.dashboards.energy-dash",
                "backup": False,
            }
        )
        assert result["success"] is True
        parsed = yaml.safe_load(cfg.read_text())
        assert "energy-dash" not in parsed["lovelace"]["dashboards"]
        assert "weather-dash" in parsed["lovelace"]["dashboards"]

    async def test_rejects_missing_filename_key(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """add/replace without `filename:` should be rejected by the validator."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "lovelace.dashboards.no-filename",
                "content": "mode: yaml\ntitle: x\n",  # filename omitted
                "backup": False,
            }
        )
        assert result["success"] is False
        assert "filename" in result["error"]

    async def test_add_merges_into_existing_dashboard_entry(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """add into an existing url_path merges (dict.update) rather than replaces."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(
            "lovelace:\n"
            "  dashboards:\n"
            "    energy-dash:\n"
            "      mode: yaml\n"
            "      title: Old\n"
            "      filename: dashboards/energy.yaml\n"
        )

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "lovelace.dashboards.energy-dash",
                "content": (
                    "title: New\n"
                    "filename: dashboards/energy.yaml\n"
                    "show_in_sidebar: true\n"
                ),
                "backup": False,
            }
        )
        assert result["success"] is True, result
        parsed = yaml.safe_load(cfg.read_text())
        entry = parsed["lovelace"]["dashboards"]["energy-dash"]
        # Old keys retained, overlapping keys overwritten, new keys added
        assert entry["mode"] == "yaml"
        assert entry["title"] == "New"
        assert entry["show_in_sidebar"] is True


class TestHandleEditYamlConfigSingleKey:
    """Single-key branch of _build_edit_yaml_config_handler must behave the same
    after the factory refactor."""

    async def test_single_key_add_creates_new_key(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("default_config:\n")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "command_line",
                "content": "- sensor:\n    name: foo\n    command: 'echo 1'\n",
                "backup": False,
            }
        )
        assert result["success"] is True, result
        parsed = yaml.safe_load(cfg.read_text())
        assert "command_line" in parsed
        assert parsed["default_config"] is None

    async def test_single_key_replace_overwrites(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("shell_command:\n  old_cmd: 'echo old'\n")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "replace",
                "yaml_path": "shell_command",
                "content": "new_cmd: 'echo new'\n",
                "backup": False,
            }
        )
        assert result["success"] is True, result
        parsed = yaml.safe_load(cfg.read_text())
        assert parsed["shell_command"] == {"new_cmd": "echo new"}

    async def test_single_key_remove_missing_errors(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("default_config:\n")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "remove",
                "yaml_path": "command_line",
                "backup": False,
            }
        )
        assert result["success"] is False
        assert "command_line" in result["error"]

    async def test_single_key_post_action_for_template(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """template -> reload_available; covers post-action lookup for single-key kind."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "template",
                "content": ("- sensor:\n    - name: t\n      state: 'ok'\n"),
                "backup": False,
            }
        )
        assert result["success"] is True, result
        assert result["post_action"] == "reload_available"
        assert result["reload_service"] == "homeassistant.reload_custom_templates"

    async def test_single_key_post_action_for_shell_command(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """shell_command falls through to default 'restart_required'."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "shell_command",
                "content": "echo: 'echo hi'\n",
                "backup": False,
            }
        )
        assert result["success"] is True, result
        assert result["post_action"] == "restart_required"


class TestHandleEditYamlConfigPathTraversal:
    """Path-traversal defense composes ``os.path.normpath`` before the
    ``fnmatch`` package check, so a crafted ``packages/../configuration.yaml``
    cannot smuggle a PACKAGES_ONLY key into ``configuration.yaml``."""

    async def test_packages_dotdot_normalizes_to_configuration_yaml_and_rejects(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """``packages/../configuration.yaml`` → ``configuration.yaml`` after
        normpath, so the ``packages/*.yaml`` fnmatch does not match and
        ``yaml_path="automation"`` is rejected with the storage-mode pointer.

        Pins the layering: the handler's normpath must run before the
        fnmatch package check in ``_classify_edit_yaml_target`` so a crafted path
        cannot smuggle a PACKAGES_ONLY key into configuration.yaml. The
        defense is correct by construction; this is belt-and-suspenders
        against a future refactor reordering those two steps."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("")

        result = await edit_yaml(
            {
                "file": "packages/../configuration.yaml",
                "action": "add",
                "yaml_path": "automation",
                "content": "- id: x\n  alias: x\n",
                "backup": False,
            }
        )
        assert result["success"] is False
        assert "packages/*.yaml" in result["error"]
        assert "ha_config_set_automation" in result["error"]


class TestHandleEditYamlConfigPackagesGate:
    """Server-side per-key gate: the component independently refuses a
    PACKAGES_ONLY key when the caller passes it in ``disabled_packages_keys``,
    even if the wrapper's client-side gate were bypassed (defense in depth).
    Mirrors the wrapper-side coverage in test_yaml_config_tool.py."""

    @pytest.mark.parametrize("key", ["automation", "script", "scene"])
    async def test_disabled_key_rejected_server_side(
        self, edit_yaml: EditYaml, key: str
    ) -> None:
        """A disabled key targeting packages/*.yaml is refused by the
        component itself — independently of the wrapper — with a message
        pointing back at the caller's runtime configuration."""
        result = await edit_yaml(
            {
                "file": f"packages/{key}.yaml",
                "action": "add",
                "yaml_path": key,
                "content": _PACKAGES_CONTENT[key],
                "backup": False,
                "disabled_packages_keys": [key],
            }
        )
        assert result["success"] is False, result
        assert key in result["error"]
        assert "disabled by the caller's runtime configuration" in result["error"]

    async def test_other_packages_key_not_blocked(self, edit_yaml: EditYaml) -> None:
        """The gate is per-key: disabling ``automation`` must not block a
        ``script`` write to the same packages surface."""
        result = await edit_yaml(
            {
                "file": "packages/things.yaml",
                "action": "add",
                "yaml_path": "script",
                "content": _PACKAGES_CONTENT["script"],
                "backup": False,
                "disabled_packages_keys": ["automation"],
            }
        )
        assert result["success"] is True, result

    async def test_configuration_yaml_disabled_key_falls_through(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """A disabled PACKAGES_ONLY key written to configuration.yaml must
        fall through to the storage-mode advisory (``_parse_and_validate_yaml_path``),
        NOT the per-key gate message — the gate is scoped to packages targets."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("")
        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "automation",
                "content": _PACKAGES_CONTENT["automation"],
                "backup": False,
                "disabled_packages_keys": ["automation"],
            }
        )
        assert result["success"] is False, result
        # The storage-mode advisory, not the per-key gate message.
        assert "disabled by the caller's runtime configuration" not in result["error"]
        assert "ha_config_set_automation" in result["error"]

    async def test_unknown_disabled_key_is_noop(self, edit_yaml: EditYaml) -> None:
        """An unrecognized key in ``disabled_packages_keys`` is filtered out
        (typo guard), so it cannot accidentally block a real, enabled key."""
        result = await edit_yaml(
            {
                "file": "packages/automations.yaml",
                "action": "add",
                "yaml_path": "automation",
                "content": _PACKAGES_CONTENT["automation"],
                "backup": False,
                "disabled_packages_keys": ["automatoin"],  # typo
            }
        )
        assert result["success"] is True, result

    async def test_default_empty_disabled_allows(self, edit_yaml: EditYaml) -> None:
        """Omitting ``disabled_packages_keys`` entirely (older wrapper, or the
        schema default) imposes no per-key restriction on a packages write."""
        result = await edit_yaml(
            {
                "file": "packages/automations.yaml",
                "action": "add",
                "yaml_path": "automation",
                "content": _PACKAGES_CONTENT["automation"],
                "backup": False,
            }
        )
        assert result["success"] is True, result

    def test_flag_map_matches_packages_only_keys(self):
        """The wrapper's ``YAML_PACKAGES_FLAG_BY_KEY`` must stay in lockstep
        with the component's ``PACKAGES_ONLY_YAML_KEYS`` (no import-time
        coupling exists), and every mapped flag must be a real ``Settings``
        field. Otherwise a packages-only key silently becomes ungated, or
        ``_disabled_packages_keys`` raises at runtime."""
        from custom_components.ha_mcp_tools.const import PACKAGES_ONLY_YAML_KEYS
        from ha_mcp.config import Settings
        from ha_mcp.tools.tools_yaml_config import YAML_PACKAGES_FLAG_BY_KEY

        assert set(YAML_PACKAGES_FLAG_BY_KEY) == set(PACKAGES_ONLY_YAML_KEYS)
        for flag in YAML_PACKAGES_FLAG_BY_KEY.values():
            assert flag in Settings.model_fields, (
                f"{flag} is mapped in YAML_PACKAGES_FLAG_BY_KEY but is not a "
                "Settings field"
            )
