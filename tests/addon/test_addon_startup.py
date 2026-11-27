"""Test Home Assistant add-on startup and logging."""

import asyncio
import json
import os
import socket
import subprocess
import urllib.error
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_socket
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from tests.addon.docker_build import python_base_args
from tests.src.unit._tools_addons_core import Supervisor
from tests.src.unit._tools_addons_core import socket_supervisor as socket_supervisor
from tests.src.unit._tools_addons_core import supervisor as supervisor
from tests.src.unit._tools_addons_core import target_app as target_app

from ._addon_start import load_addon_start

OPTIONS = "/addons/self/options"


class TestSecretPathValidation:
    """Unit tests for secret path validation logic."""

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = load_addon_start()

    def test_stored_path_of_minimum_length_is_kept(self, tmp_path):
        """The shortest valid path (8 characters) keeps the user's MCP URL."""
        (tmp_path / "secret_path.txt").write_text("/mysecrt")
        result = self.addon.get_or_create_secret_path(tmp_path)
        assert result == "/mysecrt"

    @pytest.mark.parametrize(
        "stored",
        [
            "http://127.0.0.1:9583/private_abc123",
            "no-leading-slash",
            "/custom",
            "/https://evil.com",
        ],
        ids=["full URL", "no leading slash", "too short", "scheme after slash"],
    )
    def test_invalid_stored_path_is_regenerated(self, tmp_path, stored):
        """A stored value that is not a bare path would serve the server
        under a URL no client can reach; it is replaced and rewritten."""
        secret_file = tmp_path / "secret_path.txt"
        secret_file.write_text(stored)
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


@pytest.fixture
def supervisor_address(monkeypatch) -> list[int]:
    """The port the app's fixed ``supervisor`` hostname leads to."""
    port_number: list[int] = []
    resolve = socket.getaddrinfo
    monkeypatch.setenv("no_proxy", "supervisor,127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "supervisor,127.0.0.1")

    def address(host, port, *args, **kwargs):
        if host == "supervisor" and port == 80:
            host, port = "127.0.0.1", port_number[0]
        return resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", address)
    return port_number


@pytest.fixture
def options_supervisor(supervisor: Supervisor, supervisor_address) -> Supervisor:
    """The shared Supervisor, answering at the app's fixed hostname."""
    supervisor_address.append(int(supervisor.url.rpartition(":")[2]))
    return supervisor


def _closed_port() -> int:
    """A local port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def addon_stderr(monkeypatch):
    """Capture startup errors even while pytest streams the HTTP server's access log."""
    stream = StringIO()
    monkeypatch.setattr(load_addon_start(), "sys", SimpleNamespace(stderr=stream))
    return stream


class TestMaybePersistSecretPath:
    @pytest.mark.parametrize("config", [{}, {"secret_path": "/private_same"}])
    async def test_skips_a_redundant_or_empty_options_write(
        self, options_supervisor, config
    ):
        addon = load_addon_start()
        await asyncio.to_thread(
            addon.maybe_persist_secret_path, config, "/private_same", "token"
        )
        assert options_supervisor.requests == []

    async def test_persists_the_full_config_and_the_app_token(self, options_supervisor):
        options_supervisor.answer("POST", OPTIONS)
        addon = load_addon_start()
        config = {"verify_ssl": True, "secret_path": "/private_old"}
        await asyncio.to_thread(
            addon.maybe_persist_secret_path, config, "/private_new", "test-token"
        )
        headers = options_supervisor.headers("POST", OPTIONS)
        assert headers["Authorization"] == "Bearer test-token"
        assert headers["Content-Type"] == "application/json"
        assert options_supervisor.sent("POST", OPTIONS) == [
            {"options": {"verify_ssl": True, "secret_path": "/private_new"}}
        ]

    async def test_persists_over_the_supervisor_socket_without_the_token(
        self, socket_supervisor
    ):
        """Supervisor deprecates the app's token over TCP: with the app's
        socket mounted the options go over it, and no token is sent there."""
        socket_supervisor.answer("POST", OPTIONS)
        addon = load_addon_start()
        await asyncio.to_thread(
            addon.persist_addon_options, {"verify_ssl": True}, "test-token"
        )
        assert "Authorization" not in socket_supervisor.headers("POST", OPTIONS)
        assert socket_supervisor.sent("POST", OPTIONS) == [
            {"options": {"verify_ssl": True}}
        ]

    async def test_refused_options_raise_the_supervisors_status(
        self, options_supervisor
    ):
        options_supervisor.refuse("POST", OPTIONS, "invalid options")
        addon = load_addon_start()
        with pytest.raises(urllib.error.HTTPError) as error:
            await asyncio.to_thread(addon.persist_addon_options, {}, "token")
        assert error.value.code == 400

    async def test_refused_options_log_the_recovery_path(
        self, options_supervisor, addon_stderr
    ):
        options_supervisor.refuse("POST", OPTIONS, "invalid options")
        addon = load_addon_start()
        await asyncio.to_thread(
            addon.maybe_persist_secret_path,
            {"verify_ssl": True},
            "/private_new",
            "token",
        )
        err = addon_stderr.getvalue()
        assert "HTTP 400" in err
        assert "/private_new" in err
        assert "Secret path override" in err

    async def test_an_unreachable_supervisor_raises(
        self, socket_enabled, supervisor_address
    ):
        supervisor_address.append(_closed_port())
        addon = load_addon_start()
        with pytest.raises(urllib.error.URLError):
            await asyncio.to_thread(addon.persist_addon_options, {}, "token")

    async def test_an_unreachable_supervisor_is_logged_at_startup(
        self, socket_enabled, supervisor_address, addon_stderr
    ):
        supervisor_address.append(_closed_port())
        addon = load_addon_start()
        await asyncio.to_thread(
            addon.maybe_persist_secret_path,
            {"verify_ssl": True},
            "/private_new",
            "token",
        )
        err = addon_stderr.getvalue()
        assert "Failed to persist secret_path" in err
        assert "/private_new" in err


IMAGE_TAG = "ha-mcp-addon-test"
DOCKERFILE = "homeassistant-addon/Dockerfile"


def _build_addon_image():
    """Build the addon test image via docker CLI (supports BuildKit)."""
    result = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            IMAGE_TAG,
            "-f",
            DOCKERFILE,
            "--build-arg",
            "BUILD_VERSION=1.0.0-test",
            "--build-arg",
            "BUILD_ARCH=amd64",
            *python_base_args(),
            ".",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to build {IMAGE_TAG}:\n{result.stderr}")


def _valid_non_default(option):
    """A valid value for ``option`` that differs from its default."""
    if option["type"] == "bool":
        return not option["default"]
    if option["choices"] is not None:
        return next(c for c in option["choices"] if c != option["default"])
    if option["range"] is not None:
        lo, hi = option["range"]
        return hi if option["default"] != hi else lo
    if option["type"] == "int":
        return option["default"] + 1
    return "tool_a,tool_b"


class TestAppOptions:
    """start.py exports each app option to the env var the server reads."""

    @pytest.fixture(autouse=True)
    def addon(self, monkeypatch):
        self.addon = load_addon_start()
        self.options = self.addon.load_app_options()
        # The exports write os.environ directly. setenv records the value
        # before the test, so teardown restores or removes each var.
        for env in [o["env"] for o in self.options] + ["CODE_MODE_SAVED_TOOLS_PATH"]:
            monkeypatch.setenv(env, "")
            monkeypatch.delenv(env)

    def test_every_app_option_reaches_the_server_setting(self):
        """An option exported under the wrong env var or in a format the
        server does not parse would leave the Configuration page toggle
        with no effect."""
        from ha_mcp.config_settings import Settings

        config = {option["key"]: _valid_non_default(option) for option in self.options}

        self.addon.export_app_options(config, self.options)

        settings = Settings(_env_file=None)
        for key, value in config.items():
            assert getattr(settings, key) == value, key

    @pytest.mark.parametrize(
        ("key", "raw"),
        [
            ("verify_ssl", "false"),
            ("ha_tool_concurrency", True),
            ("ha_tool_concurrency", 33),
            ("disabled_tools", 5),
        ],
        ids=[
            "bool as text",
            "bool as int",
            "out of range",
            "int as text",
        ],
    )
    def test_invalid_value_warns_and_uses_the_default(self, key, raw, capsys):
        """Supervisor validates the schema, so an invalid value means a
        hand-edited options.json. The server must get the default, and the
        log must say why the user's value was ignored."""
        option = next(o for o in self.options if o["key"] == key)

        exported = self.addon.export_app_options({key: raw}, [option])

        assert exported[key] == option["default"]
        assert (
            f"app option {key!r} has invalid value {raw!r}" in capsys.readouterr().err
        )

    def test_value_outside_the_choices_warns_and_uses_the_default(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Driven with a synthetic option so the choices check stays covered
        whether or not a real app option declares choices."""
        option = {
            "key": "test_choice",
            "type": "str",
            "default": "normal",
            "range": None,
            "choices": ["strong", "normal"],
        }

        value = self.addon.resolve_option({"test_choice": "loud"}, option)

        assert value == "normal"
        assert "app option 'test_choice' has invalid value 'loud'" in (
            capsys.readouterr().err
        )

    def test_option_missing_from_a_flavor_is_exported_only_when_present(self):
        """The stable flavor does not declare the beta keys. Exporting them
        there would mark them app-managed in the web UI, and Supervisor
        drops a key the stable schema lacks, so the save would silently not
        persist."""
        beta = next(o for o in self.options if not o["always"])

        assert beta["key"] not in self.addon.export_app_options({}, [beta])
        assert beta["env"] not in os.environ
        self.addon.export_app_options({beta["key"]: True}, [beta])
        assert os.environ[beta["env"]] == "true"

    def test_gated_off_warning_names_only_subflags_turned_on(self, capsys):
        """With the beta master off, a sub-flag a user turned on will be
        forced off at runtime; the log must name it. A sub-flag that is on
        by default (the YAML edit confirmation) was not turned on by the
        user and must not be named."""
        subflags = self.addon.beta_subflags(self.options)
        defaults = {o["key"]: o["default"] for o in self.options}
        config = {"enable_beta_features": False, "enable_code_mode": True}
        config.update({k: True for k in subflags if defaults[k]})

        self.addon.export_options_env(config)

        out = capsys.readouterr().out
        assert "enable_code_mode" in out
        assert not any(k in out for k in subflags if defaults[k])


class TestResolveEffectiveLogLevel:
    """Unit tests for resolve_effective_log_level (#1721).

    The addon hardcoded ``logging.basicConfig(level=logging.INFO)``, so the
    web Settings UI's "Log level" advanced setting could never reach the
    addon container log — reporters asked to "enable debug logging" saw
    INFO-only output. The helper reads the effective settings (including
    the feature_flags.json override) and falls back to INFO on any failure
    so logging config can never block addon startup.
    """

    @pytest.fixture(autouse=True)
    def addon(self, tmp_path, monkeypatch):
        from ha_mcp.config import reset_global_settings
        from ha_mcp.utils.data_paths import get_data_dir

        self.addon = load_addon_start()
        self.data_dir = tmp_path
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        get_data_dir.cache_clear()
        reset_global_settings()
        yield
        get_data_dir.cache_clear()
        reset_global_settings()

    def test_web_ui_debug_level_reaches_the_addon_log(self):
        import logging

        (self.data_dir / "feature_flags.json").write_text(
            json.dumps({"log_level": "DEBUG"})
        )
        assert self.addon.resolve_effective_log_level() == logging.DEBUG

    def test_unreadable_settings_fall_back_to_info_loudly(self, monkeypatch, capsys):
        """A silent INFO fallback recreates the "user set DEBUG, nothing
        happened, no clue why" bug class."""
        import logging

        monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
        assert self.addon.resolve_effective_log_level() == logging.INFO
        captured = capsys.readouterr()
        assert "web-UI Log level not applied" in captured.err
        assert "VERBOSE" in captured.err

    def test_advanced_debug_logging_toggle_removed(self):
        """The confusing addon-config toggle is gone everywhere.

        Kill-signal diagnostics now arm when the effective log level is
        DEBUG; no separate toggle. Guards against reintroduction in any
        addon flavor (functional config is hand-mirrored, not synced).
        """
        repo_root = Path(__file__).parents[2]
        for rel in (
            "homeassistant-addon/start.py",
            "homeassistant-addon/config.yaml",
            "homeassistant-addon/translations/en.yaml",
            "homeassistant-addon-dev/config.yaml",
            "homeassistant-addon-dev/translations/en.yaml",
            "tests/haos_image_build/build_image.py",
        ):
            content = (repo_root / rel).read_text(encoding="utf-8")
            assert "advanced_debug_logging" not in content, (
                f"{rel} still references the removed advanced_debug_logging toggle"
            )


class TestWidenFastmcpLogConsole:
    """Unit tests for widen_fastmcp_log_console (#1918).

    FastMCP logs its startup line ("Starting MCP server ... on
    http://0.0.0.0:9583/<secret>") through a rich console that defaults to
    80 columns when stderr is not a TTY, wrapping the URL mid-secret. A
    secret split across lines survives the find/replace users do to
    sanitize logs before sharing them.
    """

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = load_addon_start()

    def test_startup_url_stays_on_one_line(self):
        import io
        import logging

        from rich.console import Console
        from rich.logging import RichHandler

        stream = io.StringIO()
        # Explicit width=80 mirrors rich's non-TTY default in the container.
        handler = RichHandler(console=Console(file=stream, width=80))
        logger = logging.getLogger("fastmcp")
        # Detach any real fastmcp handlers so the widen only touches the
        # test's handler (and the fake URL line stays out of stderr).
        saved = logger.handlers[:]
        for pre_existing in saved:
            logger.removeHandler(pre_existing)
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            self.addon.widen_fastmcp_log_console()
            url = f"http://0.0.0.0:9583{self.addon.generate_secret_path()}"
            logger.info(
                "Starting MCP server 'ha-mcp' with transport 'http' (stateless) on %s",
                url,
            )
            assert url in stream.getvalue()
        finally:
            logger.removeHandler(handler)
            for pre_existing in saved:
                logger.addHandler(pre_existing)
            logger.setLevel(old_level)

    def test_non_rich_handlers_untouched(self):
        import io
        import logging

        from rich.console import Console
        from rich.logging import RichHandler

        logger = logging.getLogger("fastmcp")
        plain = logging.StreamHandler()
        rich_handler = RichHandler(console=Console(file=io.StringIO(), width=80))
        saved = logger.handlers[:]
        for pre_existing in saved:
            logger.removeHandler(pre_existing)
        logger.addHandler(plain)
        logger.addHandler(rich_handler)
        try:
            # Must not raise on the console-less StreamHandler, and must
            # still widen the rich handler sitting next to it.
            self.addon.widen_fastmcp_log_console()
            assert rich_handler.console.width == 200
            assert not hasattr(plain, "console")
        finally:
            logger.removeHandler(plain)
            logger.removeHandler(rich_handler)
            for pre_existing in saved:
                logger.addHandler(pre_existing)

    def test_warns_when_no_rich_handlers(self, capfd):
        """Going inert must leave a signal — support can't otherwise tell
        whether the widening took effect (same rationale as the DEBUG
        canary in main())."""
        import logging

        logger = logging.getLogger("fastmcp")
        saved = logger.handlers[:]
        for handler in saved:
            logger.removeHandler(handler)
        try:
            self.addon.widen_fastmcp_log_console()
        finally:
            for handler in saved:
                logger.addHandler(handler)
        assert "No rich handlers" in capfd.readouterr().err

    def test_main_calls_widen_at_startup(self):
        """The fix only works if main() actually invokes it after the
        ha_mcp import attaches fastmcp's handlers — guard the call site."""
        content = (
            Path(__file__).parents[2] / "homeassistant-addon" / "start.py"
        ).read_text(encoding="utf-8")
        assert "widen_fastmcp_log_console()" in content


class TestCleanupStaleMigrationMarker:
    """Unit tests for cleanup_stale_migration_marker.

    Best-effort cleanup of the one-time enable_skills_as_tools migration
    marker (removed in #1133). Runs on every boot, so silent failure on
    permission-restricted /data mounts is the production-only failure
    mode worth locking down.
    """

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = load_addon_start()

    def test_removes_marker_when_present(self, tmp_path):
        marker = tmp_path / ".skills_as_tools_default_migration_v1"
        marker.write_text("")
        assert marker.exists()

        self.addon.cleanup_stale_migration_marker(tmp_path)

        assert not marker.exists()

    def test_noop_when_marker_absent(self, tmp_path):
        # No marker present — must not raise (missing_ok=True).
        self.addon.cleanup_stale_migration_marker(tmp_path)

    def test_swallows_oserror_and_logs(self, tmp_path, monkeypatch, capsys):
        """Permission-restricted /data mounts must not crash boot.

        Simulates the real-world failure mode where unlink raises despite
        ``missing_ok=True`` (e.g. EACCES on a read-only bind mount).
        """
        marker = tmp_path / ".skills_as_tools_default_migration_v1"
        marker.write_text("")

        def boom(self, missing_ok=False):
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr(Path, "unlink", boom)

        self.addon.cleanup_stale_migration_marker(tmp_path)

        captured = capsys.readouterr()
        assert "Failed to remove stale migration marker" in captured.err
        assert "Safe to ignore" in captured.err


@pytest.mark.slow
class TestAddonStartup:
    """Test add-on container startup behavior."""

    @pytest.fixture(autouse=True, scope="class")
    def build_image(self):
        """Build the addon image once before all tests in this class."""
        _build_addon_image()

    @pytest.fixture(autouse=True)
    def docker_socket(self, socket_enabled):
        """Let Core's test plugin pass the Docker daemon's Unix socket through."""
        pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)

    @pytest.fixture
    def addon_config(self, tmp_path):
        """Create a test add-on configuration file."""
        config = {
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
            assert "[INFO] Generated new secret path with 128-bit entropy" in logs
            assert "[INFO] Home Assistant URL: http://supervisor/core" in logs
            assert "🔐 MCP Server URL: http://<home-assistant-ip>:9583/private_" in logs
            assert "Secret Path: /private_" in logs
            assert (
                "⚠️  IMPORTANT: Copy this exact URL - the secret path is required!"
                in logs
            )

            # Verify debug messages
            assert "[INFO] Importing ha_mcp module..." in logs
            assert "[INFO] Starting MCP server..." in logs

            # Verify FastMCP started successfully
            assert "Starting MCP server 'ha-mcp'" in logs
            assert "Uvicorn running on http://0.0.0.0:9583" in logs

            # Should not have errors
            assert "[ERROR] Failed to start MCP server:" not in logs

            # Default config = INFO level: the DEBUG canary must be
            # absent and kill-signal diagnostics must NOT arm. Guards
            # the arming gate against regressing to always-on (the
            # SA_SIGINFO handler must stay opt-in via DEBUG).
            assert "Debug logging active (log_level applied from settings)" not in logs
            assert "arming kill-signal diagnostics" not in logs
            assert "kill-signal diagnostics installed for" not in logs

        finally:
            container.stop()

    def test_addon_startup_custom_secret_path(self, tmp_path):
        """Test that add-on uses custom secret path when configured."""
        # Create config with custom secret path
        config = {
            "secret_path": "/my_custom_secret",
        }
        config_file = tmp_path / "options.json"
        with open(config_file, "w") as f:
            json.dump(config, f)

        container = (
            DockerContainer(image=IMAGE_TAG)
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
            assert "[INFO] Using custom secret path from configuration" in logs
            assert (
                "🔐 MCP Server URL: http://<home-assistant-ip>:9583/my_custom_secret"
                in logs
            )
            assert "Secret Path: /my_custom_secret" in logs

        finally:
            container.stop()

    def test_web_ui_debug_log_level_reaches_addon_logs(self, tmp_path):
        """Regression for the #1721 diagnosis: the web Settings UI
        "Log level" advanced setting (persisted to
        ``/data/feature_flags.json``) must actually change addon log
        verbosity. start.py hardcoded ``basicConfig(level=INFO)``, so
        reporters asked to "enable debug logging" could never produce
        DEBUG output. Also covers the DEBUG-armed kill-signal
        diagnostics (which replaced the advanced_debug_logging toggle).
        """
        config_file = tmp_path / "options.json"
        with open(config_file, "w") as f:
            json.dump({"secret_path": ""}, f)
        with open(tmp_path / "feature_flags.json", "w") as f:
            json.dump({"log_level": "DEBUG"}, f)

        container = (
            DockerContainer(image=IMAGE_TAG)
            .with_env("SUPERVISOR_TOKEN", "test-supervisor-token")
            .with_env("HOMEASSISTANT_URL", "http://supervisor/core")
            .with_volume_mapping(str(tmp_path), "/data", mode="rw")
        )
        # The deferred install thread logs its confirmation after uvicorn
        # reports it is running, so the wait keys on that later line.
        container.waiting_for(
            LogMessageWaitStrategy(
                "kill-signal diagnostics installed for"
            ).with_startup_timeout(45)
        )
        container.start()

        try:
            stdout, stderr = container.get_logs()
            logs = stdout.decode("utf-8") + "\n" + stderr.decode("utf-8")

            # The canary is emitted at DEBUG through the logging system,
            # so its presence proves the level override was applied.
            assert "Debug logging active (log_level applied from settings)" in logs
            # DEBUG also arms the kill-signal diagnostics.
            assert "arming kill-signal diagnostics" in logs
            assert "kill-signal diagnostics installed for" in logs
        finally:
            container.stop()

    def test_addon_startup_missing_supervisor_token(self, addon_config):
        """Test that add-on exits with error when SUPERVISOR_TOKEN is missing."""
        container = DockerContainer(image=IMAGE_TAG).with_volume_mapping(
            str(addon_config.parent), "/data", mode="ro"
        )
        # The wait reads the logs before it checks whether the container
        # exited, so it also returns for this container that stops at once.
        container.waiting_for(
            LogMessageWaitStrategy("SUPERVISOR_TOKEN not found").with_startup_timeout(
                30
            )
        )

        container.start()

        try:
            # Get logs (both stdout and stderr)
            stdout, stderr = container.get_logs()
            logs = stdout.decode("utf-8") + "\n" + stderr.decode("utf-8")

            # Verify error message
            assert "[ERROR] SUPERVISOR_TOKEN not found! Cannot authenticate." in logs

        finally:
            container.stop()


class TestBetaMasterAutoEnableInDevAddon:
    """Dev addon keeps the beta sub-flag keys in its Supervisor options
    schema (unlike stable). start.py auto-writes
    ``ENABLE_BETA_FEATURES=true`` whenever any of those sub-flag keys
    are present in ``/data/options.json``, so the runtime master gate
    in ``_apply_feature_flag_overrides`` becomes a no-op for dev addon
    users — Supervisor options remain authoritative for the beta sub-flags."""

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = load_addon_start()
        self.subflags = self.addon.beta_subflags(self.addon.load_app_options())

    def test_auto_enable_writes_true_when_any_beta_key_truthy(self, monkeypatch):
        """Dev-addon options with ANY beta sub-flag set to True →
        ENABLE_BETA_FEATURES=true written to env."""
        monkeypatch.delenv("ENABLE_BETA_FEATURES", raising=False)
        self.addon.maybe_auto_enable_beta_master(
            {"verify_ssl": True, "enable_yaml_config_editing": True},
            self.subflags,
        )
        assert os.environ.get("ENABLE_BETA_FEATURES") == "true"

    def test_auto_enable_skips_when_all_beta_keys_false(self, monkeypatch):
        """HA Supervisor persists every schema-declared option to
        options.json with its default value on first start, so all 5
        beta keys land there with ``false`` immediately on a fresh
        dev-addon install. The auto-enable must NOT fire in that case
        — the master should follow the user's actual sub-flag choices,
        not the bare presence of the schema keys. Previously this test
        asserted the opposite (presence alone fires) and the user hit
        the resulting "locked-on master, no sub-flags enabled" bug
        """
        monkeypatch.delenv("ENABLE_BETA_FEATURES", raising=False)
        self.addon.maybe_auto_enable_beta_master(
            dict.fromkeys(self.subflags, False), self.subflags
        )
        assert "ENABLE_BETA_FEATURES" not in os.environ

    def test_auto_enable_skips_non_bool_truthy_values(self, monkeypatch):
        """Defensive: only Python ``True`` triggers, not a truthy
        non-bool. Supervisor's JSON parsing returns proper bools, but
        if a future malformed options.json or test fixture passes a
        truthy string, we want the gate to stay shut rather than
        silently flip the master on."""
        monkeypatch.delenv("ENABLE_BETA_FEATURES", raising=False)
        self.addon.maybe_auto_enable_beta_master(
            {"enable_yaml_config_editing": "true"}, self.subflags
        )
        assert "ENABLE_BETA_FEATURES" not in os.environ

    def test_auto_enable_does_not_set_var_when_no_beta_key(self, monkeypatch):
        """Stable-addon options never carry any beta key → no auto-
        enable. Master stays at the pydantic default (False) so the
        UI master toggle remains the gate."""
        monkeypatch.delenv("ENABLE_BETA_FEATURES", raising=False)
        self.addon.maybe_auto_enable_beta_master(
            {"verify_ssl": True, "enable_tool_search": True}, self.subflags
        )
        assert "ENABLE_BETA_FEATURES" not in os.environ

    def test_auto_enable_keys_match_BETA_FEATURE_FIELDS_registry(self):
        """The keys checked by start.py must exactly match the
        runtime registry — drift would silently break the auto-enable
        signal for dev-addon users."""
        from ha_mcp.config import BETA_FEATURE_FIELDS

        assert set(self.subflags) == set(BETA_FEATURE_FIELDS), (
            "start.py's beta sub-flags differ from config.BETA_FEATURE_FIELDS"
        )


def test_the_webhook_proxy_ships_the_apps_supervisor_transport() -> None:
    """Both apps send Supervisor requests through one transport module; each
    image copies its own file, so the two copies must stay the same."""
    repo = Path(__file__).parents[2]
    shared = "supervisor_transport.py"
    assert (repo / "homeassistant-addon" / shared).read_bytes() == (
        repo / "homeassistant-addon-webhook-proxy-dev" / shared
    ).read_bytes()
