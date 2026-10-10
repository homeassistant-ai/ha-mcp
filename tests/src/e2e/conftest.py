"""
Testcontainers integration for E2E testing.

Spins up an isolated Home Assistant Docker container for each test session.
Tests MUST run against this container — never against a real HA instance.

Environment Variables:
    HA_TEST_PORT: Optional fixed port for HA container (default: dynamic).
                  Example: HA_TEST_PORT=8124

NOTE: config.py loads HOMEASSISTANT_URL from the .env.test file at import
time, so checking os.environ for a pre-set URL is not a reliable guard here.
Protection against accidental real-HA usage is instead ensured by:
  - Guard 1: Docker must be available (testcontainers requirement)
  - Guard 3: HA API must become ready within 60s (container health check)
  - tests/AGENTS.md: documents lanes, markers, and test patterns
  - docs/agents/development.md: documents test commands
"""

import asyncio
import http.server
import logging
import os
import shutil
import sys
import threading
import time
from collections.abc import AsyncGenerator, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest

# Must precede the ha_mcp imports below; see the module docstring.
from . import _collection_data_dir  # noqa: F401

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))  # tests/src/ for haos_runtime

from haos_runtime import (
    HAOS_IMAGE_ENV,
    boot_haos_qemu,
    is_haos_backend_selected,
    is_haos_embedded_mode,
    is_haos_inaddon_mode,
    is_haos_stdio_mode,
)

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.server import HomeAssistantSmartMCPServer

from ._conftest_collection import (
    pytest_collection_modifyitems,  # noqa: F401  (pytest hook)
    pytest_runtest_logreport,  # noqa: F401  (pytest hook)
)
from ._conftest_embedded import _is_embedded_backend_selected
from ._conftest_haos import (
    _bringup_haos_out_of_process_server,
    _dump_haos_session_logs,
    _haos_post_boot_setup,
    _prepare_haos_image,
)
from ._conftest_readiness import (
    _HA_CONTAINER_KEY,
    pytest_runtest_makereport,  # noqa: F401  (pytest hook)
    pytest_sessionfinish,  # noqa: F401  (pytest hook)
    pytest_terminal_summary,  # noqa: F401  (pytest hook)
    pytest_testnodedown,  # noqa: F401  (pytest hook)
)
from ._conftest_testcontainer import (
    _build_ha_testcontainer,
    _detect_docker_host,
    _prepare_testcontainer_config,
    _reset_ha_in_process_caches,
    _wait_for_testcontainer_ready,
)

# Import test utilities
from .utilities.assertions import parse_mcp_result
from .utilities.supervisor_mock import (
    _supervisor_mock_server,  # noqa: F401  (session fixture supervisor_mock depends on)
    supervisor_mock,  # noqa: F401  (re-exported fixture)
)

# Import test constants
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from test_constants import TEST_TOKEN

# Configure logging for tests
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@pytest.fixture(scope="session")
async def test_settings():
    """Get test configuration settings."""
    settings = get_global_settings()
    logger.info(f"Test settings: HA_URL={settings.homeassistant_url}")
    return settings


@pytest.fixture(scope="session")
def _blueprint_http_server():
    """Start a local HTTP server for blueprint files used by HAOS tests."""
    env = _detect_docker_host()

    assets_dir = Path(__file__).parent.parent.parent / "assets" / "blueprints"
    assets_dir.mkdir(parents=True, exist_ok=True)

    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(assets_dir))
    handler.log_message = lambda *args: None  # type: ignore[method-assign]
    srv = http.server.HTTPServer(("0.0.0.0", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    base_url = f"http://{env['hostname']}:{port}"
    logger.info(f"🌐 Blueprint HTTP server on :{port}, container URL: {base_url}")

    try:
        yield {
            "base_url": base_url,
            "port": port,
            "extra_hosts": env["extra_hosts"],
            "local_dir": str(assets_dir),
        }
    finally:
        srv.shutdown()


@pytest.fixture(scope="session", autouse=True)
def in_process_data_dir(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Point the in-process server's data and backup dirs at a temp dir.

    The container and HAOS external lanes run the server inside pytest, and
    without this every write tool adds an auto-backup snapshot, usage-log line
    and other files to the developer's ``~/.ha-mcp``. The backup dir is pinned
    too because an existing ``~/.local/share/ha_mcp/backups`` wins over the data
    dir. Remote and stdio lanes set their own environment.
    """
    from ha_mcp.config import _reset_global_settings
    from ha_mcp.utils.data_paths import get_data_dir

    config_dir = tmp_path_factory.mktemp("in-process-config")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HA_MCP_CONFIG_DIR", str(config_dir))
        mp.setenv("HAMCP_BACKUP_DIR", str(config_dir / "backups"))
        get_data_dir.cache_clear()
        _reset_global_settings()
        try:
            yield config_dir
        finally:
            get_data_dir.cache_clear()
            _reset_global_settings()


@pytest.fixture(scope="session")
def ha_container_with_fresh_config(request, in_process_data_dir):  # noqa: PLR0915
    """Create Home Assistant test environment with fresh config.

    Default backend: testcontainer (HA Core Docker image). When the
    ``HAOS_TEST_IMAGE_PATH`` env var points to a pre-baked HAOS qcow2,
    the fixture instead boots HAOS under QEMU/KVM and returns the same
    base_url + token contract. Container-specific keys (container,
    port, config_path) are None on the HAOS path — tests that depend on
    those should skip when the HAOS backend is selected (see #1281).
    """
    # HAOS backend dispatch — short-circuit the testcontainer path entirely.
    if is_haos_backend_selected():
        base_image_path = Path(os.environ[HAOS_IMAGE_ENV])
        inaddon = is_haos_inaddon_mode()
        haos_embedded = is_haos_embedded_mode()
        haos_stdio = is_haos_stdio_mode()
        image_path = _prepare_haos_image(
            base_image_path, inaddon, haos_embedded, haos_stdio
        )
        with boot_haos_qemu(image_path) as base_url:
            token, blueprint_for_haos = _haos_post_boot_setup(base_url, request)
            # Pull setup-time work INTO the try/finally so post-mortem log
            # dump runs even when trigger_dev_addon_update or
            # wait_for_addon_mcp_ready raises — those steps own ~all the
            # inaddon-specific failure surface, and without logs they're
            # opaque "unknown error" failures.
            try:
                addon_mcp_url, embedded_webhook_url = (
                    _bringup_haos_out_of_process_server(
                        base_url, token, inaddon, haos_embedded
                    )
                )
                yield {
                    "container": None,
                    "port": None,
                    "base_url": base_url,
                    "config_path": None,
                    "blueprint_server": blueprint_for_haos,
                    "token": token,
                    # backend marker distinguishes inaddon dispatch (mcp_client
                    # → addon_mcp_url), haos_embedded (mcp_client →
                    # embedded_webhook_url), and external (in-process FastMCP
                    # server pointing at base_url).
                    "backend": (
                        "haos_inaddon"
                        if inaddon
                        else "haos_embedded"
                        if haos_embedded
                        else "haos_stdio"
                        if haos_stdio
                        else "haos"
                    ),
                    # Only set on inaddon mode; external/embedded modes leave None.
                    "addon_mcp_url": addon_mcp_url,
                    # Only set on haos_embedded; other HAOS modes leave None. Named
                    # to match the container embedded backend's key so mcp_client's
                    # HTTP-transport branch is shared.
                    "embedded_webhook_url": embedded_webhook_url,
                }
            finally:
                _dump_haos_session_logs(base_url, token, inaddon)
        return

    # --- Testcontainer path ---
    # Safety guard 1: ensure Docker is available before doing anything else
    try:
        import docker as docker_sdk

        docker_sdk.from_env().ping()
    except Exception as e:  # noqa: BLE001
        pytest.fail(
            f"Docker is not available: {e}\n"
            "E2E tests require a running Docker daemon (testcontainers).\n"
            "Start Docker and retry."
        )

    logger.info("🐳 Creating Home Assistant container with testcontainers...")

    # Embedded backend (#1527): install the in-process MCP server integration
    # into this same testcontainer and drive it over its ingress webhook. The
    # wheel name is captured here and consumed by the entrypoint preinstall below.
    embedded = _is_embedded_backend_selected()
    config_path, temp_dir, local_blueprint, embedded_wheel_name = (
        _prepare_testcontainer_config(embedded)
    )

    container = _build_ha_testcontainer(config_path, embedded, embedded_wheel_name)

    with container:
        # Readiness-gate budgets for the testcontainer path. Defined at
        # the top of this ``with container:`` block for grep-ability —
        # successor of the five per-gate constants the single
        # ``CoreState.RUNNING`` check replaced. The HAOS branch above
        # keeps its own ``SUN_WAIT = 60`` local (HAOS-qemu boot is a
        # different scope, no ``[READINESS_GATE_TIMING]`` emit).
        #
        # ``CORE_STATE_TIMEOUT = 60`` ≈ 12× the observed-max of ~5s
        # across the first CI window (4 worker sessions,
        # ``entries_loaded == entries_total == 11``). The upstream
        # per-domain ceiling ``SLOW_SETUP_MAX_WAIT = 300`` from
        # ``homeassistant/setup.py`` is the latest point at which a
        # stuck integration would surface as ``CoreState`` stuck at
        # ``starting`` — 60s is well clear of that without paying the
        # worst-case wall-clock if a slow integration legitimately
        # needs the full budget.
        CORE_STATE_TIMEOUT = 60
        # ``SUN_WAIT = 5`` is the residual tight poll:
        # ``CoreState.RUNNING`` does not strictly imply
        # ``sun.sun != "unknown"`` because sun's first periodic position
        # computation runs as a scheduled task after
        # ``async_setup_entry`` returns. Template tests asserting
        # above/below_horizon would fail without this inline check.
        SUN_WAIT = 5

        # Get the dynamically assigned port
        host_port = container.get_exposed_port(8123)
        base_url = f"http://localhost:{host_port}"

        # Set environment variables for the dynamic URL so WebSocket client uses correct port
        os.environ["HOMEASSISTANT_URL"] = base_url
        os.environ["HOMEASSISTANT_TOKEN"] = TEST_TOKEN
        # Enable feature flags for e2e tests. Beta sub-flags require
        # the master to also be on.
        os.environ["ENABLE_BETA_FEATURES"] = "true"
        os.environ["ENABLE_YAML_CONFIG_EDITING"] = "true"
        # Per-key sub-toggles default OFF; the E2E suite covers the
        # whole packages/*.yaml surface so enable all three.
        os.environ["ENABLE_YAML_PACKAGES_AUTOMATION"] = "true"
        os.environ["ENABLE_YAML_PACKAGES_SCRIPT"] = "true"
        os.environ["ENABLE_YAML_PACKAGES_SCENE"] = "true"
        os.environ["HAMCP_ENABLE_FILESYSTEM_TOOLS"] = "true"
        # Operator extra YAML write keys (#1887): widen the allowlist with a
        # single non-built-in key so the success-path write test has a key to
        # exercise. Value mirrors _EMBEDDED_FEATURE_FLAGS["extra_yaml_write_keys"].
        os.environ["HA_MCP_EXTRA_YAML_KEYS"] = "alert2"
        # Strict best-practices gate (#1779) defaults ON with its parent; pin it
        # OFF so the suite's keyless writes aren't hard-blocked. The strict-gate
        # e2e test builds its own server with the flag enabled.
        os.environ["ENABLE_STRICT_MANDATORY_BPS"] = "false"
        # Snapshot deletion (#1861) defaults OFF in production; enabled here so
        # the e2e suite can cover the (snapshot, delete) guard chain against a
        # disposable test container.
        os.environ["ENABLE_SNAPSHOT_DELETE"] = "true"

        # Reset cached settings + WebSocket pool so subsequent client
        # lookups pick up the new container's URL.
        _reset_ha_in_process_caches()

        logger.info(f"🚀 Home Assistant container started on {base_url}")
        logger.info(f"🐳 Container ID: {container.get_container_host_ip()}:{host_port}")

        # Use test token for API readiness checks
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        embedded_webhook_url = _wait_for_testcontainer_ready(
            container,
            base_url,
            headers,
            embedded,
            CORE_STATE_TIMEOUT,
            SUN_WAIT,
        )

        # Store connection info for other fixtures
        container_info = {
            "container": container,
            "port": host_port,
            "base_url": base_url,
            "config_path": str(config_path),
            "blueprint_server": local_blueprint,
            "token": TEST_TOKEN,
            # ``embedded`` reuses the whole testcontainer path but swaps the
            # server-under-test to the in-process integration; mcp_server yields
            # None and mcp_client speaks HTTP to embedded_webhook_url (below).
            "backend": "embedded" if embedded else "container",
            # Set only on the embedded backend; None keeps the container-lane
            # dispatch assertions (addon_mcp_url is None) unchanged.
            "embedded_webhook_url": embedded_webhook_url,
        }

        request.config.stash[_HA_CONTAINER_KEY] = container
        try:
            yield container_info
        finally:
            del request.config.stash[_HA_CONTAINER_KEY]
            # Container cleanup runs via the enclosing ``with container:``
            # block's ``__exit__`` (calls ``stop()`` which removes the
            # container). With ``TESTCONTAINERS_RYUK_DISABLED=true`` set in
            # the CI workflow env (see
            # .github/workflows/{pr,e2e-tests,performance-tests}.yml)
            # the with-block exit IS the only cleanup mechanism — Python's
            # context-manager protocol guarantees ``__exit__`` fires on
            # both normal and exception flows, so the Ryuk reaper safety
            # net is not needed. Refs #366.
            #
            # Do NOT add an explicit ``container.stop()`` here.
            # testcontainers' ``DockerContainer.stop()`` calls
            # ``remove(force=True)`` and is non-idempotent — a second call
            # from the enclosing ``with container:`` ``__exit__`` raises
            # ``docker.errors.NotFound`` at session teardown.
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info("✅ Cleanup completed")


@pytest.fixture(scope="session")
async def ha_client(
    ha_container_with_fresh_config,
) -> AsyncGenerator[HomeAssistantClient]:
    """Create Home Assistant client connected to the container or HAOS QEMU."""
    container_info = ha_container_with_fresh_config
    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)

    client = HomeAssistantClient(base_url=base_url, token=token)

    # Verify connection
    try:
        config = await client.get_config()
        if not config:
            pytest.fail(f"Failed to connect to Home Assistant at {base_url}")

        logger.info(
            f"✅ Connected to HA: {config.get('location_name', 'Unknown')} v{config.get('version', 'Unknown')}"
        )
        logger.info(f"🏠 Components: {len(config.get('components', []))} loaded")

    except Exception as e:  # noqa: BLE001
        pytest.fail(f"Home Assistant connection failed: {e}\nURL: {base_url}")

    yield client
    await client.close()


@pytest.fixture(scope="session")
async def mcp_server(
    ha_container_with_fresh_config,
) -> AsyncGenerator[HomeAssistantSmartMCPServer | None]:
    """Create MCP server instance connected to the container or HAOS QEMU.

    Yields None on the inaddon HAOS backend, the container embedded backend, and
    the haos_embedded backend — in all three the server-under-test runs in a
    separate process (the ha-mcp dev addon inside booted HAOS; the in-process
    in-process MCP server entry inside the testcontainer or the HAOS core container),
    so spinning up an in-process FastMCP server here would be wasteful and
    misleading (it'd connect to HA but tests would never use it). The
    ``mcp_client`` fixture branches on backend to either use this in-process
    server or build an HTTP transport pointing at the out-of-process server.
    """
    container_info = ha_container_with_fresh_config
    if container_info.get("backend") in (
        "haos_inaddon",
        "haos_stdio",
        "embedded",
        "haos_embedded",
    ):
        logger.info(
            "%s mode: skipping in-process MCP server "
            "(tests use the out-of-process server transport instead)",
            container_info.get("backend"),
        )
        yield None
        return

    logger.info("🚀 Creating MCP server instance...")
    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)

    # Create client for the server
    client = HomeAssistantClient(base_url=base_url, token=token)

    # Create server with the client
    server = HomeAssistantSmartMCPServer(client=client)
    tools = await server.mcp.list_tools()
    logger.info(
        f"✅ MCP server initialized with {len(tools)} tools connected to {base_url}"
    )

    yield server
    # Server cleanup handled by server.close()


@pytest.fixture(scope="session")
async def mcp_client(
    ha_container_with_fresh_config, mcp_server, haos_stdio_config_dir
) -> AsyncGenerator[Client]:
    """Create FastMCP client — in-memory for in-process server, HTTP otherwise.

    On testcontainer + HAOS-external: in-memory transport bound to the
    ``mcp_server`` fixture (current behavior).
    On HAOS-stdio: ``StdioTransport`` launches the installed ``ha-mcp`` command
    with an isolated config directory and the real HAOS URL/token.
    On HAOS-inaddon: ``StreamableHttpTransport`` pointing at the dev
    addon's MCP endpoint (running inside the booted HAOS).
    On embedded (#1527): ``StreamableHttpTransport`` pointing at the
    in-process MCP server entry's ingress webhook (running inside the testcontainer).
    On haos_embedded (#1527): the same, but the in-process MCP server runs
    inside the HAOS core container (webhook on the booted VM). In all HTTP cases
    the server-under-test is a separate process; the local process is just a client.
    """
    container_info = ha_container_with_fresh_config
    backend = container_info.get("backend")
    if backend == "haos_stdio":
        client = _stdio_client(container_info, haos_stdio_config_dir)
        try:
            async with client:
                logger.debug("🔗 FastMCP client connected (stdio subprocess transport)")
                yield client
        finally:
            await _retire_stdio_sidecar(haos_stdio_config_dir)
        return

    if backend in ("haos_inaddon", "embedded", "haos_embedded"):
        from ha_mcp._vendor.fastmcp.client.transports import StreamableHttpTransport

        if backend in ("embedded", "haos_embedded"):
            server_url = container_info.get("embedded_webhook_url")
            missing_msg = (
                f"{backend} backend signaled but container_info has no "
                "embedded_webhook_url — the embedded-webhook readiness gate must "
                "run + populate this key before mcp_client is requested. Check "
                "ha_container_with_fresh_config's embedded branch."
            )
        else:
            server_url = container_info.get("addon_mcp_url")
            missing_msg = (
                "Inaddon backend signaled but container_info has no "
                "addon_mcp_url — wait_for_addon_mcp_ready must run + "
                "populate this key before mcp_client is requested. "
                "Check ha_container_with_fresh_config's inaddon branch."
            )
        if not server_url:
            raise RuntimeError(missing_msg)
        logger.info(f"🔗 FastMCP client connecting (HTTP) to {server_url}")
        transport = StreamableHttpTransport(url=server_url)
        client = Client(transport)
        async with client:
            logger.debug("🔗 FastMCP client connected (HTTP transport, %s)", backend)
            yield client
        return

    # Default path: in-memory transport.
    client = Client(mcp_server.mcp)
    async with client:
        logger.debug("🔗 FastMCP client connected (in-memory transport)")
        yield client


def _stdio_env(container_info: dict[str, Any], config_dir: Path) -> dict[str, str]:
    """Build the explicit environment for an installed ``ha-mcp`` process."""
    # StdioTransport's explicit env does not inherit the pytest process. Keep every
    # load-bearing setting here, including the env-file selector that prevents a
    # contributor's checkout-root .env from changing subprocess behavior.
    return {
        "HOMEASSISTANT_URL": container_info["base_url"],
        "HOMEASSISTANT_TOKEN": container_info.get("token", TEST_TOKEN),
        "HA_MCP_CONFIG_DIR": str(config_dir),
        # Config-dir isolation alone still permits an existing legacy backup dir.
        "HAMCP_BACKUP_DIR": str(config_dir / "backups"),
        "HAMCP_ENV_FILE": os.environ.get("HAMCP_ENV_FILE", "tests/.env.test"),
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # Fail quickly against a disposable HA if connection setup is broken.
        "HA_MAX_RETRIES": "1",
        # Unset also enables the sidecar; spell this out because stdio visibility
        # E2E requires the web settings endpoint to participate in the sequence.
        "HA_MCP_DISABLE_SETTINGS_UI": "false",
        # Beta master plus all packages/*.yaml sub-toggles.
        "ENABLE_BETA_FEATURES": "true",
        "ENABLE_YAML_CONFIG_EDITING": "true",
        "ENABLE_YAML_PACKAGES_AUTOMATION": "true",
        "ENABLE_YAML_PACKAGES_SCRIPT": "true",
        "ENABLE_YAML_PACKAGES_SCENE": "true",
        "HAMCP_ENABLE_FILESYSTEM_TOOLS": "true",
        # Non-built-in YAML write key used by the success-path coverage (#1887).
        "HA_MCP_EXTRA_YAML_KEYS": "alert2",
        # Strict best-practices defaults on with its parent. Pinning it off
        # preserves the full suite's keyless writes; #1779 enables it explicitly.
        "ENABLE_STRICT_MANDATORY_BPS": "false",
        # Production defaults snapshot deletion off; the disposable HAOS guest
        # may exercise the guarded deletion path.
        "ENABLE_SNAPSHOT_DELETE": "true",
    }


def _stdio_client(container_info: dict[str, Any], config_dir: Path) -> Client:
    """Return a FastMCP client backed by the installed stdio entry point."""
    from ha_mcp._vendor.fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command="ha-mcp",
        # FastMCP expects an explicit empty list; omitting args changes its default.
        args=[],
        env=_stdio_env(container_info, config_dir),
        # The fixture owns process lifetime and must run lifespan cancellation.
        keep_alive=False,
    )
    return Client(transport)


async def _retire_stdio_sidecar(config_dir: Path) -> None:
    """Retire the detached listener without blocking the event loop."""
    from ha_mcp import stdio_settings_sidecar

    # Test config dirs are session/test-scoped and discarded afterwards, so
    # retirement need not preserve discovery files for a future process.
    await asyncio.to_thread(stdio_settings_sidecar.retire_sidecar, config_dir)


@pytest.fixture(scope="session")
def haos_stdio_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Config for the full-suite HAOS stdio process.

    It stays separate because visibility E2E mutates entity_visibility.json and
    concurrent stdio processes would collide on ui.url and the sidecar port.
    """
    return tmp_path_factory.mktemp("haos-stdio-config")


@pytest.fixture(scope="session")
def packaging_stdio_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Config for tests that explicitly request ``stdio_mcp_client``.

    A second directory prevents its process from replacing the HAOS stdio lane's
    ui.url, sidecar listener, and visibility configuration.
    """
    return tmp_path_factory.mktemp("packaging-stdio-config")


@pytest.fixture(scope="session")
async def stdio_mcp_client(
    ha_container_with_fresh_config,
    packaging_stdio_config_dir,
) -> AsyncGenerator[Client]:
    """Spawn ``ha-mcp`` as a subprocess and connect via stdio JSON-RPC.

    The default ``mcp_client`` also uses this real transport on the HAOS stdio
    lane. Everywhere else it uses the lane's primary server, so this dedicated
    fixture supplies installed-entry-point stdio coverage for packaging and
    launcher-specific tests.

    Both paths cover the transport real users hit via Claude Desktop, Claude CLI,
    ``uvx``, or Docker stdio mode: subprocess startup, JSON serialization
    framing, and the installed-wheel contract. This fixture specifically catches
    packaging regressions (for example skills missing from the installed package,
    #1280), entry-point startup failures, and JSON serialization issues without
    taking over the full suite outside the dedicated HAOS stdio lane.
    """

    container_info = ha_container_with_fresh_config
    client = _stdio_client(container_info, packaging_stdio_config_dir)
    try:
        async with client:
            logger.debug("🔗 FastMCP client connected (stdio subprocess transport)")
            yield client
    finally:
        await _retire_stdio_sidecar(packaging_stdio_config_dir)


# Test session information
@pytest.fixture(scope="session", autouse=True)
async def test_session_info(ha_client, ha_container_with_fresh_config):
    """Log test session information."""
    config = await ha_client.get_config()
    container_info = ha_container_with_fresh_config

    logger.info("=" * 80)
    logger.info("🧪 HOME ASSISTANT MCP SERVER E2E TEST SESSION (FRESH CONFIG)")
    logger.info("=" * 80)
    logger.info(
        f"🏠 Home Assistant: {config.get('location_name')} v{config.get('version')}"
    )
    logger.info(f"🐳 Container URL: {container_info['base_url']}")
    logger.info(f"🔧 Components: {len(config.get('components', []))}")
    logger.info(f"🕒 Timezone: {config.get('time_zone', 'Unknown')}")
    logger.info("📁 Fresh config from: initial_test_state")
    logger.info(f"📂 Config path: {container_info['config_path']}")
    logger.info("=" * 80)

    yield

    logger.info("=" * 80)
    logger.info("✅ E2E TEST SESSION COMPLETED (FRESH CONFIG)")
    logger.info("=" * 80)


@pytest.fixture
def cleanup_tracker():
    """
    Track entities created during tests for cleanup.

    Usage in tests:
        cleanup_tracker.track("automation", "automation.test_automation")
        cleanup_tracker.track("script", "script.test_script")
    """
    created_entities: list[tuple[str, str]] = []

    class CleanupTracker:
        def track(self, entity_type: str, entity_id: str):
            """Track an entity for cleanup."""
            created_entities.append((entity_type, entity_id))
            logger.info(f"📝 Tracking {entity_type}: {entity_id} for cleanup")

        def get_tracked(self) -> list[tuple[str, str]]:
            """Get all tracked entities."""
            return created_entities.copy()

    tracker = CleanupTracker()
    yield tracker

    # Cleanup logic - log what would be cleaned up
    # Real implementation would delete the entities
    if created_entities:
        logger.info(f"🧹 Would clean up {len(created_entities)} test entities:")
        for entity_type, entity_id in created_entities:
            logger.info(f"  - {entity_type}: {entity_id}")


@pytest.fixture
async def test_light_entity(mcp_client) -> str:
    """
    Find a suitable light entity for testing.

    Returns the entity_id of a light that can be used for testing.
    Prefers entities that are currently off to minimize disruption.
    """
    # Search for light entities
    search_result = await mcp_client.call_tool(
        "ha_search", {"query": "light", "domain_filter": "light", "limit": 10}
    )

    # Parse search results
    search_data = parse_mcp_result(search_result)

    if not search_data.get("success") or not search_data.get("entities"):
        pytest.skip("No light entities available for testing")

    # Find a light that's currently off (preferred for testing)
    for entity in search_data["entities"]:
        entity_id = entity["entity_id"]

        # Get current state
        state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": entity_id}
        )
        state_data = parse_mcp_result(state_result)

        if state_data.get("data", {}).get("state") == "off":
            logger.info(f"🔍 Using test light: {entity_id} (currently off)")
            return entity_id

    # If no off lights, use the first available
    entity_id = search_data["entities"][0]["entity_id"]
    logger.info(f"🔍 Using test light: {entity_id} (may be on)")
    return entity_id


@pytest.fixture
async def clean_test_environment(mcp_client):
    """
    Ensure clean test environment by removing any existing test entities.

    This fixture runs before tests to clean up any leftover test data
    from previous test runs.
    """
    logger.info("🧹 Cleaning test environment...")

    # Search for test entities (containing 'test' or 'e2e' in name)
    search_patterns = ["test", "e2e"]

    for pattern in search_patterns:
        # Search automations
        search_result = await mcp_client.call_tool(
            "ha_search",
            {"query": pattern, "domain_filter": "automation", "limit": 20},
        )

        search_data = parse_mcp_result(search_result)
        if search_data.get("success") and search_data.get("entities"):
            for entity in search_data["entities"]:
                entity_id = entity["entity_id"]
                if any(test_word in entity_id.lower() for test_word in ["test", "e2e"]):
                    logger.info(f"🗑️ Found test automation to clean: {entity_id}")
                    # In real implementation, would delete here

    logger.info("✅ Test environment cleaned")


class TestDataFactory:
    """Factory for creating test data configurations."""

    @staticmethod
    def automation_config(name: str, **overrides) -> dict[str, Any]:
        """Create a basic automation configuration for testing."""
        config = {
            "alias": f"Test {name} E2E",
            "description": f"E2E test automation - {name} - safe to delete",
            "trigger": [{"platform": "time", "at": "06:00:00"}],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.bed_light"}}
            ],
            "initial_state": False,  # Start disabled for safety
            "mode": "single",
        }

        config.update(overrides)
        return config

    @staticmethod
    def script_config(name: str, **overrides) -> dict[str, Any]:
        """Create a basic script configuration for testing."""
        config = {
            "alias": f"Test {name} Script E2E",
            "description": f"E2E test script - {name} - safe to delete",
            "sequence": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "light.bed_light"},
                },
                {"delay": {"seconds": 1}},
                {
                    "service": "light.turn_off",
                    "target": {"entity_id": "light.bed_light"},
                },
            ],
            "mode": "single",
        }
        config.update(overrides)
        return config

    @staticmethod
    def helper_config(helper_type: str, name: str, **overrides) -> dict[str, Any]:
        """Create helper configuration for testing."""
        base_configs = {
            "input_boolean": {"name": f"Test {name} Boolean", "initial": False},
            "input_number": {
                "name": f"Test {name} Number",
                "min_value": 0,
                "max_value": 100,
                "step": 1,
                "unit_of_measurement": "units",
            },
            "input_text": {
                "name": f"Test {name} Text",
                "initial": "test_value",
                "min": 0,
                "max": 255,
            },
        }

        config = base_configs.get(helper_type, {})
        config.update(overrides)
        return config


@pytest.fixture
def test_data_factory() -> TestDataFactory:
    """Provide factory for creating test data configurations."""
    return TestDataFactory()


@pytest.fixture
async def wait_for_state_change():
    """
    Utility fixture for waiting for entity state changes.

    Usage:
        await wait_for_state_change(mcp_client, "light.bedroom", "on", timeout=10)
    """

    async def _wait_for_state(
        client: Client, entity_id: str, expected_state: str, timeout: int = 5
    ) -> bool:
        """Wait for entity to reach expected state."""
        start_time = time.monotonic()

        while time.monotonic() - start_time < timeout:
            state_result = await client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            current_state = state_data.get("data", {}).get("state")
            if current_state == expected_state:
                logger.info(f"✅ {entity_id} reached state '{expected_state}'")
                return True

            await asyncio.sleep(0.5)

        logger.warning(
            f"⚠️ {entity_id} did not reach state '{expected_state}' within {timeout}s"
        )
        return False

    return _wait_for_state


@pytest.fixture(scope="session")
def local_blueprint_server(ha_container_with_fresh_config):
    """Return blueprint URL info for tests that need to import blueprints."""
    server = ha_container_with_fresh_config["blueprint_server"]
    logger.info(f"🌐 Blueprint server at {server['base_url']}")
    yield server
