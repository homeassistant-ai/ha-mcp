"""The component's ``dashboards`` ``docs`` mode and its card walk's reach.

``docs`` hands the server every storage dashboard's config for the card search
(issue #2694). Servers predating the ``dashboards_docs`` capability still read
``search`` matches, so that walk must reach cards nested under a custom card's
own keys too.
"""

from __future__ import annotations

from typing import Any

from .test_component_ws_phase2_async import (
    FakeDashboard,
    _run_dashboards,
    _storage_dash,
)
from .test_component_ws_search import FakeHass, wsapi

_BODY = {"views": [{"cards": [{"type": "tile", "entity": "light.a"}]}]}


def _dashboards(monkeypatch: Any, dashboards_map: dict[Any, Any]) -> None:
    monkeypatch.setattr(wsapi, "_lovelace_dashboards_map", lambda hass: dashboards_map)


def test_docs_returns_storage_configs_only(monkeypatch: Any) -> None:
    """YAML bodies (including a YAML default dashboard) never leave the component."""
    _dashboards(
        monkeypatch,
        {
            None: FakeDashboard(None, "yaml", config={}, body=_BODY),
            "home": _storage_dash("home", "Home", body=_BODY),
            "yaml-dash": FakeDashboard("yaml-dash", "yaml", config={}, body=_BODY),
        },
    )

    result = _run_dashboards(FakeHass(), {"mode": "docs"})

    assert result["docs"] == [{"url_path": "home", "config": _BODY}]
    assert result["load_failed"] == 0


def test_docs_counts_dashboards_that_fail_to_load(monkeypatch: Any) -> None:
    _dashboards(
        monkeypatch,
        {
            "home": _storage_dash("home", "Home", body=_BODY),
            "broken": FakeDashboard(
                "broken", "storage", config={}, load_error=RuntimeError("boom")
            ),
        },
    )

    result = _run_dashboards(FakeHass(), {"mode": "docs"})

    assert [doc["url_path"] for doc in result["docs"]] == ["home"]
    assert result["load_failed"] == 1


def test_docs_without_lovelace_is_unavailable(monkeypatch: Any) -> None:
    monkeypatch.setattr(wsapi, "_lovelace_dashboards_map", lambda hass: None)

    result = _run_dashboards(FakeHass(), {"mode": "docs"})

    assert result == {"mode": "docs", "available": False, "docs": []}


def test_search_reaches_cards_under_a_custom_cards_own_keys(monkeypatch: Any) -> None:
    """``search`` matches (read by older servers) reach nested custom-card cards."""
    body = {
        "views": [
            {
                "cards": [
                    {
                        "type": "custom:astra-board-page",
                        "groups": [
                            {"cards": [{"card": {"type": "tile", "entity": "light.x"}}]}
                        ],
                    },
                    {
                        "type": "custom:button-card",
                        "custom_fields": {
                            "my-field": {"type": "tile", "entity": "light.x"}
                        },
                    },
                ]
            }
        ]
    }
    _dashboards(monkeypatch, {"home": _storage_dash("home", "Home", body=body)})

    result = _run_dashboards(FakeHass(), {"mode": "search", "query": "light.x"})

    assert [(m["card_path"], m["card_type"]) for m in result["matches"]] == [
        ("views[0].cards[0].groups[0].cards[0].card", "tile"),
        ('views[0].cards[1].custom_fields["my-field"]', "tile"),
    ]


def test_docs_capability_is_advertised() -> None:
    """The component advertises ``dashboards_docs``, which gates the server's ``docs`` read."""
    assert "dashboards_docs" in wsapi._do_info(FakeHass())["capabilities"]
