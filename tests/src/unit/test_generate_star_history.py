"""Unit tests for the self-hosted star-history chart generator."""

from __future__ import annotations

import importlib.util
import io
import json
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "generate_star_history.py"
_spec = importlib.util.spec_from_file_location("generate_star_history", _SCRIPT)
assert _spec and _spec.loader
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _history():
    return generator.aggregate_daily(
        "homeassistant-ai/ha-mcp",
        [
            datetime(2026, 1, 1, 12, tzinfo=UTC),
            datetime(2026, 1, 1, 18, tzinfo=UTC),
            datetime(2026, 1, 3, 9, tzinfo=UTC),
            datetime(2026, 1, 5, 20, tzinfo=UTC),
        ],
        generated_on=date(2026, 1, 6),
    )


def test_fetch_starred_at_paginates_and_discards_identity() -> None:
    first_page = [
        {
            "starred_at": f"2026-01-{1 + index // 24:02d}T{index % 24:02d}:00:00Z",
            "user": {"login": f"private-user-{index}"},
        }
        for index in range(100)
    ]
    second_page = [
        {
            "starred_at": "2026-01-06T00:00:00Z",
            "user": {"login": "private-user-final"},
        }
    ]
    responses = [first_page, second_page]
    requested_pages = []

    def opener(request, *, timeout):
        assert timeout == 30
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["Accept"] == "application/vnd.github.star+json"
        requested_pages.append(request.full_url)
        return _Response(json.dumps(responses.pop(0)).encode())

    timestamps = generator.fetch_starred_at(
        "homeassistant-ai/ha-mcp", "test-token", opener=opener
    )

    assert len(timestamps) == 101
    assert requested_pages[0].endswith("per_page=100&page=1")
    assert requested_pages[1].endswith("per_page=100&page=2")
    assert all(isinstance(timestamp, datetime) for timestamp in timestamps)
    assert not any("private-user" in str(timestamp) for timestamp in timestamps)


def test_aggregate_daily_fills_gaps_and_accumulates() -> None:
    history = _history()

    assert history.started_on == date(2026, 1, 1)
    assert history.generated_on == date(2026, 1, 6)
    assert history.total == 4
    assert [(point.day, point.stars) for point in history.points] == [
        (date(2026, 1, 1), 2),
        (date(2026, 1, 2), 2),
        (date(2026, 1, 3), 3),
        (date(2026, 1, 4), 3),
        (date(2026, 1, 5), 4),
        (date(2026, 1, 6), 4),
    ]


def test_aggregate_daily_rejects_empty_history() -> None:
    with pytest.raises(ValueError, match="at least one"):
        generator.aggregate_daily("owner/repo", [])


def test_render_starts_on_january_2026_and_smooths_monotonically() -> None:
    history = generator.aggregate_daily(
        "homeassistant-ai/ha-mcp",
        [
            datetime(2025, 12, 30, 12, tzinfo=UTC),
            datetime(2025, 12, 31, 12, tzinfo=UTC),
            datetime(2026, 1, 2, 12, tzinfo=UTC),
            datetime(2026, 1, 4, 12, tzinfo=UTC),
        ],
        generated_on=date(2026, 1, 5),
    )

    svg = generator.render_svg(history, "light")

    assert "Jan 2026" in svg
    assert "Dec 2025" not in svg
    assert "grew from 2 stars on January 1, 2026" in svg
    assert " C " in svg


def test_monotone_tangents_do_not_overshoot_flat_growth() -> None:
    coordinates = [(0.0, 10.0), (1.0, 10.0), (2.0, 8.0), (3.0, 5.0)]
    tangents = generator._monotone_tangents(coordinates)

    assert tangents[0] == 0
    assert tangents[1] == 0
    for index, ((x, y), (next_x, next_y)) in enumerate(pairwise(coordinates)):
        width = next_x - x
        control_y = y + tangents[index] * width / 3
        next_control_y = next_y - tangents[index + 1] * width / 3
        low, high = sorted((y, next_y))
        assert low <= control_y <= high
        assert low <= next_control_y <= high


def test_repository_must_use_owner_repo_format() -> None:
    with pytest.raises(ValueError, match="OWNER/REPO"):
        generator.fetch_starred_at("invalid", "token", opener=lambda *_a, **_k: None)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_chart_is_valid_accessible_svg(mode: str) -> None:
    svg = generator.render_svg(_history(), mode)
    root = ET.fromstring(svg)
    namespace = {"svg": "http://www.w3.org/2000/svg"}

    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.attrib["role"] == "img"
    assert root.attrib["aria-labelledby"] == "chart-title chart-description"
    assert root.find("svg:title", namespace) is not None
    assert root.find("svg:desc", namespace) is not None
    assert "private-user" not in svg
    assert "homeassistant-ai/ha-mcp" in svg
    assert "4" in svg


def test_write_charts_uses_stable_production_names(tmp_path: Path) -> None:
    written = generator.write_charts(_history(), tmp_path)

    assert written == [tmp_path / "light.svg", tmp_path / "dark.svg"]
    assert all(path.is_file() for path in written)
