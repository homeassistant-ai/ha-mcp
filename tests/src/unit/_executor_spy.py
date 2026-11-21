"""Count the executor jobs a component command runs on a real ``hass``.

The spy wraps the ``hass`` fixture's own ``async_add_executor_job``, so every
job still runs; a test reads which module's function went to the executor.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant


@contextmanager
def executor_jobs(hass: HomeAssistant) -> Iterator[MagicMock]:
    """Spy on every ``hass.async_add_executor_job`` call while the block runs."""
    real = hass.async_add_executor_job
    with patch.object(hass, "async_add_executor_job", wraps=real) as spy:
        yield spy


def jobs_from(spy: MagicMock, module: str) -> int:
    """How many executor jobs ran a function of the component module ``module``."""
    return sum(
        call.args[0].__module__
        == f"custom_components.ha_mcp_tools.websocket_api.{module}"
        for call in spy.call_args_list
    )
