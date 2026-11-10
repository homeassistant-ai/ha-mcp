"""The app tree runs the stand-in guard too."""

import pytest

from tests import core_stand_in_guard as guard


def test_app_conftest_registers_the_hook(pytestconfig: pytest.Config) -> None:
    assert guard.is_registered(pytestconfig)
