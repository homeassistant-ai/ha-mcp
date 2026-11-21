"""Sets the current time one module reads, leaving every other clock alone."""

from __future__ import annotations

from datetime import datetime, tzinfo
from types import ModuleType
from typing import Self

import pytest


def set_module_clock(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, now: datetime
) -> None:
    """Make ``datetime.now`` in ``module`` answer ``now``.

    Only that module's clock: Core's ``freezer`` would also stop the event
    loop the client runs on.
    """

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(now.timestamp(), tz)

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
