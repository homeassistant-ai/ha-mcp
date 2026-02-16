"""
Story test fixtures.

Provides:
- HA container with demo state (session-scoped)
- FastMCP in-memory client for setup/teardown
- Story catalog discovery and loading
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import yaml
from testcontainers.core.container import DockerContainer

# Add src to path for imports
TESTS_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))

from fastmcp import Client  # noqa: E402
from test_constants import TEST_TOKEN  # noqa: E402

from ha_mcp.client import HomeAssistantClient  # noqa: E402
from ha_mcp.server import HomeAssistantSmartMCPServer  # noqa: E402

logger = logging.getLogger(__name__)

# renovate: datasource=docker depName=ghcr.io/home-assistant/home-assistant
HA_IMAGE = "ghcr.io/home-assistant/home-assistant:2026.1.3"

CATALOG_DIR = Path(__file__).parent / "catalog"


# ---------------------------------------------------------------------------
# Story loading
# ---------------------------------------------------------------------------
def discover_stories() -> list[dict]:
    """Discover all story YAML files in the catalog directory."""
    stories = []
    for yaml_file in sorted(CATALOG_DIR.glob("s*.yaml")):
        with open(yaml_file) as f:
            story = yaml.safe_load(f)
        story["_file"] = str(yaml_file)
        stories.append(story)
    return stories


def story_ids() -> list[str]:
    """Return story IDs for pytest parametrize."""
    return [s["id"] for s in discover_stories()]


# ---------------------------------------------------------------------------
# HA Container (session-scoped)
# ---------------------------------------------------------------------------
def _setup_config_directory() -> Path:
    """Copy initial_test_state to a temp dir for the HA container."""
    config_dir = Path(tempfile.mkdtemp(prefix="ha_story_"))
    initial_state = TESTS_DIR / "initial_test_state"
    if not initial_state.exists():
        raise FileNotFoundError(f"initial_test_state not found at {initial_state}")

    shutil.copytree(initial_state, config_dir, dirs_exist_ok=True)

    # Set permissions
    os.chmod(config_dir, 0o755)
    for item in config_dir.rglob("*"):
        if item.is_file():
            os.chmod(item, 0o644)
        elif item.is_dir():
            os.chmod(item, 0o755)

    return config_dir


def _wait_for_ha(url: str, timeout: int = 120) -> None:
    """Poll HA until the API is ready."""
    import requests

    logger.info(f"Waiting for HA at {url} ...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(
                f"{url}/api/config",
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                timeout=5,
            )
            if r.status_code == 200:
                version = r.json().get("version", "unknown")
                logger.info(f"HA ready (version {version})")
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    raise TimeoutError(f"HA not ready after {timeout}s")


@pytest.fixture(scope="session")
def ha_container():
    """Session-scoped HA container for all story tests."""
    config_dir = _setup_config_directory()

    container = (
        DockerContainer(HA_IMAGE)
        .with_exposed_ports(8123)
        .with_volume_mapping(str(config_dir), "/config", "rw")
        .with_env("TZ", "UTC")
        .with_kwargs(privileged=True)
    )

    with container:
        port = container.get_exposed_port(8123)
        url = f"http://localhost:{port}"
        logger.info(f"HA container started on {url}")

        # Set env for server
        os.environ["HOMEASSISTANT_URL"] = url
        os.environ["HOMEASSISTANT_TOKEN"] = TEST_TOKEN

        time.sleep(5)
        _wait_for_ha(url)
        time.sleep(10)  # component stabilization

        yield {"url": url, "token": TEST_TOKEN, "port": port}

    # Cleanup
    shutil.rmtree(config_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# FastMCP client for setup/teardown
# ---------------------------------------------------------------------------
@pytest.fixture
async def mcp_client(ha_container) -> AsyncGenerator[Client]:
    """FastMCP in-memory client for programmatic setup/teardown."""
    import ha_mcp.config

    ha_mcp.config._settings = None

    client = HomeAssistantClient(
        base_url=ha_container["url"], token=ha_container["token"]
    )
    server = HomeAssistantSmartMCPServer(client=client)
    fastmcp_client = Client(server.mcp)

    async with fastmcp_client:
        yield fastmcp_client


# ---------------------------------------------------------------------------
# Story execution helpers
# ---------------------------------------------------------------------------
async def run_setup_steps(mcp_client: Client, steps: list[dict]) -> None:
    """Execute setup steps via FastMCP in-memory calls."""
    for step in steps:
        tool_name = step["tool"]
        args = step.get("args", {})
        logger.info(f"  [setup] {tool_name}({args})")
        try:
            await mcp_client.call_tool(tool_name, args)
        except Exception as e:
            logger.error(f"  [setup] {tool_name} failed: {e}")
            raise


async def run_teardown_steps(mcp_client: Client, steps: list[dict]) -> None:
    """Execute teardown steps via FastMCP in-memory calls."""
    for step in steps:
        tool_name = step["tool"]
        args = step.get("args", {})
        logger.info(f"  [teardown] {tool_name}({args})")
        try:
            await mcp_client.call_tool(tool_name, args)
        except Exception as e:
            logger.warning(f"  [teardown] {tool_name} failed (ok): {e}")
