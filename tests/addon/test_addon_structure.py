"""Test Home Assistant add-on structure and configuration."""

import importlib.util
import os
import re
import stat
import sys
import warnings
from pathlib import Path

import pytest
import yaml

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Fallback for older Python


ADDON_DIR = "homeassistant-addon"
_REPO_ROOT = Path(__file__).resolve().parents[2]

_POLICY_PATH = _REPO_ROOT / "src/ha_mcp/settings_ui/_locale_policy.py"
_POLICY_SPEC = importlib.util.spec_from_file_location("_locale_policy", _POLICY_PATH)
if _POLICY_SPEC is None or _POLICY_SPEC.loader is None:  # pragma: no cover
    raise ImportError(f"cannot load locale policy from {_POLICY_PATH}")
_POLICY = importlib.util.module_from_spec(_POLICY_SPEC)
_POLICY_SPEC.loader.exec_module(_POLICY)
is_best_effort_locale = _POLICY.is_best_effort_locale


def _report_translation_issues(path: Path, issues: list[str]) -> None:
    """Warn for a best-effort locale, while preserving strict failures."""
    if not issues:
        return
    message = "; ".join(issues)
    if is_best_effort_locale(path.stem):
        warnings.warn(
            f"best-effort locale {path.stem} in {path}: {message}",
            pytest.PytestWarning,
            stacklevel=2,
        )
        return
    raise AssertionError(message)


# Resolved once at import: pytest parametrizes at collection time, so an empty
# list would collect zero cases and report as skipped rather than failed.
# test_addon_config_glob_is_not_empty below is what keeps that honest.
_ADDON_DIRS = sorted(
    path.parent.name for path in _REPO_ROOT.glob("homeassistant-addon*/config.yaml")
)


def test_best_effort_addon_translation_issues_warn_instead_of_fail() -> None:
    issues = ["missing configuration.example"]
    with pytest.warns(
        pytest.PytestWarning,
        match=r"best-effort locale tlh.*missing configuration\.example",
    ):
        _report_translation_issues(Path("translations/tlh.yaml"), issues)

    with pytest.raises(AssertionError, match=r"missing configuration\.example"):
        _report_translation_issues(Path("translations/de.yaml"), issues)


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

        # Verify architectures (only 64-bit platforms supported by uv image)
        expected_archs = ["amd64", "aarch64"]
        assert all(arch in config["arch"] for arch in expected_archs)

        # Verify 32-bit platforms are not included
        unsupported_archs = ["armhf", "armv7", "i386"]
        assert not any(arch in config["arch"] for arch in unsupported_archs), (
            "32-bit platforms not supported by uv base image"
        )

    @pytest.mark.parametrize(
        ("addon_dir", "entry"),
        [
            ("homeassistant-addon", r"^### {key}\b"),
            ("homeassistant-addon-dev", r"^\| `{key}`"),
        ],
        ids=["stable section", "dev table row"],
    )
    def test_docs_describe_every_app_option(self, addon_dir, entry):
        """The Configuration page shows only a name and a short help text
        per option; DOCS.md is where a user finds the details. The stable
        DOCS.md has a section per option and the dev one a table row."""
        config = yaml.safe_load((_REPO_ROOT / addon_dir / "config.yaml").read_text())
        docs = (_REPO_ROOT / addon_dir / "DOCS.md").read_text(encoding="utf-8")

        missing = [
            key
            for key in config["schema"]
            if not re.search(entry.format(key=re.escape(key)), docs, re.MULTILINE)
        ]

        assert not missing, f"{addon_dir}/DOCS.md does not document {missing}"

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

    def test_addon_config_glob_is_not_empty(self):
        """``test_translations_cover_every_schema_key`` is parametrized over
        this glob at collection time. An empty glob collects zero cases and
        pytest reports it as skipped, which reads as green — so assert the
        glob found something, the way the in-body glob in
        ``test_addon_names_are_backup_filename_safe`` does.
        """
        assert _ADDON_DIRS, (
            "no homeassistant-addon*/config.yaml found — the schema-key "
            "translation check would silently collect zero cases"
        )

    @pytest.mark.filterwarnings("always:best-effort locale")
    @pytest.mark.parametrize("addon_dir", _ADDON_DIRS)
    def test_translations_cover_every_schema_key(self, addon_dir):
        """Every key declared in ``config.yaml``'s ``schema:`` must have a
        matching ``configuration.<key>`` entry — with both ``name`` and
        ``description`` populated — in *every* ``translations/*.yaml`` file,
        not only English. The ``advanced_debug_logging`` schema field was
        added on stable but the translation was forgotten — the addon
        Configuration UI then showed an unlabelled checkbox. A missing
        localized ``name``/``description`` shows the same unlabelled toggle
        to that language's users, so lock the parity across every shipped
        locale.

        Checked in both directions. An option removed from ``schema:``
        leaves its ``configuration.<key>`` entry behind in every locale,
        where it is dead weight that reads as a supported option and gets
        dutifully re-translated for the next language — the one-directional
        version of this check could not see it.

        Parametrized over the same ``homeassistant-addon*/config.yaml`` glob
        as ``test_addon_names_are_backup_filename_safe`` below: hardcoding the
        pair left both Webhook Proxy flavors with no such check at all.
        """
        with open(f"{addon_dir}/config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        declared_keys = set(cfg.get("schema", {}).keys())
        # ``secret_path`` is intentionally undocumented in user-facing
        # translations (it's an advanced/hidden override the wizard
        # handles, not a user-set option). It stays in ``declared_keys``
        # so a catalog that documents it anyway is not called an orphan.
        schema_keys = declared_keys - {"secret_path"}

        translation_files = sorted(Path(addon_dir, "translations").glob("*.yaml"))
        assert translation_files, f"{addon_dir}/translations has no *.yaml files"
        for tf in translation_files:
            try:
                with open(tf, encoding="utf-8") as f:
                    translations = yaml.safe_load(f)
                configuration = translations.get("configuration", {})
                orphaned = sorted(set(configuration) - declared_keys)
                assert not orphaned, (
                    f"{tf} documents `configuration` key(s) that {addon_dir}/"
                    f"config.yaml no longer declares in `schema:`: {orphaned}. "
                    "Supervisor renders nothing for them — delete the entries."
                )
                for key in sorted(schema_keys):
                    entry = configuration.get(key)
                    assert entry is not None, (
                        f"{tf} is missing a `configuration.{key}` entry for the "
                        "schema field declared in config.yaml"
                    )
                    assert entry.get("name"), (
                        f"{tf} `configuration.{key}` needs a non-empty `name` "
                        "(Supervisor renders it as the user-facing toggle label)"
                    )
                    assert entry.get("description"), (
                        f"{tf} `configuration.{key}` needs a non-empty "
                        "`description` (Supervisor renders it as the help tooltip "
                        "under the toggle)"
                    )
            except Exception as exc:  # noqa: BLE001
                _report_translation_issues(tf, [str(exc)])

    def test_addon_names_are_backup_filename_safe(self):
        r"""No add-on ``name`` may contain ``/``.

        Home Assistant Supervisor builds the pre-update backup filename from
        the add-on name (spaces -> underscores, other characters kept) and
        validates it against ``^[^/]+\.tar$``. A ``/`` in the name therefore
        makes "Update" with "Create backup before update" enabled crash with
        ``does not match regular expression`` (issue #1707). Covers every
        ``homeassistant-addon*`` flavour so a new add-on can't reintroduce it.
        """
        configs = sorted(_REPO_ROOT.glob("homeassistant-addon*/config.yaml"))
        assert configs, "no add-on config.yaml files found to validate"
        for config_path in configs:
            name = yaml.safe_load(config_path.read_text())["name"]
            assert "/" not in name, (
                f"{config_path.parent.name}: add-on name {name!r} contains "
                r"'/', which breaks the Supervisor pre-update backup filename "
                r"(^[^/]+\.tar$, issue #1707). Use a different separator "
                "such as '-'."
            )
