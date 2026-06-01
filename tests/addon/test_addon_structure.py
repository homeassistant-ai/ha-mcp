"""Test Home Assistant add-on structure and configuration."""

import os
import stat
import sys

import pytest
import yaml

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Fallback for older Python


ADDON_DIR = "homeassistant-addon"


class TestAddonStructure:
    """Verify add-on meets Home Assistant requirements."""

    def test_required_files_exist(self):
        """Check all required add-on files are present."""
        required_files = [
            "config.yaml",
            "Dockerfile",
            "start.py",
            "README.md",
            "DOCS.md",
        ]
        for file in required_files:
            path = os.path.join(ADDON_DIR, file)
            assert os.path.exists(path), f"Missing required file: {file}"

    def test_config_yaml_valid(self):
        """Verify config.yaml is valid YAML with required fields."""
        with open(f"{ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)

        required_fields = ["name", "description", "version", "slug", "arch", "image"]
        for field in required_fields:
            assert field in config, f"Missing required field: {field}"

        # Verify add-on version matches package version (synced by semantic-release)
        with open("pyproject.toml", "rb") as f:
            pyproject = tomllib.load(f)
        expected_version = pyproject["project"]["version"]
        assert config["version"] == expected_version, (
            f"Add-on version {config['version']} should match package version {expected_version}"
        )

        # Verify essential configurations
        assert config["hassio_api"] is True, "hassio_api required for Supervisor"
        assert config["homeassistant_api"] is True, "homeassistant_api required"

        # Verify image field uses per-architecture naming
        assert config["image"] == "ghcr.io/homeassistant-ai/ha-mcp-addon-{arch}", (
            "image field must use per-architecture naming with {arch} placeholder"
        )

        # Verify port configuration (fixed internal port)
        assert "ports" in config, "ports section required for HTTP transport"
        assert "9583/tcp" in config["ports"], "port 9583/tcp must be exposed"

        # Verify ingress is enabled so the stable add-on exposes the web
        # Settings UI ("Open Web UI" button). This must stay declared here —
        # the release pipeline syncs version/changelog only, not functional
        # config, so ingress is not auto-mirrored from the dev add-on. Locks
        # the regression where stable shipped without the button.
        assert config.get("ingress") is True, (
            "ingress must be enabled so the 'Open Web UI' button / web Settings "
            "UI is reachable on the stable add-on"
        )
        assert config.get("ingress_port") == 9583, (
            "ingress_port must be 9583 (the fixed internal MCP/web port)"
        )
        assert config.get("ingress_stream") is True, (
            "ingress_stream must be enabled so streamed responses flush through "
            "the ingress proxy (streamable-HTTP MCP transport)"
        )

        # Verify secret_path configuration (optional advanced override)
        assert "secret_path" not in config["options"], (
            "secret_path should be optional and omitted so Supervisor treats it as advanced"
        )
        assert "secret_path" in config["schema"], (
            "schema must include secret_path field"
        )
        assert config["schema"]["secret_path"] == "str?", (
            "secret_path schema should be optional string (str?)"
        )

        # Verify backup_hint configuration
        assert "backup_hint" in config["options"], (
            "options must include backup_hint field"
        )
        assert config["options"]["backup_hint"] == "normal", (
            "default backup_hint should be normal"
        )
        assert config["schema"]["backup_hint"] == "list(strong|normal|weak|auto)", (
            "backup_hint schema must enumerate allowed values"
        )

        # Verify architectures (only 64-bit platforms supported by uv image)
        expected_archs = ["amd64", "aarch64"]
        assert all(arch in config["arch"] for arch in expected_archs)

        # Verify 32-bit platforms are not included
        unsupported_archs = ["armhf", "armv7", "i386"]
        assert not any(arch in config["arch"] for arch in unsupported_archs), (
            "32-bit platforms not supported by uv base image"
        )

    def test_stable_addon_exposes_nonbeta_tool_options(self):
        """Stable add-on must expose the NON-beta operator options that dev
        has — ``tool_search_max_results``, ``disabled_tools``, ``pinned_tools``
        — in both ``options`` and ``schema``. These are not beta features, so
        the web-UI master gate doesn't apply; without them in the stable
        schema they were unreachable on stable (start.py wrote defaults, the
        web UI showed them ``origin='addon'``-locked, and the override applier
        skipped them). Regression guard for that dev/stable config drift."""
        with open(f"{ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)

        # key: (schema type, default value). Defaults are load-bearing — a
        # non-empty disabled_tools default would silently lock tools off.
        expected = {
            "tool_search_max_results": ("int(2,10)?", 5),
            "disabled_tools": ("str?", ""),
            "pinned_tools": ("str?", ""),
        }
        for key, (schema_type, default) in expected.items():
            assert key in config["options"], f"{key!r} must be in stable options"
            assert config["options"].get(key) == default, (
                f"{key!r} default must be {default!r}"
            )
            assert config["schema"].get(key) == schema_type, (
                f"{key!r} must be in stable schema as {schema_type!r}"
            )

    def test_stable_and_dev_agree_on_nonbeta_tool_options(self):
        """The three non-beta tool options must stay in sync between the
        stable and dev add-ons — same defaults AND same schema types. Guards
        against future one-sided drift (the exact bug class this fix
        addresses: dev gains/changes an option, stable is forgotten)."""
        keys = ("tool_search_max_results", "disabled_tools", "pinned_tools")
        with open(f"{ADDON_DIR}/config.yaml") as f:
            stable = yaml.safe_load(f)
        with open("homeassistant-addon-dev/config.yaml") as f:
            dev = yaml.safe_load(f)
        for key in keys:
            assert stable["options"].get(key) == dev["options"].get(key), (
                f"{key!r} option default differs between stable and dev add-ons"
            )
            assert stable["schema"].get(key) == dev["schema"].get(key), (
                f"{key!r} schema type differs between stable and dev add-ons"
            )

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Unix permissions not applicable on Windows"
    )
    def test_start_script_executable(self):
        """Verify start.py has executable permissions."""
        start_py = f"{ADDON_DIR}/start.py"
        st = os.stat(start_py)
        assert st.st_mode & stat.S_IXUSR, "start.py must be executable"

    def test_start_script_has_shebang(self):
        """Verify start.py has proper shebang."""
        with open(f"{ADDON_DIR}/start.py") as f:
            first_line = f.readline()
        assert first_line.startswith("#!"), "start.py missing shebang"
        assert "python" in first_line.lower(), "start.py shebang must reference python"

    @pytest.mark.parametrize(
        "addon_dir", ["homeassistant-addon", "homeassistant-addon-dev"]
    )
    def test_translations_cover_every_schema_key(self, addon_dir):
        """Every key declared in ``config.yaml``'s ``schema:`` must have a
        matching ``configuration.<key>`` entry in ``translations/en.yaml``
        with both ``name`` and ``description`` populated. The
        ``advanced_debug_logging`` schema field was added on stable but
        the translation was forgotten — the addon Configuration UI
        then showed an unlabelled checkbox. Lock the parity so the
        same class of silent gap can't recur.
        """
        with open(f"{addon_dir}/config.yaml") as f:
            cfg = yaml.safe_load(f)
        with open(f"{addon_dir}/translations/en.yaml") as f:
            translations = yaml.safe_load(f)
        schema_keys = set(cfg.get("schema", {}).keys())
        # ``secret_path`` is intentionally undocumented in user-facing
        # translations (it's an advanced/hidden override the wizard
        # handles, not a user-set option).
        schema_keys.discard("secret_path")
        configuration = translations.get("configuration", {})
        for key in sorted(schema_keys):
            entry = configuration.get(key)
            assert entry is not None, (
                f"{addon_dir}/translations/en.yaml is missing a "
                f"`configuration.{key}` entry for the schema field "
                f"declared in config.yaml"
            )
            assert entry.get("name"), (
                f"{addon_dir}/translations/en.yaml `configuration.{key}` "
                "needs a non-empty `name` (Supervisor renders it as the "
                "user-facing toggle label)"
            )
            assert entry.get("description"), (
                f"{addon_dir}/translations/en.yaml `configuration.{key}` "
                "needs a non-empty `description` (Supervisor renders it "
                "as the help tooltip under the toggle)"
            )
