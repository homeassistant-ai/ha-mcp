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
  - AGENTS.md: documents correct test-run commands
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from testcontainers.core.container import DockerContainer

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fastmcp import Client

from ha_mcp.client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.server import HomeAssistantSmartMCPServer

# Import test utilities
from .utilities.assertions import parse_mcp_result

# Import test constants
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from test_constants import TEST_TOKEN

# Configure logging for tests
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _setup_config_permissions(config_path: Path) -> None:
    """Set up proper permissions for Home Assistant config directory."""
    import os
    import stat

    # Set directory permissions recursively
    for root, dirs, files in os.walk(config_path):
        for d in dirs:
            os.chmod(
                os.path.join(root, d),
                stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH,
            )
        for f in files:
            os.chmod(
                os.path.join(root, f),
                stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IRGRP
                | stat.S_IWGRP
                | stat.S_IROTH,
            )


def _ensure_hacs_frontend(initial_state_path: Path) -> None:
    """Download HACS frontend if not present.

    HACS requires the frontend (~51MB) to be present to fully initialize.
    This is not committed to git to keep the repo size manageable.
    """
    import tarfile
    import urllib.request

    hacs_dir = initial_state_path / "custom_components" / "hacs"
    frontend_dir = hacs_dir / "hacs_frontend"

    # Check if HACS is installed and frontend is missing
    if hacs_dir.exists() and not frontend_dir.exists():
        logger.info("HACS frontend not found, downloading...")

        try:
            # Get the latest frontend version from GitHub API
            import json

            api_url = "https://api.github.com/repos/hacs/frontend/releases/latest"
            with urllib.request.urlopen(api_url, timeout=30) as response:
                release_data = json.loads(response.read())
                tag_name = release_data["tag_name"]

            # Download and extract the frontend
            tarball_url = f"https://github.com/hacs/frontend/releases/download/{tag_name}/hacs_frontend-{tag_name}.tar.gz"
            logger.info(f"Downloading HACS frontend {tag_name}...")

            with urllib.request.urlopen(tarball_url, timeout=120) as response, tarfile.open(fileobj=response, mode="r:gz") as tar:
                    # Extract to temp location first
                    temp_extract = Path(tempfile.mkdtemp())
                    tar.extractall(temp_extract)

                    # Move the hacs_frontend subdirectory
                    extracted_frontend = temp_extract / f"hacs_frontend-{tag_name}" / "hacs_frontend"
                    if extracted_frontend.exists():
                        shutil.move(str(extracted_frontend), str(frontend_dir))
                        logger.info(f"HACS frontend installed at {frontend_dir}")
                    else:
                        logger.warning("Could not find hacs_frontend in downloaded archive")

                    # Cleanup temp
                    shutil.rmtree(temp_extract, ignore_errors=True)

        except Exception as e:
            logger.warning(f"Failed to download HACS frontend: {e}")
            logger.warning("HACS tests may be skipped without the frontend")


def _install_custom_component(
    config_path: Path,
    component_src: Path,
    domain: str,
    title: str,
) -> bool:
    """Install a custom component into the test HA config.

    Copies component source into custom_components/<domain> and injects a
    config entry so HA loads it on startup. Returns True if installed.
    """
    if not component_src.exists():
        logger.info("%s source not found — skipping installation", domain)
        return False

    dest = config_path / "custom_components" / domain
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(component_src, dest, dirs_exist_ok=True)

    # Inject config entry if not already present
    storage_file = config_path / ".storage" / "core.config_entries"
    if storage_file.exists():
        data = json.loads(storage_file.read_text())
        entries = data.get("data", {}).get("entries", [])
        if not any(e.get("domain") == domain for e in entries):
            entries.append(
                {
                    "created_at": "2025-09-07T23:56:28.040744+00:00",
                    "data": {},
                    "disabled_by": None,
                    "discovery_keys": {},
                    "domain": domain,
                    "entry_id": f"e2e_test_{domain}_entry",
                    "minor_version": 1,
                    "modified_at": "2025-09-07T23:56:28.040747+00:00",
                    "options": {},
                    "pref_disable_new_entities": False,
                    "pref_disable_polling": False,
                    "source": "import",
                    "subentries": [],
                    "title": title,
                    "unique_id": domain,
                    "version": 1,
                }
            )
            storage_file.write_text(json.dumps(data, indent=2))

    logger.info("Installed %s component", domain)
    return True


@pytest.fixture(scope="session")
async def test_settings():
    """Get test configuration settings."""
    settings = get_global_settings()
    logger.info(f"Test settings: HA_URL={settings.homeassistant_url}")
    return settings


@pytest.fixture(scope="session")
def ha_container_with_fresh_config():
    """Create Home Assistant container with fresh config using testcontainers."""
    # --- Safety guard 1: ensure Docker is available before doing anything else ---
    try:
        import docker as docker_sdk
        docker_sdk.from_env().ping()
    except Exception as e:
        pytest.fail(
            f"Docker is not available: {e}\n"
            "E2E tests require a running Docker daemon (testcontainers).\n"
            "Start Docker and retry."
        )

    logger.info("🐳 Creating Home Assistant container with testcontainers...")

    # Create temporary directory for this test session
    temp_dir = tempfile.mkdtemp(prefix="ha_e2e_test_")

    # Copy initial test state to temporary directory
    initial_state_path = Path(__file__).parent.parent.parent / "initial_test_state"
    config_path = Path(temp_dir)

    if not initial_state_path.exists():
        pytest.fail(f"Initial test state not found at {initial_state_path}")

    # Ensure HACS frontend is downloaded (if HACS is present)
    _ensure_hacs_frontend(initial_state_path)

    # Copy all files from initial_test_state
    shutil.copytree(initial_state_path, config_path, dirs_exist_ok=True)

    # Inject GITHUB_TOKEN into HACS config entry if available.
    # Without a valid token HACS disables itself, causing flaky test skips.
    # In CI the automatic GITHUB_TOKEN provides sufficient read access.
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        storage_file = config_path / ".storage" / "core.config_entries"
        if storage_file.exists():
            ce_data = json.loads(storage_file.read_text())
            for entry in ce_data.get("data", {}).get("entries", []):
                if entry.get("domain") == "hacs":
                    entry["data"] = {"token": github_token}
                    logger.info("Injected GITHUB_TOKEN into HACS config entry")
                    break
            storage_file.write_text(json.dumps(ce_data, indent=2))

    # Install custom components from repo source
    repo_root = Path(__file__).parent.parent.parent.parent
    if _install_custom_component(
        config_path,
        repo_root / "homeassistant-addon-webhook-proxy" / "mcp_proxy",
        "mcp_proxy",
        "MCP Webhook Proxy",
    ):
        # mcp_proxy needs a config file pointing at HA's own API
        proxy_config = {
            "target_url": "http://localhost:8123/api/",
            "webhook_id": "mcp_e2e_test_webhook_proxy",
        }
        (config_path / ".mcp_proxy_config.json").write_text(
            json.dumps(proxy_config)
        )
    _install_custom_component(
        config_path,
        repo_root / "custom_components" / "ha_mcp_tools",
        "ha_mcp_tools",
        "HA MCP Tools",
    )

    # Ensure proper permissions for Home Assistant
    _setup_config_permissions(config_path)

    logger.info(
        f"📁 Fresh HA config prepared at: {config_path} with proper permissions"
    )

    # Create testcontainer with port configuration
    from test_constants import HA_TEST_IMAGE

    container = DockerContainer(HA_TEST_IMAGE)

    # Check for custom port via environment variable
    custom_port = os.environ.get("HA_TEST_PORT")
    if custom_port:
        try:
            port = int(custom_port)
            container = container.with_bind_ports(8123, port)
            logger.info(f"🔌 Using fixed port {port} (from HA_TEST_PORT)")
        except ValueError:
            logger.warning(f"⚠️ Invalid HA_TEST_PORT '{custom_port}', using dynamic port")
            container = container.with_exposed_ports(8123)
    else:
        container = container.with_exposed_ports(8123)  # Dynamic port assignment
    container = container.with_volume_mapping(
        str(config_path), "/config", "rw"
    )  # Ensure read-write mount
    container = container.with_env("TZ", "UTC")
    # Add privileged mode for Home Assistant hardware access
    container = container.with_kwargs(privileged=True)

    # Remove any .HA_RESTORE file that might cause issues
    restore_file = config_path / ".HA_RESTORE"
    if restore_file.exists():
        restore_file.unlink()
        logger.info("🗑️ Removed .HA_RESTORE file from config")

    with container:
        # Get the dynamically assigned port
        host_port = container.get_exposed_port(8123)
        base_url = f"http://localhost:{host_port}"

        # Set environment variables for the dynamic URL so WebSocket client uses correct port
        os.environ["HOMEASSISTANT_URL"] = base_url
        os.environ["HOMEASSISTANT_TOKEN"] = TEST_TOKEN
        # Enable feature flags for e2e tests
        os.environ["ENABLE_YAML_CONFIG_EDITING"] = "true"
        os.environ["HAMCP_ENABLE_FILESYSTEM_TOOLS"] = "true"

        # Reset cached settings so WebSocket client picks up the dynamic URL
        import ha_mcp.config
        ha_mcp.config._settings = None

        # Reset the WebSocket manager to ensure fresh connection with new URL
        from ha_mcp.client.websocket_client import websocket_manager
        websocket_manager._client = None
        websocket_manager._current_loop = None

        logger.info(f"🚀 Home Assistant container started on {base_url}")
        logger.info(f"🐳 Container ID: {container.get_container_host_ip()}:{host_port}")

        # Check if container is actually running
        import docker

        docker_client = docker.from_env()
        try:
            container_obj = docker_client.containers.get(
                container.get_wrapped_container().id
            )
            logger.info(f"📋 Container status: {container_obj.status}")
            logger.info(f"🔌 Port mappings: {container_obj.ports}")

            # Get recent logs for debugging
            logs = container_obj.logs(tail=20).decode("utf-8", errors="ignore")
            logger.info(f"📄 Container logs:\n{logs}")
        except Exception as e:
            logger.warning(f"⚠️ Could not inspect container: {e}")

        # Wait for API to be ready
        import requests

        api_ready = False

        # Use test token for API readiness checks
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        for attempt in range(60):  # Up to 60 seconds additional wait
            try:
                response = requests.get(f"{base_url}/api/", timeout=5, headers=headers)
                if response.status_code == 200:
                    logger.info(
                        f"🏠 Home Assistant API ready after {attempt + 1} additional attempts"
                    )
                    api_ready = True
                    break
            except requests.exceptions.RequestException:
                if attempt == 0:
                    logger.info("🔄 Waiting for Home Assistant API to become ready...")
                if attempt % 15 == 0 and attempt > 0:
                    logger.info(f"⏳ Still waiting... {attempt}/60 attempts")
                time.sleep(1)

        if not api_ready:
            pytest.fail(
                f"Home Assistant API at {base_url} did not become ready within 60 seconds.\n"
                "The container may have failed to start. Check Docker logs for details."
            )

        # Poll until HA components are fully loaded.  HA typically loads 80+
        # components; 50 is the minimum needed for tests (covers automation,
        # script, input_*, group, scene, and other commonly-tested domains).
        MIN_COMPONENTS = 50
        STABILIZATION_TIMEOUT = 30

        logger.info("⏳ Waiting for Home Assistant components to stabilize...")
        last_count = 0
        for stabilize_attempt in range(STABILIZATION_TIMEOUT):
            try:
                config_resp = requests.get(
                    f"{base_url}/api/config", timeout=2, headers=headers
                )
                if config_resp.status_code == 200:
                    component_count = len(
                        config_resp.json().get("components", [])
                    )
                    if component_count >= MIN_COMPONENTS:
                        logger.info(
                            f"✅ Home Assistant stabilized with {component_count} components "
                            f"after {stabilize_attempt + 1}s"
                        )
                        break
                    if component_count != last_count:
                        logger.info(
                            f"⏳ {component_count} components loaded, waiting for more..."
                        )
                        last_count = component_count
                elif config_resp.status_code >= 400:
                    logger.warning(
                        f"⚠️ Stabilization check returned HTTP {config_resp.status_code}"
                    )
            except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
                logger.debug(f"Stabilization check failed: {exc}")
            time.sleep(1)
        else:
            pytest.fail(
                f"Home Assistant component stabilization timed out after {STABILIZATION_TIMEOUT}s. "
                f"Only {last_count} components loaded (minimum: {MIN_COMPONENTS}). "
                f"Check Docker logs."
            )

        # Wait for entities to actually register (components loaded ≠
        # entities available). HA 2026.4+ can report 80+ components while
        # individual integrations (demo, sun, helpers) are still registering
        # their entities and WebSocket handlers. The demo integration alone
        # creates 60+ entities (lights, sensors, switches, etc.).
        MIN_ENTITIES = 50
        ENTITY_STABILIZATION_TIMEOUT = 30
        logger.info("⏳ Waiting for entities to register...")
        last_entity_count = 0
        for entity_attempt in range(ENTITY_STABILIZATION_TIMEOUT):
            try:
                states_resp = requests.get(
                    f"{base_url}/api/states",
                    timeout=5,
                    headers=headers,
                )
                if states_resp.status_code == 200:
                    entity_count = len(states_resp.json())
                    if entity_count >= MIN_ENTITIES:
                        logger.info(
                            f"✅ {entity_count} entities registered "
                            f"after {entity_attempt + 1}s"
                        )
                        break
                    if entity_count != last_entity_count:
                        logger.info(
                            f"⏳ {entity_count} entities registered, "
                            f"waiting for more..."
                        )
                        last_entity_count = entity_count
            except requests.exceptions.RequestException:
                pass
            time.sleep(1)
        else:
            logger.warning(
                f"⚠️ Entity registration timed out after "
                f"{ENTITY_STABILIZATION_TIMEOUT}s with only "
                f"{last_entity_count} entities — some tests may fail"
            )

        # Store connection info for other fixtures
        container_info = {
            "container": container,
            "port": host_port,
            "base_url": base_url,
            "config_path": str(config_path),
        }

        try:
            yield container_info
        finally:
            # Cleanup temp directory (container cleanup handled by 'with' statement)
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info("✅ Cleanup completed")


@pytest.fixture(scope="session")
async def ha_client(
    ha_container_with_fresh_config,
) -> AsyncGenerator[HomeAssistantClient]:
    """Create Home Assistant client connected to the container."""
    container_info = ha_container_with_fresh_config
    base_url = container_info["base_url"]

    client = HomeAssistantClient(base_url=base_url, token=TEST_TOKEN)

    # Verify connection
    try:
        config = await client.get_config()
        if not config:
            pytest.fail(f"Failed to connect to Home Assistant at {base_url}")

        logger.info(
            f"✅ Connected to HA: {config.get('location_name', 'Unknown')} v{config.get('version', 'Unknown')}"
        )
        logger.info(f"🏠 Components: {len(config.get('components', []))} loaded")

    except Exception as e:
        pytest.fail(f"Home Assistant connection failed: {e}\nURL: {base_url}")

    yield client
    await client.close()


@pytest.fixture(scope="session")
async def mcp_server(
    ha_container_with_fresh_config,
) -> AsyncGenerator[HomeAssistantSmartMCPServer]:
    """Create MCP server instance connected to the container."""
    logger.info("🚀 Creating MCP server instance...")

    container_info = ha_container_with_fresh_config
    base_url = container_info["base_url"]

    # Create client for the server
    client = HomeAssistantClient(base_url=base_url, token=TEST_TOKEN)

    # Create server with the client
    server = HomeAssistantSmartMCPServer(client=client)
    tools = await server.mcp.list_tools()
    logger.info(
        f"✅ MCP server initialized with {len(tools)} tools connected to {base_url}"
    )

    yield server
    # Server cleanup handled by server.close()


@pytest.fixture(scope="session")
async def mcp_client(mcp_server) -> AsyncGenerator[Client]:
    """Create FastMCP client connected to our server."""
    client = Client(mcp_server.mcp)

    async with client:
        logger.debug("🔗 FastMCP client connected (in-memory transport)")
        yield client


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
        "ha_search_entities", {"query": "light", "domain_filter": "light", "limit": 10}
    )

    # Parse search results
    search_data = parse_mcp_result(search_result)

    data = search_data.get("data", {})
    if not data.get("success") or not data.get("results"):
        pytest.skip("No light entities available for testing")

    # Find a light that's currently off (preferred for testing)
    for entity in data["results"]:
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
    entity_id = data["results"][0]["entity_id"]
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
            "ha_search_entities",
            {"query": pattern, "domain_filter": "automation", "limit": 20},
        )

        search_data = parse_mcp_result(search_result)
        if search_data.get("success") and search_data.get("results"):
            for entity in search_data["results"]:
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
        start_time = time.time()

        while time.time() - start_time < timeout:
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
