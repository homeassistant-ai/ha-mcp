"""A Z-Wave network with no radio, for the testcontainer lanes.

``tests/zwave_mock`` is a Z-Wave JS server whose driver talks to zwave-js's
mock controller. It runs in a Node container on Docker's default bridge, where
the Home Assistant container reaches it by address.
"""

from __future__ import annotations

from pathlib import Path

from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from tests.test_constants import ZWAVE_MOCK_IMAGE

_SOURCE = Path(__file__).resolve().parents[3] / "zwave_mock"
# The source is mounted read-only; the install needs a directory of its own.
_RUN = (
    "cp -r /rig /srv/zwave && cd /srv/zwave "
    "&& npm ci --no-audit --no-fund && exec node server.mjs"
)
# The last line server.mjs prints once the driver is ready.
_READY = "zwave mock listening"
#: The node ``server.mjs`` defines beside the controller.
NODE_ID = 2


def start_zwave_mock() -> tuple[DockerContainer, str]:
    """Start the server; return its container and the URL Home Assistant uses."""
    container = (
        DockerContainer(ZWAVE_MOCK_IMAGE)
        .with_volume_mapping(str(_SOURCE), "/rig", "ro")
        .with_command(["sh", "-c", _RUN])
        # The image pull and the install both happen inside this wait.
        .waiting_for(LogMessageWaitStrategy(_READY).with_startup_timeout(300))
    )
    container.start()
    address = container.get_docker_client().bridge_ip(
        container.get_wrapped_container().id
    )
    return container, f"ws://{address}:3000"
