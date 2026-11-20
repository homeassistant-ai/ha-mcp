"""A Z-Wave mock container that fails to start is stopped, and says why.

testcontainers re-raises a readiness timeout without stopping the container,
and the helper has not yet returned it to the fixture that would. Driving the
real helper needs Docker, so the container and the image pull are local
doubles.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self

import pytest

from tests.src.e2e.utilities import zwave_mock


class _Container:
    """Stands where ``DockerContainer`` does; fails at the chosen step."""

    def __init__(self, image: str, fail_at: str, stop_fails: bool) -> None:
        self.fail_at = fail_at
        self.stop_fails = stop_fails
        self.stops = 0

    def with_volume_mapping(self, *args: Any) -> Self:
        return self

    def with_command(self, command: Any) -> Self:
        return self

    def waiting_for(self, strategy: Any) -> Self:
        return self

    def start(self) -> Self:
        if self.fail_at == "start":
            raise TimeoutError("the server never said it was listening")
        return self

    def get_wrapped_container(self) -> Any:
        return SimpleNamespace(id="abc")

    def get_docker_client(self) -> Any:
        def bridge_ip(container_id: str) -> str:
            raise TimeoutError("the container has no bridge address")

        return SimpleNamespace(bridge_ip=bridge_ip)

    def stop(self) -> None:
        self.stops += 1
        if self.stop_fails:
            raise RuntimeError("the daemon went away")


@pytest.mark.parametrize("stop_fails", [False, True])
@pytest.mark.parametrize("fail_at", ["start", "address"])
def test_a_failed_start_stops_the_container_and_keeps_its_error(
    monkeypatch: pytest.MonkeyPatch, fail_at: str, stop_fails: bool
) -> None:
    made: list[_Container] = []

    def container(image: str) -> _Container:
        made.append(_Container(image, fail_at, stop_fails))
        return made[-1]

    pulls = SimpleNamespace(images=SimpleNamespace(pull=lambda image: None))
    monkeypatch.setattr(zwave_mock.docker, "from_env", lambda: pulls)
    monkeypatch.setattr(zwave_mock, "DockerContainer", container)

    with pytest.raises(TimeoutError):
        zwave_mock.start_zwave_mock()

    assert [c.stops for c in made] == [1]
