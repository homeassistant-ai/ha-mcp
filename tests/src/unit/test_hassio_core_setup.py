"""``set_up_hassio`` fails on what Core only logs while it sets hassio up."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ._hassio_core import _SETUP_LOGGERS, SupervisorApi, set_up_hassio
from ._tools_addons_core import Supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app


class _WithoutAnAnswer:
    """``supervisor``, never given its answer to ``path``."""

    def __init__(self, supervisor: SupervisorApi, path: str) -> None:
        self._supervisor = supervisor
        self._path = path
        self.url = supervisor.url

    def default(self, method: str, path: str, data: Any = None) -> None:
        if path != self._path:
            self._supervisor.default(method, path, data)

    def default_refusal(
        self, method: str, path: str, message: str, status: int = 400
    ) -> None:
        self._supervisor.default_refusal(method, path, message, status)

    def forget_requests(self) -> None:
        self._supervisor.forget_requests()


async def test_a_read_core_logs_as_failed_and_loads_without_fails_the_setup(
    hass: HomeAssistant, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core logs the jobs it could not read and loads the entry all the same.
    The watch is gone again afterwards, also when the setup failed."""
    silent = _WithoutAnAnswer(supervisor, "/jobs/info")
    watched = [logging.getLogger(name) for name in _SETUP_LOGGERS]
    before = [list(logger.handlers) for logger in watched]

    with pytest.raises(AssertionError, match="SupervisorJobsCoordinator"):
        await set_up_hassio(hass, silent, monkeypatch)

    assert [list(logger.handlers) for logger in watched] == before
