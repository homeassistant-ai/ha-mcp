#!/usr/bin/env python3
"""
Home Assistant MCP Test Environment Manager

Interactive test environment for Home Assistant MCP Server development.
Uses testcontainers to manage a Home Assistant instance for testing.

Environment Variables:
    HA_TEST_PORT: Optional fixed port for Home Assistant container (default: random)
                  Example: HA_TEST_PORT=8123
"""

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from testcontainers.core.container import DockerContainer

# Add tests directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))
from test_constants import TEST_PASSWORD, TEST_TOKEN, TEST_USER

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class HomeAssistantTestEnvironment:
    """Manages a containerized Home Assistant test environment."""

    def __init__(self):
        self.container: DockerContainer | None = None
        self.ha_url: str | None = None
        self.ha_token = TEST_TOKEN
        self.test_user = TEST_USER
        self.test_password = TEST_PASSWORD

    def _setup_config_directory(self) -> Path:
        """Set up Home Assistant configuration directory."""
        import tempfile
        import shutil

        # Create temporary directory for HA config
        config_dir = Path(tempfile.mkdtemp(prefix="ha_test_env_"))

        # Find initial_test_state directory
        test_root = Path(__file__).parent
        initial_state_paths = [
            test_root / "initial_test_state",
            test_root / "setup" / "homeassistant" / "initial_test_state",
            test_root.parent / "initial_test_state",
        ]

        initial_state_dir = None
        for path in initial_state_paths:
            if path.exists():
                initial_state_dir = path
                break

        if not initial_state_dir:
            raise FileNotFoundError(
                "Could not find initial_test_state directory. Checked:\n"
                + "\n".join(f"  - {p}" for p in initial_state_paths)
            )

        # Copy initial test state to config directory
        logger.info(f"📁 Setting up config from: {initial_state_dir}")
        for item in initial_state_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, config_dir)
            elif item.is_dir():
                shutil.copytree(item, config_dir / item.name)

        # Set proper permissions
        os.chmod(config_dir, 0o755)
        for item in config_dir.rglob("*"):
            if item.is_file():
                os.chmod(item, 0o644)
            elif item.is_dir():
                os.chmod(item, 0o755)

        logger.info(f"📂 Config directory prepared: {config_dir}")
        return config_dir

    def start_container(self) -> None:
        """Start the Home Assistant container."""
        if self.container:
            logger.warning("Container is already running")
            return

        logger.info("🐳 Starting Home Assistant test container...")

        # Set up config directory
        config_dir = self._setup_config_directory()

        # Create container with port configuration
        # renovate: datasource=docker depName=ghcr.io/home-assistant/home-assistant
        container = DockerContainer("ghcr.io/home-assistant/home-assistant:2025.12.5")

        # Check for custom port via environment variable
        custom_port = os.environ.get("HA_TEST_PORT")
        if custom_port:
            try:
                port = int(custom_port)
                container = container.with_bind_ports(8123, port)
                logger.info(f"🔌 Using fixed port {port} (from HA_TEST_PORT)")
            except ValueError:
                logger.warning(f"⚠️ Invalid HA_TEST_PORT '{custom_port}', using random port")
                container = container.with_bind_ports(8123, None)
        else:
            container = container.with_bind_ports(8123, None)  # Random host port

        self.container = (
            container
            .with_volume_mapping(str(config_dir), "/config", "rw")
            .with_env("TZ", "UTC")
            .with_kwargs(privileged=True)
        )

        self.container.start()

        # Get connection details
        host_port = self.container.get_exposed_port(8123)
        self.ha_url = f"http://localhost:{host_port}"

        logger.info(
            f"🚀 Container started: {self.container.get_container_host_ip()}:{host_port}"
        )
        logger.info(f"🌐 Home Assistant URL: {self.ha_url}")

        # Wait for Home Assistant to be ready
        self._wait_for_home_assistant()

    def _wait_for_home_assistant(self, timeout: int = 120) -> None:
        """Wait for Home Assistant to be ready."""
        logger.info("⏳ Waiting for Home Assistant to become ready...")

        start_time = time.time()
        attempts = 0

        while time.time() - start_time < timeout:
            attempts += 1
            try:
                # Check frontend (no auth required) to see if HA is up
                response = requests.get(f"{self.ha_url}/", timeout=5)
                logger.debug(f"Attempt {attempts}: HTTP {response.status_code}")
                if response.status_code == 200:
                    logger.info("✅ Home Assistant frontend is ready!")
                    # Now verify API with token
                    try:
                        headers = {"Authorization": f"Bearer {self.ha_token}"}
                        api_response = requests.get(
                            f"{self.ha_url}/api/config", headers=headers, timeout=5
                        )
                        if api_response.status_code == 200:
                            config = api_response.json()
                            logger.info(
                                f"✅ API authenticated! Version: {config.get('version', 'unknown')}"
                            )
                            logger.info(
                                f"🏠 Components loaded: {len(config.get('components', []))}"
                            )
                        else:
                            logger.warning(
                                f"⚠️ API token may be invalid (HTTP {api_response.status_code}). "
                                "Tests may fail. See tests/README.md for token update instructions."
                            )
                    except requests.RequestException as e:
                        logger.warning(f"⚠️ Could not verify API token: {e}")
                    return
                else:
                    logger.debug(f"Non-200 response: {response.status_code}")
            except requests.RequestException as e:
                logger.debug(f"Request failed: {type(e).__name__}: {e}")

            if attempts % 6 == 0:  # Every 30 seconds
                logger.info(f"⏳ Still waiting... ({attempts * 5}s elapsed)")

            time.sleep(5)

        raise TimeoutError(f"Home Assistant not ready after {timeout}s")

    def stop_container(self) -> None:
        """Stop and clean up the container."""
        if not self.container:
            logger.warning("No container to stop")
            return

        logger.info("🛑 Stopping Home Assistant container...")
        self.container.stop()
        self.container = None
        self.ha_url = None
        logger.info("✅ Container stopped and cleaned up")

    def run_tests(self) -> None:
        """Run all E2E tests against the running container."""
        if not self.ha_url:
            logger.error("❌ No container running. Start container first.")
            return

        logger.info("🧪 Running E2E tests...")

        # Set environment variables for tests
        env = os.environ.copy()
        env["HOMEASSISTANT_URL"] = self.ha_url
        env["HOMEASSISTANT_TOKEN"] = self.ha_token

        # Run pytest
        cmd = [sys.executable, "-m", "pytest", "tests/src/e2e/", "-v", "--tb=short"]

        try:
            result = subprocess.run(cmd, env=env, cwd=Path(__file__).parent.parent)
            if result.returncode == 0:
                logger.info("✅ All tests passed!")
            else:
                logger.warning(f"⚠️ Tests completed with exit code: {result.returncode}")
        except KeyboardInterrupt:
            logger.info("🛑 Test run interrupted by user")
        except Exception as e:
            logger.error(f"❌ Error running tests: {e}")

    def print_status(self) -> None:
        """Print current environment status."""
        print("\n" + "=" * 80)
        print("🏠 HOME ASSISTANT MCP TEST ENVIRONMENT")
        print("=" * 80)

        if self.container and self.ha_url:
            print(f"\n🌐 Web UI: {self.ha_url}")
            print(f"   Username: {self.test_user}")
            print(f"   Password: {self.test_password}")
            print("\n📋 Copy-paste for testing:")
            print(f"   export HOMEASSISTANT_URL={self.ha_url}")
            print(f"   export HOMEASSISTANT_TOKEN={self.ha_token}")
            print("\n🔑 Full API Token:")
            print(f"   {self.ha_token}")
            print("\n🐳 Container Status: Running")
            print("📊 API Health: ", end="")

            try:
                headers = {"Authorization": f"Bearer {self.ha_token}"}
                response = requests.get(
                    f"{self.ha_url}/api/config", headers=headers, timeout=5
                )
                if response.status_code == 200:
                    print("✅ Ready")
                else:
                    print(f"⚠️ Status {response.status_code}")
            except:
                print("❌ Not accessible")
        else:
            print("🐳 Container Status: Not running")

        print("=" * 70)


def show_menu() -> str:
    """Show interactive menu and return user choice."""
    print("\n📋 MENU:")
    print("1) Run all E2E tests")
    print("2) Stop container and exit")
    print("3) Show environment status")

    while True:
        choice = input("\nChoose option (1-3): ").strip()
        if choice in ["1", "2", "3"]:
            return choice
        print("❌ Invalid choice. Please enter 1, 2, or 3.")


def main():
    """Main entry point for the test environment manager."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Home Assistant MCP Test Environment Manager"
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Run in non-interactive mode (wait for SIGINT instead of showing menu)",
    )
    args = parser.parse_args()

    print("🚀 Home Assistant MCP Test Environment Manager")
    print("=" * 50)

    env = HomeAssistantTestEnvironment()

    try:
        # Start the container
        env.start_container()
        env.print_status()

        if args.no_interactive:
            # Non-interactive mode: just wait for interrupt
            logger.info("🔄 Running in non-interactive mode. Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("\n🛑 Received interrupt signal")
        else:
            # Interactive menu loop
            while True:
                choice = show_menu()

                if choice == "1":
                    env.run_tests()
                elif choice == "2":
                    env.stop_container()
                    print("👋 Goodbye!")
                    break
                elif choice == "3":
                    env.print_status()

    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user")
    except Exception as e:
        logger.error(f"❌ Error: {e}")
    finally:
        # Cleanup
        if env.container:
            env.stop_container()


if __name__ == "__main__":
    main()
