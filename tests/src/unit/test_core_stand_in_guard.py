"""The guard that fails a test leaving a stand-in for a Home Assistant module."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import homeassistant.core

from tests.core_stand_in_guard import core_module_stand_ins


def test_an_object_in_place_of_a_core_module_is_reported() -> None:
    name = "homeassistant.helpers.a_stand_in"
    sys.modules[name] = SimpleNamespace()  # type: ignore[assignment]
    try:
        assert name in core_module_stand_ins()
    finally:
        del sys.modules[name]


def test_an_imported_core_module_is_not_reported() -> None:
    assert homeassistant.core.__name__ not in core_module_stand_ins()
