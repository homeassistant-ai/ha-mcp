"""Test Home Assistant add-on startup and logging."""

import functools
import importlib.util
import json
import subprocess
import time
from pathlib import Path

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy


@functools.cache
def _load_addon_start():
    """Import homeassistant-addon/start.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "addon_start",
        Path(__file__).parents[2] / "homeassistant-addon" / "start.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSecretPathValidation:
    """Unit tests for secret path validation logic."""

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = _load_addon_start()

    def test_valid_path_accepted(self, tmp_path):
        secret_file = tmp_path / "secret_path.txt"
        secret_file.write_text("/private_abc123")
        result = self.addon.get_or_create_secret_path(tmp_path)
        assert result == "/private_abc123"

    def test_url_in_file_triggers_regeneration(self, tmp_path):
        secret_file = tmp_path / "secret_path.txt"
        secret_file.write_text("https://192.168.1.18:9583/private_abc123")
        result = self.addon.get_or_create_secret_path(tmp_path)
        assert result.startswith("/private_")
        assert "://" not in result
        assert secret_file.read_text() == result

    def test_empty_file_triggers_regeneration(self, tmp_path):
        secret_file = tmp_path / "secret_path.txt"
        secret_file.write_text("")
        result = self.addon.get_or_create_secret_path(tmp_path)
        assert result.startswith("/private_")

    def test_url_custom_path_triggers_regeneration(self, tmp_path):
        result = self.addon.get_or_create_secret_path(
            tmp_path, custom_path="http://attacker.example.com/x"
        )
        assert result.startswith("/private_")
        assert "://" not in result

    def test_valid_custom_path_used(self, tmp_path):
        result = self.addon.get_or_create_secret_path(
            tmp_path, custom_path="my_custom_secret"
        )
        assert result == "/my_custom_secret"

    def test_no_secret_file_generates_new_path(self, tmp_path):
        result = self.addon.get_or_create_secret_path(tmp_path)
        assert result.startswith("/private_")
        assert (tmp_path / "secret_path.txt").read_text() == result

    def test_whitespace_custom_path_falls_through_to_stored(self, tmp_path):
        (tmp_path / "secret_path.txt").write_text("/private_stored")
        result = self.addon.get_or_create_secret_path(tmp_path, custom_path="   ")
        assert result == "/private_stored"

    def test_is_valid_secret_path(self):
        assert self.addon._is_valid_secret_path("/private_abc") is True
        assert self.addon._is_valid_secret_path("/mysecrt") is True   # exactly 8 chars
        assert self.addon._is_valid_secret_path("/custom") is False   # 7 chars — too short
        assert self.addon._is_valid_secret_path("/short") is False    # too short
        assert self.addon._is_valid_secret_path("https://example.com/x") is False
        assert self.addon._is_valid_secret_path("/https://evil.com") is False
        assert self.addon._is_valid_secret_path("no-leading-slash") is False
        assert self.addon._is_valid_secret_path("") is False


class TestPersistAddonOptions:
    """Unit tests for persisting addon options to Supervisor (#941)."""

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = _load_addon_start()

    def test_sends_full_options_dict_as_post(self, monkeypatch):
        """Sends POST /addons/self/options with body {"options": <full dict>}."""
        captured: dict = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b""

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.header_items())
            captured["body"] = req.data
            return FakeResp()

        monkeypatch.setattr(self.addon.urllib.request, "urlopen", fake_urlopen)

        options = {
            "backup_hint": "normal",
            "enable_skills": True,
            "secret_path": "/private_abc12345",
        }
        # Returns None on success — helper communicates failure via exceptions.
        assert self.addon.persist_addon_options(options, "test-token") is None
        assert captured["url"] == "http://supervisor/addons/self/options"
        assert captured["method"] == "POST"
        assert captured["headers"]["Authorization"] == "Bearer test-token"
        assert captured["headers"]["Content-type"] == "application/json"
        assert json.loads(captured["body"]) == {"options": options}

    def test_http_error_propagates(self, monkeypatch):
        """Validation failures from Supervisor propagate to the caller.

        Silent suppression here would hide the exact failure this PR exists
        to fix: the webhook proxy unable to discover the secret path. Main()
        must see the exception so it can log an actionable recovery message.
        """
        import io
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"result":"error","message":"invalid options"}'),
            )

        monkeypatch.setattr(self.addon.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(urllib.error.HTTPError):
            self.addon.persist_addon_options({"secret_path": "/private_x"}, "test-token")

    def test_connection_error_propagates(self, monkeypatch):
        """Network failures propagate to the caller — no silent swallowing."""
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(self.addon.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(urllib.error.URLError):
            self.addon.persist_addon_options({"secret_path": "/private_x"}, "test-token")


class TestMaybePersistSecretPath:
    """Tests for the gate-and-recover wrapper called from main() (#941)."""

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = _load_addon_start()

    def test_skips_when_config_is_empty(self, monkeypatch):
        """If /data/options.json was missing/corrupt, don't try to persist.

        Sending a bare `{"secret_path": ...}` without required fields would
        trip schema validation on the Supervisor side and produce a second,
        misleading error line on top of the "Failed to read config" we
        already logged upstream.
        """
        calls: list = []
        monkeypatch.setattr(
            self.addon,
            "persist_addon_options",
            lambda *args, **kwargs: calls.append(args),
        )
        self.addon.maybe_persist_secret_path({}, "/private_new", "test-token")
        assert calls == []

    def test_skips_when_stored_path_matches(self, monkeypatch):
        """No-op when Supervisor already has the right value — avoids noise on every restart."""
        calls: list = []
        monkeypatch.setattr(
            self.addon,
            "persist_addon_options",
            lambda *args, **kwargs: calls.append(args),
        )
        config = {"backup_hint": "normal", "secret_path": "/private_same"}
        self.addon.maybe_persist_secret_path(config, "/private_same", "test-token")
        assert calls == []

    def test_persists_with_full_config_merged(self, monkeypatch):
        """When the path changes, POSTs `{**config, "secret_path": new}`."""
        captured: list = []

        def fake_persist(options, token):
            captured.append((options, token))

        monkeypatch.setattr(self.addon, "persist_addon_options", fake_persist)
        config = {
            "backup_hint": "normal",
            "enable_skills": True,
            "secret_path": "/private_old",
        }
        self.addon.maybe_persist_secret_path(config, "/private_new", "test-token")
        assert captured == [
            (
                {
                    "backup_hint": "normal",
                    "enable_skills": True,
                    "secret_path": "/private_new",
                },
                "test-token",
            )
        ]

    def test_catches_http_error_and_logs_actionable_message(self, monkeypatch, capfd):
        """HTTPError from Supervisor must not escape — the addon must keep starting — but the log line must name the path so the user can recover manually."""
        import urllib.error

        def raising_persist(options, token):
            raise urllib.error.HTTPError(
                "http://supervisor/addons/self/options",
                400,
                "Bad Request",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )

        monkeypatch.setattr(self.addon, "persist_addon_options", raising_persist)
        # Should not raise.
        self.addon.maybe_persist_secret_path(
            {"backup_hint": "normal", "secret_path": "/private_old"},
            "/private_new",
            "test-token",
        )
        err = capfd.readouterr().err
        assert "Failed to persist secret_path" in err
        assert "HTTP 400" in err
        assert "/private_new" in err  # user-facing recovery value
        assert "Secret path override" in err  # points at the config field

    def test_catches_network_error_and_logs(self, monkeypatch, capfd):
        """URLError / timeout / OSError all caught the same way."""
        import urllib.error

        def raising_persist(options, token):
            raise urllib.error.URLError("supervisor unreachable")

        monkeypatch.setattr(self.addon, "persist_addon_options", raising_persist)
        self.addon.maybe_persist_secret_path(
            {"backup_hint": "normal", "secret_path": "/private_old"},
            "/private_new",
            "test-token",
        )
        err = capfd.readouterr().err
        assert "Failed to persist secret_path" in err
        assert "supervisor unreachable" in err
        assert "/private_new" in err


class TestSkillsAsToolsMigration:
    """Unit tests for one-time enable_skills_as_tools default migration.

    Background: the Pydantic default for enable_skills_as_tools was flipped
    to True in #806, but the add-on's config.yaml was never updated at the
    same time, so add-on users silently stayed on False. This migration
    flips the stored value to True once for existing installs, then
    respects the user's choice on subsequent boots.
    """

    MARKER_NAME = ".skills_as_tools_default_migration_v1"

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = _load_addon_start()

    def _make_options(self, tmp_path, value):
        """Write an options.json with enable_skills_as_tools=value."""
        config_file = tmp_path / "options.json"
        with open(config_file, "w") as f:
            json.dump({"enable_skills_as_tools": value}, f)
        return config_file

    def test_migration_flips_stored_false_and_persists(self, tmp_path):
        """First boot after update: stored False gets forced to True and
        persisted to options.json so the UI reflects the new value."""
        config_file = self._make_options(tmp_path, False)

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=False,
            config_read_ok=True,
        )

        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()
        with open(config_file) as f:
            assert json.load(f)["enable_skills_as_tools"] is True

    def test_migration_respects_marker_when_exists(self, tmp_path):
        """After migration has run, respect the user's stored value even if
        it is False (user deliberately toggled it off)."""
        config_file = self._make_options(tmp_path, False)
        (tmp_path / self.MARKER_NAME).touch()

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=False,
            config_read_ok=True,
        )

        assert result is False
        # Marker should still exist; options.json untouched.
        assert (tmp_path / self.MARKER_NAME).exists()
        with open(config_file) as f:
            assert json.load(f)["enable_skills_as_tools"] is False

    def test_migration_creates_marker_when_stored_true(self, tmp_path):
        """First boot, stored already True: no persistence needed, but the
        marker must still be created so a future user-initiated False is
        respected."""
        config_file = self._make_options(tmp_path, True)

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=True,
            config_read_ok=True,
        )

        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()

    def test_migration_survives_missing_options_json(self, tmp_path):
        """If options.json does not exist, the migration still applies the
        runtime override and creates the marker — no crash."""
        config_file = tmp_path / "options.json"  # Does not exist

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=False,
            config_read_ok=True,
        )

        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()

    def test_migration_survives_options_json_write_failure(self, tmp_path):
        """If persisting to options.json fails (read-only filesystem), the
        runtime override is still applied, the marker is still created so
        the migration does not loop, and the on-disk options.json is left
        unmodified."""
        config_file = self._make_options(tmp_path, False)
        # Make the file read-only so the migration's write fails at the OS
        # layer rather than via a mock coupled to the current open() call
        # sites. chmod on the file alone is sufficient on POSIX because
        # open(..., "w") rechecks file permissions.
        config_file.chmod(0o444)

        try:
            result = self.addon.migrate_skills_as_tools_default(
                data_dir=tmp_path,
                config_file=config_file,
                stored_value=False,
                config_read_ok=True,
            )
        finally:
            # Restore write permission so tmp_path cleanup works on all
            # runners.
            config_file.chmod(0o644)

        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()
        # options.json must remain unmodified — verifies the write failed
        # before touching disk, not merely that the function didn't crash.
        with open(config_file) as f:
            assert json.load(f)["enable_skills_as_tools"] is False

    def test_migration_respects_marker_with_stored_true(self, tmp_path):
        """Marker exists, stored True: respect stored, no rewrite."""
        config_file = self._make_options(tmp_path, True)
        (tmp_path / self.MARKER_NAME).touch()

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=True,
            config_read_ok=True,
        )

        assert result is True

    def test_migration_survives_malformed_options_json(self, tmp_path):
        """Corrupt options.json (JSONDecodeError) is logged, runtime override
        still applied, marker still created — migration does not loop."""
        config_file = tmp_path / "options.json"
        with open(config_file, "w", encoding="utf-8") as f:
            f.write("{not valid json")

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=False,
            config_read_ok=True,
        )

        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()

    @pytest.mark.parametrize("payload", ["[]", '"just a string"', "null", "42"])
    def test_migration_logs_non_dict_top_level(self, tmp_path, payload, capsys):
        """Parsed-but-non-dict options.json (list, string, null, number) is
        observable in logs rather than silently skipped, and options.json
        stays in its original state."""
        config_file = tmp_path / "options.json"
        config_file.write_text(payload, encoding="utf-8")

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=False,
            config_read_ok=True,
        )

        # Runtime override still applied, marker still created.
        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()
        # options.json untouched.
        assert config_file.read_text(encoding="utf-8") == payload
        # The non-dict branch must log something observable.
        captured = capsys.readouterr()
        assert "options.json" in (captured.out + captured.err)
        assert "expected dict" in (captured.out + captured.err)

    def test_migration_skips_marker_when_config_unreadable(self, tmp_path):
        """If main() could not read options.json (malformed JSON or I/O
        error), the migration must not create the marker. Otherwise, once
        options.json recovers with the user's real stored False, the
        migration would never run and the intended force-to-true would be
        silently lost."""
        config_file = tmp_path / "options.json"
        # Does not exist — simulates an unreadable file. The important
        # signal is config_read_ok=False, which is the flag main() would
        # set after a json.JSONDecodeError.
        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=True,  # fallback default used by main()
            config_read_ok=False,
        )

        # Runtime default still applied, but marker must NOT be created so
        # the migration can run again on a later boot.
        assert result is True
        assert not (tmp_path / self.MARKER_NAME).exists()


IMAGE_TAG = "ha-mcp-addon-test"
DOCKERFILE = "homeassistant-addon/Dockerfile"


def _build_addon_image():
    """Build the addon test image via docker CLI (supports BuildKit)."""
    result = subprocess.run(
        [
            "docker", "build",
            "-t", IMAGE_TAG,
            "-f", DOCKERFILE,
            "--build-arg", "BUILD_VERSION=1.0.0-test",
            "--build-arg", "BUILD_ARCH=amd64",
            ".",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to build {IMAGE_TAG}:\n{result.stderr}")


class TestResolveBoolOption:
    """Unit tests for the resolve_bool_option helper used for verify_ssl."""

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = _load_addon_start()

    def test_missing_key_returns_default(self):
        assert self.addon.resolve_bool_option({}, "verify_ssl", True) is True
        assert self.addon.resolve_bool_option({}, "verify_ssl", False) is False

    def test_explicit_false_returns_false(self):
        assert (
            self.addon.resolve_bool_option({"verify_ssl": False}, "verify_ssl", True)
            is False
        )

    def test_explicit_true_returns_true(self):
        assert (
            self.addon.resolve_bool_option({"verify_ssl": True}, "verify_ssl", False)
            is True
        )

    def test_string_value_falls_back_to_default(self):
        # HA Supervisor coerces YAML scalars to the schema type, so a string
        # here means user-edited options.json with a bad type. The secure
        # default must win — never accept "false" as a string.
        assert (
            self.addon.resolve_bool_option({"verify_ssl": "false"}, "verify_ssl", True)
            is True
        )

    def test_int_value_falls_back_to_default(self):
        assert (
            self.addon.resolve_bool_option({"verify_ssl": 0}, "verify_ssl", True)
            is True
        )

    def test_none_value_falls_back_to_default(self):
        assert (
            self.addon.resolve_bool_option({"verify_ssl": None}, "verify_ssl", True)
            is True
        )


@pytest.mark.slow
class TestAddonStartup:
    """Test add-on container startup behavior."""

    @pytest.fixture(autouse=True, scope="class")
    def build_image(self):
        """Build the addon image once before all tests in this class."""
        _build_addon_image()

    @pytest.fixture
    def addon_config(self, tmp_path):
        """Create a test add-on configuration file."""
        config = {
            "backup_hint": "normal",
            "secret_path": "",  # Auto-generate
        }
        config_file = tmp_path / "options.json"
        with open(config_file, "w") as f:
            json.dump(config, f)
        return config_file

    @pytest.fixture
    def container(self, addon_config):
        """Create the add-on container for testing (image built by build_image fixture)."""
        return (
            DockerContainer(image=IMAGE_TAG)
            .with_bind_ports(9583, 9583)
            .with_env("SUPERVISOR_TOKEN", "test-supervisor-token")
            .with_env("HOMEASSISTANT_URL", "http://supervisor/core")
            .with_volume_mapping(str(addon_config.parent), "/data", mode="rw")
        )

    def test_addon_startup_logs(self, container):
        """Test that add-on produces expected startup logs."""
        # Configure wait strategy for server actually starting
        container.waiting_for(
            LogMessageWaitStrategy("Uvicorn running on").with_startup_timeout(30)
        )

        # Start container
        container.start()

        try:
            # Get logs (both stdout and stderr)
            stdout, stderr = container.get_logs()
            logs = stdout.decode("utf-8") + "\n" + stderr.decode("utf-8")

            # Verify expected log messages
            assert "[INFO] Starting Home Assistant MCP Server..." in logs
            assert "[INFO] Backup hint mode: normal" in logs
            assert "[INFO] Generated new secret path with 128-bit entropy" in logs
            assert "[INFO] Home Assistant URL: http://supervisor/core" in logs
            assert "🔐 MCP Server URL: http://<home-assistant-ip>:9583/private_" in logs
            assert "Secret Path: /private_" in logs
            assert "⚠️  IMPORTANT: Copy this exact URL - the secret path is required!" in logs

            # Verify debug messages
            assert "[INFO] Importing ha_mcp module..." in logs
            assert "[INFO] Starting MCP server..." in logs

            # Verify FastMCP started successfully
            assert "Starting MCP server 'ha-mcp'" in logs
            assert "Uvicorn running on http://0.0.0.0:9583" in logs

            # Should not have errors
            assert "[ERROR] Failed to start MCP server:" not in logs

        finally:
            container.stop()

    def test_addon_startup_custom_secret_path(self, tmp_path):
        """Test that add-on uses custom secret path when configured."""
        # Create config with custom secret path
        config = {
            "backup_hint": "strong",
            "secret_path": "/my_custom_secret",
        }
        config_file = tmp_path / "options.json"
        with open(config_file, "w") as f:
            json.dump(config, f)

        container = (
            DockerContainer(image=IMAGE_TAG)
            .with_bind_ports(9583, 9583)
            .with_env("SUPERVISOR_TOKEN", "test-supervisor-token")
            .with_env("HOMEASSISTANT_URL", "http://supervisor/core")
            .with_volume_mapping(str(config_file.parent), "/data", mode="rw")
        )

        # Configure wait strategy
        container.waiting_for(
            LogMessageWaitStrategy("MCP Server URL:").with_startup_timeout(30)
        )

        container.start()

        try:
            # Get logs
            logs = container.get_logs()[0].decode("utf-8")

            # Verify custom config is used
            assert "[INFO] Backup hint mode: strong" in logs
            assert "[INFO] Using custom secret path from configuration" in logs
            assert "🔐 MCP Server URL: http://<home-assistant-ip>:9583/my_custom_secret" in logs
            assert "Secret Path: /my_custom_secret" in logs

        finally:
            container.stop()

    def test_addon_startup_missing_supervisor_token(self, addon_config):
        """Test that add-on exits with error when SUPERVISOR_TOKEN is missing."""
        container = (
            DockerContainer(image=IMAGE_TAG)
            .with_bind_ports(9583, 9583)
            .with_volume_mapping(str(addon_config.parent), "/data", mode="ro")
        )

        container.start()

        try:
            # Wait a bit for container to start and error
            time.sleep(3)

            # Get logs (both stdout and stderr)
            stdout, stderr = container.get_logs()
            logs = stdout.decode("utf-8") + "\n" + stderr.decode("utf-8")

            # Verify error message
            assert "[ERROR] SUPERVISOR_TOKEN not found! Cannot authenticate." in logs

        finally:
            container.stop()
