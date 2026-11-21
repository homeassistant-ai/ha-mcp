"""A Z-Wave network with no radio, for the testcontainer lanes.

``tests/zwave_mock`` is a Z-Wave JS server whose driver talks to zwave-js's
mock controller. It runs in a Node container on Docker's default bridge, where
the Home Assistant container reaches it by address.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import docker
import docker.errors
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from tests.test_constants import ZWAVE_MOCK_IMAGE

logger = logging.getLogger(__name__)

_SOURCE = Path(__file__).resolve().parents[3] / "zwave_mock"
# The source is mounted read-only; the install needs a directory of its own.
_RUN = (
    "cp -r /rig /srv/zwave && cd /srv/zwave "
    "&& npm ci --no-audit --no-fund && exec node server.mjs"
)
# The last line server.mjs prints once the driver is ready.
_READY = "zwave mock listening"
# The pinned mirror, then Docker Hub's copy of the same tag: a pull from
# either can fail on a shared runner.
_IMAGES = (
    ZWAVE_MOCK_IMAGE,
    ZWAVE_MOCK_IMAGE.removeprefix("public.ecr.aws/docker/library/"),
)
_PULL_ROUNDS = 2
_SECONDS_BETWEEN_ROUNDS = 15
#: The node ``server.mjs`` defines beside the controller.
NODE_ID = 2


def _pulled_image() -> str:
    """Pull the Node image from the first registry that serves it."""
    client = docker.from_env()
    failures: list[str] = []
    for round_number in range(_PULL_ROUNDS):
        if round_number:
            time.sleep(_SECONDS_BETWEEN_ROUNDS)
        for image in _IMAGES:
            try:
                client.images.pull(image)
            except docker.errors.DockerException as error:
                logger.warning("Pulling %s failed: %s", image, error)
                failures.append(f"{image}: {error}")
            else:
                return image
    raise RuntimeError("No registry served the Node image: " + "; ".join(failures))


def start_zwave_mock() -> tuple[DockerContainer, str]:
    """Start the server; return its container and the URL Home Assistant uses."""
    container = (
        DockerContainer(_pulled_image())
        .with_volume_mapping(str(_SOURCE), "/rig", "ro")
        .with_command(["sh", "-c", _RUN])
        # The install happens inside this wait.
        .waiting_for(LogMessageWaitStrategy(_READY).with_startup_timeout(300))
    )
    try:
        container.start()
        address = container.get_docker_client().bridge_ip(
            container.get_wrapped_container().id
        )
    except BaseException:
        # A start that timed out leaves its container running. A stop that
        # fails as well must not replace the error that says why.
        try:
            container.stop()
        except Exception:
            logger.exception("Stopping the Z-Wave mock container failed")
        raise
    return container, f"ws://{address}:3000"
