"""Resource replacement and timed-out refreshes must retain their budgets."""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

import pytest

from .test_card_definitions import cc


def test_replacement_disposes_runtime_after_its_last_reader(tmp_path, monkeypatch):
    path = tmp_path / "card.js"
    path.write_text("first")
    entered, release, replacement = (threading.Event() for _ in range(3))
    events = []

    def form(tag):
        entered.set()
        assert release.wait(5)
        events.append("reader finished")
        return []

    def load(*args, **kwargs):
        if events:
            events.append("replacement allocated")
            replacement.set()
        else:
            events.append("first allocated")
        return MagicMock(
            tags=["test-card"],
            cards=[],
            form=form,
            close=lambda: events.append("runtime disposed"),
        )

    monkeypatch.setattr(cc, "_Bundle", load)
    monkeypatch.setattr(cc, "_TOTAL_MEMORY", cc._BUNDLE_MEMORY)
    custom = cc.CustomCards("dom")
    custom.refresh([path])
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(custom.describe, "test-card")
        assert entered.wait(5)
        stamp = path.stat().st_mtime + 1
        os.utime(path, (stamp, stamp))
        refresh = pool.submit(custom.refresh, [path])
        try:
            assert not replacement.wait(0.2), "replacement overlaps active runtime"
        finally:
            release.set()
        reader.result(5)
        refresh.result(5)
    assert events == [
        "first allocated",
        "reader finished",
        "runtime disposed",
        "replacement allocated",
    ]


def test_refresh_stops_starting_bundles_when_total_budget_expires(
    tmp_path, monkeypatch
):
    files = [tmp_path / f"broken-{i}.js" for i in range(10)]
    for path in files:
        path.write_text("broken")
    clock = [0.0]
    attempts = []

    def load(dom, source, **kwargs):
        attempts.append(source)
        clock[0] += 4
        raise ValueError("broken card")

    monkeypatch.setattr(cc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cc, "_Bundle", load)
    custom = cc.CustomCards("dom")
    custom.refresh(files)
    first_pass = len(attempts)
    assert 0 < first_pass < len(files)
    custom.refresh(files)
    assert first_pass < len(attempts) <= 2 * first_pass


@pytest.mark.asyncio
async def test_timed_out_callers_share_one_background_refresh(monkeypatch):
    release = asyncio.Event()
    result = object()

    async def refresh(hass):
        await release.wait()
        return result

    worker = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(cc, "_async_refresh", worker)
    monkeypatch.setattr(cc, "_refresh_task", None, raising=False)
    tasks = []

    def create(coro, name):
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    hass = MagicMock()
    hass.async_create_background_task = create
    try:
        responses = await asyncio.gather(
            *(cc.async_get_custom_cards(hass, timeout=0.01) for _ in range(10))
        )
        assert responses == [None] * 10
        assert worker.await_count == 1
        release.set()
        assert await cc.async_get_custom_cards(hass, timeout=1) is result
    finally:
        release.set()
        await asyncio.gather(*tasks)
