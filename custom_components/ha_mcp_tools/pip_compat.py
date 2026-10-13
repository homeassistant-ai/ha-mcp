"""Home Assistant's ``pip_kwargs`` across its 2026.11 signature change."""

from __future__ import annotations

import inspect
from typing import Any

from homeassistant import requirements


def pip_kwargs(config_dir: str) -> dict[str, Any]:
    """Return HA's pip install kwargs on either ``pip_kwargs`` signature.

    HA 2026.11 removed the ``config/deps`` target and with it the
    ``config_dir`` argument (home-assistant/core#168155).
    """
    if inspect.signature(requirements.pip_kwargs).parameters:
        kwargs: dict[str, Any] = requirements.pip_kwargs(config_dir)
    else:
        kwargs = requirements.pip_kwargs()
    return kwargs
