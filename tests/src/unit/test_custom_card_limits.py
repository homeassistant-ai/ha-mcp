"""Broken installed cards must stay within their resource budgets.

The cards run through the component's public ``CustomCards`` and
``async_get_custom_cards``; the download test runs on a real ``hass`` whose
dashboard resources name a local card.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from custom_components.ha_mcp_tools import custom_cards as cc

from ._component_core import ws_result

# This test covers native thread ownership; the real DOM is fetched through
# the production cold-cache path in E2E, never vendored into fixtures.
_DOM = """
globalThis.__linkedom = {
  HTMLElement: class HTMLElement {},
  parseHTML: function () { return {
    document: {}, customElements: {define: function () {}}
  }; }
};
"""


@pytest.mark.parametrize("module", [False, True])
def test_all_quickjs_context_operations_use_the_creating_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: bool
) -> None:
    import quickjs

    context_type = quickjs.Context
    function_type = quickjs.Function
    calls: list[int] = []
    engines: list[Any] = []

    class TrackedContext:
        def __init__(self) -> None:
            calls.append(threading.get_ident())
            self.context = context_type()

        def __getattr__(self, name: str) -> Any:
            method = getattr(self.context, name)

            def tracked(*args: Any) -> Any:
                calls.append(threading.get_ident())
                return method(*args)

            return tracked

    def tracked_function(*args: Any, **kwargs: Any) -> Any:
        engine = function_type(*args, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(quickjs, "Context", TrackedContext)
    monkeypatch.setattr(quickjs, "Function", tracked_function)
    source = (
        "globalThis.modeOK = this === "
        + ("undefined;" if module else "globalThis;")
        + ("void import.meta; export " if module else "")
        + "class Card extends HTMLElement {setConfig(c) {if (!modeOK) throw new Error('wrong mode');}}"
        + "customElements.define('a-card', Card);"
    )
    card = tmp_path / "card.js"
    card.write_text(source)
    custom = cc.CustomCards(_DOM)
    custom.refresh([card], {card} if module else set())
    assert custom.check("a-card", {}) == []
    assert custom.describe("a-card") is not None
    # Removing the resource disposes its runtime.
    custom.refresh([])
    (engine,) = engines
    assert not hasattr(engine, "_context")
    with pytest.raises(RuntimeError, match="shutdown"):
        engine("check", None)
    assert len(set(calls)) == 1


# ``getConfigElement`` records each editor it builds; the editor reports the
# record when a config is checked against it. ``slow-card``'s editor never
# finishes loading: its timers keep rescheduling themselves.
_EDITORS = """
globalThis.__prepared = [];
function editorFor(tag) {
  __prepared.push(tag);
  return { setConfig: function () { throw new Error('prepared: ' + __prepared.join(',')); } };
}
class OneCard extends HTMLElement { setConfig(c) {} static getConfigElement() { return editorFor('one-card'); } }
class TwoCard extends HTMLElement { setConfig(c) {} static getConfigElement() { return editorFor('two-card'); } }
class SlowCard extends HTMLElement {
  setConfig(c) {}
  static getConfigForm() { return { schema: [{ name: 'entity', selector: { entity: {} } }] }; }
  static getConfigElement() {
    return new Promise(function () { (function spin() { setTimeout(spin); })(); });
  }
}
customElements.define('one-card', OneCard);
customElements.define('two-card', TwoCard);
customElements.define('slow-card', SlowCard);
"""


def test_only_requested_editors_are_prepared_and_cached(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cards = tmp_path / "cards.js"
    cards.write_text(_EDITORS)
    custom = cc.CustomCards(_DOM)
    custom.refresh([cards])
    try:
        # Loading prepared no editor, the check prepared only its own, and a
        # second check reuses it.
        prepared = [{"source": "editor", "message": "prepared: one-card"}]
        assert custom.check("one-card", {}) == prepared
        assert custom.check("one-card", {}) == prepared

        # An editor that outlives its budget is stopped and never used.
        with caplog.at_level(logging.DEBUG, logger=cc.__name__):
            described = custom.describe("slow-card")
        assert described is not None
        assert described["fields"] is None
        assert any(
            record.exc_info and record.exc_info[0] is TimeoutError
            for record in caplog.records
        )
        assert custom.check("slow-card", {}) == []
    finally:
        custom.refresh([])


@pytest.mark.asyncio
async def test_linkedom_download_stops_before_buffering_an_oversized_response(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aiohttp_client: ClientSessionGenerator,
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunk = b"x" * 64 * 1024
    total = 1024  # 64 MiB, far past the download limit
    sent: list[int] = []

    async def linkedom(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        try:
            for i in range(total):
                await response.write(chunk)
                sent.append(i)
        except OSError:
            pass  # the client hung up
        return response

    app = web.Application()
    app.router.add_get("/linkedom.tgz", linkedom)
    server = await aiohttp_client(app)
    monkeypatch.setattr(cc, "LINKEDOM_URL", str(server.make_url("/linkedom.tgz")))
    for name in ("_custom", "_refresh_task", "_dom_failed_at", "_resource_error"):
        monkeypatch.setattr(cc, name, None)
    transform = MagicMock(return_value="oversized content was accepted")
    monkeypatch.setattr(cc, "dom_script", transform)
    assert await async_setup_component(hass, "lovelace", {})
    ws = await hass_ws_client(hass)
    await ws_result(
        ws, "lovelace/resources/create", res_type="module", url="/local/card.js"
    )

    assert await cc.async_get_custom_cards(hass) is None

    status = cc.custom_load_status(None)
    assert status["state"] == "unavailable"
    assert "linkedom" in status["reason"]
    transform.assert_not_called()
    # The download ran and was cut off by its size limit, well before the end.
    assert any(
        isinstance(record.exc_info[1], ValueError)
        and "size limit" in str(record.exc_info[1])
        for record in caplog.records
        if record.exc_info
    )
    assert 0 < len(sent) < total
