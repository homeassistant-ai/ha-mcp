"""The component's dashboard get for a dashboard with no saved config (#2632)."""

from collections.abc import Callable
from typing import Any

from .test_component_ws_phase2_async import (
    ConfigNotFound,
    FakeDashboard,
    _run_dashboards,
    patch_dashboards,  # noqa: F401 - fixture
)
from .test_component_ws_search import FakeHass


def test_get_dashboard_without_saved_config_is_not_missing(
    patch_dashboards: Callable[[dict[str, Any]], None],  # noqa: F811
) -> None:
    """A dashboard Core loads with ``ConfigNotFound`` exists; it has no saved
    config yet, so the get must not report it missing."""
    patch_dashboards(
        {"fresh": FakeDashboard("fresh", "storage", load_error=ConfigNotFound())}
    )
    res = _run_dashboards(FakeHass(), {"mode": "get", "url_path": "fresh"})
    assert res["status"] == "no_config"
    assert res["config"] is None
