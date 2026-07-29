#!/usr/bin/env python3
"""Generate privacy-preserving star-history SVGs for the project website."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from html import escape
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_VERSION = "2022-11-28"
DEFAULT_REPOSITORY = "homeassistant-ai/ha-mcp"
DEFAULT_OUTPUT_DIR = Path("site/public/star-history")
DEFAULT_START_ON = date(2026, 1, 1)
WIDTH = 960
HEIGHT = 520
PLOT_LEFT = 82
PLOT_RIGHT = 900
PLOT_TOP = 154
PLOT_BOTTOM = 432


class DailyPoint(NamedTuple):
    day: date
    stars: int


class StarHistory(NamedTuple):
    repository: str
    generated_on: date
    points: tuple[DailyPoint, ...]

    @property
    def total(self) -> int:
        return self.points[-1].stars

    @property
    def started_on(self) -> date:
        return self.points[0].day


class Theme(NamedTuple):
    surface: str
    plot_surface: str
    text: str
    secondary_text: str
    muted_text: str
    grid: str
    border: str
    accent: str
    accent_end: str


LINE_WIDTH = 3.2
AREA_OPACITY = 0.16
THEMES = {
    "light": Theme(
        "#EFF9FE",
        "#FFFFFF",
        "#001721",
        "#004156",
        "#4A6B75",
        "#DFF3FC",
        "#B9E6FC",
        "#009AC7",
        "#2FC1CF",
    ),
    "dark": Theme(
        "#001721",
        "#002E3E",
        "#EFF9FE",
        "#B9E6FC",
        "#7BD4FB",
        "#004156",
        "#006787",
        "#009AC7",
        "#2FC1CF",
    ),
}


def _api_url(repository: str, page: int) -> str:
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("repository must use the OWNER/REPO format")
    return (
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(name, safe='')}/stargazers"
        f"?per_page=100&page={page}"
    )


def fetch_starred_at(
    repository: str,
    token: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> list[datetime]:
    """Fetch timestamped stars while retaining no account identity fields."""
    if not token:
        raise ValueError("GITHUB_TOKEN or GH_TOKEN is required")

    starred_at: list[datetime] = []
    page = 1
    while True:
        request = Request(
            _api_url(repository, page),
            headers={
                "Accept": "application/vnd.github.star+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "ha-mcp-star-history",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with opener(request, timeout=30) as response:
                payload = json.load(response)
        except HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub stargazers request failed with HTTP {err.code}: {detail}"
            ) from err
        except URLError as err:
            raise RuntimeError(f"GitHub stargazers request failed: {err.reason}") from err

        if not isinstance(payload, list):
            raise RuntimeError("GitHub stargazers response was not a list")
        for item in payload:
            if not isinstance(item, dict) or not isinstance(
                item.get("starred_at"), str
            ):
                raise RuntimeError("GitHub response omitted a starred_at timestamp")
            starred_at.append(_parse_timestamp(item["starred_at"]))

        if len(payload) < 100:
            return starred_at
        page += 1


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise RuntimeError(f"Invalid GitHub starred_at timestamp: {value!r}") from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def aggregate_daily(
    repository: str,
    timestamps: Iterable[datetime],
    *,
    generated_on: date | None = None,
) -> StarHistory:
    """Aggregate timestamps into cumulative daily totals."""
    counts = Counter(timestamp.astimezone(UTC).date() for timestamp in timestamps)
    if not counts:
        raise ValueError("at least one stargazer timestamp is required")

    last_star_day = max(counts)
    end = generated_on or datetime.now(tz=UTC).date()
    if end < last_star_day:
        raise ValueError("generated_on cannot precede the latest star")

    current = min(counts)
    cumulative = 0
    points: list[DailyPoint] = []
    while current <= end:
        cumulative += counts[current]
        points.append(DailyPoint(current, cumulative))
        current += timedelta(days=1)
    return StarHistory(repository, end, tuple(points))


def _nice_ticks(total: int) -> tuple[int, list[int]]:
    rough_step = max(total / 4, 1)
    magnitude = 10 ** math.floor(math.log10(rough_step))
    normalized = rough_step / magnitude
    step_factor = min((1, 2, 2.5, 5, 10), key=lambda value: abs(value - normalized))
    step = max(1, int(step_factor * magnitude))
    maximum = math.ceil(total / step) * step
    ticks = list(range(0, maximum + step, step))
    return maximum, ticks


def _format_count(value: int) -> str:
    return f"{value:,}"


def _compact_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 10_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return f"{round(value / 1_000):.0f}K"


def _coordinates(
    history: StarHistory, maximum: int
) -> tuple[list[tuple[float, float]], Callable[[date], float]]:
    span_days = max((history.generated_on - history.started_on).days, 1)

    def x_for(day: date) -> float:
        ratio = (day - history.started_on).days / span_days
        return PLOT_LEFT + ratio * (PLOT_RIGHT - PLOT_LEFT)

    coordinates = [
        (
            x_for(point.day),
            PLOT_BOTTOM
            - (point.stars / maximum) * (PLOT_BOTTOM - PLOT_TOP),
        )
        for point in history.points
    ]
    return coordinates, x_for


def _monotone_tangents(
    coordinates: Sequence[tuple[float, float]],
) -> list[float]:
    slopes = [
        (next_y - y) / (next_x - x)
        for (x, y), (next_x, next_y) in pairwise(coordinates)
    ]
    tangents = [slopes[0]]
    for previous, following in pairwise(slopes):
        tangents.append(0.0 if previous * following <= 0 else (previous + following) / 2)
    tangents.append(slopes[-1])

    for index, slope in enumerate(slopes):
        if slope == 0:
            tangents[index] = 0.0
            tangents[index + 1] = 0.0
            continue
        alpha = tangents[index] / slope
        beta = tangents[index + 1] / slope
        magnitude = alpha * alpha + beta * beta
        if magnitude > 9:
            scale = 3 / math.sqrt(magnitude)
            tangents[index] = scale * alpha * slope
            tangents[index + 1] = scale * beta * slope
    return tangents


def _line_path(coordinates: Sequence[tuple[float, float]]) -> str:
    first_x, first_y = coordinates[0]
    commands = [f"M {first_x:.2f} {first_y:.2f}"]
    if len(coordinates) == 1:
        return commands[0]

    tangents = _monotone_tangents(coordinates)
    for index, ((x, y), (next_x, next_y)) in enumerate(pairwise(coordinates)):
        width = next_x - x
        control_1_x = x + width / 3
        control_1_y = y + tangents[index] * width / 3
        control_2_x = next_x - width / 3
        control_2_y = next_y - tangents[index + 1] * width / 3
        commands.append(
            f"C {control_1_x:.2f} {control_1_y:.2f} "
            f"{control_2_x:.2f} {control_2_y:.2f} "
            f"{next_x:.2f} {next_y:.2f}"
        )
    return " ".join(commands)


def _window_history(history: StarHistory, start_on: date) -> StarHistory:
    points = tuple(point for point in history.points if point.day >= start_on)
    if not points:
        raise ValueError("chart start date must not follow generated_on")
    return StarHistory(history.repository, history.generated_on, points)


def _date_ticks(history: StarHistory, count: int = 5) -> list[date]:
    span = (history.generated_on - history.started_on).days
    return [
        history.started_on + timedelta(days=round(span * index / (count - 1)))
        for index in range(count)
    ]


def _header_svg(theme: Theme, history: StarHistory) -> str:
    return f"""
  <text x="82" y="58" fill="{theme.muted_text}" font-size="12" font-weight="650" letter-spacing="1.1">COMMUNITY GROWTH</text>
  <text x="82" y="91" fill="{theme.text}" font-size="28" font-weight="700">Star history</text>
  <text x="900" y="65" fill="{theme.text}" font-size="36" font-weight="720" text-anchor="end">{_format_count(history.total)}</text>
  <text x="900" y="91" fill="{theme.secondary_text}" font-size="13" text-anchor="end">{escape(history.repository)}</text>"""


def render_svg(
    history: StarHistory,
    mode: str,
    *,
    start_on: date = DEFAULT_START_ON,
) -> str:
    """Render one standalone, accessible star-history SVG."""
    history = _window_history(history, start_on)
    try:
        theme = THEMES[mode]
    except KeyError as err:
        raise ValueError("mode must be 'light' or 'dark'") from err

    maximum, y_ticks = _nice_ticks(history.total)
    coordinates, x_for = _coordinates(history, maximum)
    path = _line_path(coordinates)
    area = (
        f"{path} L {coordinates[-1][0]:.2f} {PLOT_BOTTOM} "
        f"L {coordinates[0][0]:.2f} {PLOT_BOTTOM} Z"
    )
    gradient_id = f"area-{mode}"
    line_gradient_id = f"line-{mode}"

    horizontal_grid = []
    for tick in y_ticks:
        y = PLOT_BOTTOM - (tick / maximum) * (PLOT_BOTTOM - PLOT_TOP)
        horizontal_grid.append(
            f'<line x1="{PLOT_LEFT}" y1="{y:.2f}" x2="{PLOT_RIGHT}" '
            f'y2="{y:.2f}" stroke="{theme.grid}"/>'
        )
        horizontal_grid.append(
            f'<text x="{PLOT_LEFT - 14}" y="{y + 4:.2f}" fill="{theme.muted_text}" '
            f'font-size="11" text-anchor="end">{_compact_count(tick)}</text>'
        )

    date_grid = []
    for tick_day in _date_ticks(history):
        x = x_for(tick_day)
        date_grid.append(
            f'<text x="{x:.2f}" y="{PLOT_BOTTOM + 28}" fill="{theme.muted_text}" '
            f'font-size="11" text-anchor="middle">{tick_day:%b %Y}</text>'
        )

    area_element = (
        f'<path d="{area}" fill="url(#{gradient_id})" opacity="{AREA_OPACITY}"/>'
    )
    end_x, end_y = coordinates[-1]
    endpoint_elements = f"""
  <circle cx="{end_x:.2f}" cy="{end_y:.2f}" r="5" fill="{theme.accent_end}" stroke="{theme.plot_surface}" stroke-width="3"/>
  <text x="{end_x - 10:.2f}" y="{max(PLOT_TOP + 14, end_y - 12):.2f}" fill="{theme.text}" font-size="12" font-weight="700" text-anchor="end">{_format_count(history.total)}</text>"""
    description = escape(
        f"{history.repository} grew from {history.points[0].stars:,} stars on "
        f"{history.started_on:%B %-d, %Y} to {history.total:,} stars on "
        f"{history.generated_on:%B %-d, %Y}."
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="chart-title chart-description">
  <title id="chart-title">Star history for {escape(history.repository)}</title>
  <desc id="chart-description">{description}</desc>
  <defs>
    <linearGradient id="{gradient_id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{theme.accent}" stop-opacity="1"/>
      <stop offset="1" stop-color="{theme.accent_end}" stop-opacity="0.08"/>
    </linearGradient>
    <linearGradient id="{line_gradient_id}" x1="{PLOT_LEFT}" y1="0" x2="{PLOT_RIGHT}" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{theme.accent}"/>
      <stop offset="1" stop-color="{theme.accent_end}"/>
    </linearGradient>
  </defs>
  <rect width="{WIDTH}" height="{HEIGHT}" rx="18" fill="{theme.surface}"/>
  {_header_svg(theme, history)}
  <rect x="{PLOT_LEFT}" y="{PLOT_TOP}" width="{PLOT_RIGHT - PLOT_LEFT}" height="{PLOT_BOTTOM - PLOT_TOP}" rx="8" fill="{theme.plot_surface}" stroke="{theme.border}"/>
  <g stroke-width="1" shape-rendering="crispEdges">
    {''.join(horizontal_grid)}
    {''.join(date_grid)}
  </g>
  {area_element}
  <path d="{path}" fill="none" stroke="url(#{line_gradient_id})" stroke-width="{LINE_WIDTH}" stroke-linecap="round" stroke-linejoin="round"/>
  {endpoint_elements}
  <text x="{PLOT_LEFT}" y="493" fill="{theme.muted_text}" font-size="10">Updated {history.generated_on:%Y-%m-%d} · generated from GitHub's stargazers API</text>
</svg>
"""


def write_charts(history: StarHistory, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for mode in ("light", "dark"):
        destination = output_dir / f"{mode}.svg"
        destination.write_text(render_svg(history, mode), encoding="utf-8")
        written.append(destination)
    return written


def _load_timestamps(path: Path) -> list[datetime]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise ValueError("input JSON must be an array of timestamp strings")
    return [_parse_timestamp(item) for item in payload]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--input-json", type=Path)
    parser.add_argument("--generated-on", type=date.fromisoformat)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.input_json:
            timestamps = _load_timestamps(args.input_json)
        else:
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
            timestamps = fetch_starred_at(args.repository, token)
        history = aggregate_daily(
            args.repository, timestamps, generated_on=args.generated_on
        )
        written = write_charts(history, args.output_dir)
    except (OSError, RuntimeError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    print(
        f"Generated {len(written)} SVGs for {history.total:,} stars "
        f"through {history.generated_on.isoformat()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
