"""Broken installed cards must stay within their resource budgets."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from .test_card_definitions import cc


def test_all_quickjs_context_operations_use_the_creating_thread(monkeypatch) -> None:
    import quickjs

    context_type = quickjs.Context
    calls: list[int] = []

    class TrackedContext:
        def __init__(self):
            calls.append(threading.get_ident())
            self.context = context_type()

        def __getattr__(self, name):
            method = getattr(self.context, name)

            def tracked(*args):
                calls.append(threading.get_ident())
                return method(*args)

            return tracked

    monkeypatch.setattr(quickjs, "Context", TrackedContext)
    dom = (
        Path(__file__).parents[2]
        / "initial_test_state/.storage/ha_mcp_tools/linkedom-0.18.13.js"
    ).read_text(encoding="utf-8")
    bundle = cc._Bundle(
        dom, "customElements.define('a-card', class extends HTMLElement {});"
    )
    bundle.check("a-card", {})
    bundle.form("a-card")
    assert bundle.memory > 0
    assert len(set(calls)) == 1


def test_preparing_many_tags_has_one_total_budget(monkeypatch) -> None:
    import quickjs

    clock = [0.0]
    prepared = []

    def call(op, payload):
        if op == "cards":
            return {"value": {"tags": list(range(100)), "cards": []}}
        if op == "prepare":
            clock[0] += 1
            prepared.append(payload)
        return {}

    monkeypatch.setattr(
        quickjs, "Function", MagicMock(return_value=MagicMock(side_effect=call))
    )
    monkeypatch.setattr(cc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cc._Bundle, "_settle", lambda self: None)
    monkeypatch.setattr(cc, "_PREPARE_SECONDS", 2, raising=False)
    with pytest.raises(TimeoutError):
        cc._Bundle("dom", "source")
    assert len(prepared) < 100


@pytest.mark.asyncio
async def test_linkedom_download_stops_before_buffering_an_oversized_response(
    tmp_path, monkeypatch
) -> None:
    import sys

    consumed = []

    async def chunks(size):
        for i in range(20):
            consumed.append(i)
            yield b"x" * 8

    response = MagicMock()
    response.content.iter_chunked = chunks
    response.read = AsyncMock(return_value=b"x" * 160)
    session = MagicMock()
    session.get.return_value.__aenter__ = AsyncMock(return_value=response)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.aiohttp_client",
        SimpleNamespace(async_get_clientsession=lambda hass: session),
    )
    monkeypatch.setattr(cc, "_dom_failed_at", None)
    monkeypatch.setattr(cc, "_MAX_DOM_DOWNLOAD_BYTES", 16, raising=False)
    transform = MagicMock(return_value="oversized content was accepted")
    monkeypatch.setattr(cc, "dom_script", transform)
    hass = MagicMock()
    hass.config.path.side_effect = lambda *parts: str(tmp_path.joinpath(*parts))
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    assert await cc._async_dom(hass) is None
    transform.assert_not_called()
    response.read.assert_not_awaited()
    assert len(consumed) < 20
