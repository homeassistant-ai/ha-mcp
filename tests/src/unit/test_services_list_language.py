"""The ``ha_mcp_tools/services_list`` wire language.

Both halves of the same contract: the command schema must NOT inject a
``language`` default (a voluptuous default is applied before the async prep
runs, so ``default="en"`` would pin every non-English instance to English),
and the prep must therefore read ``hass.config.language`` for an omitted value.

Kept in its own module rather than in ``test_component_ws_phase2_async.py``,
which is already over the 1,000-line ratchet limit.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

# Import through the sibling module: importing it installs the
# ``homeassistant.*`` MagicMock stubs and forces the REAL voluptuous into
# ``sys.modules`` first, so the component package (which imports homeassistant)
# is never hoisted above the stub install.
from .test_component_ws_search import _REAL_VOL, FakeHass, wsapi


def _validated(monkeypatch: pytest.MonkeyPatch, msg: dict) -> dict:
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    return _REAL_VOL.Schema(wsapi._services_list_schema())(msg)


def test_schema_leaves_an_omitted_language_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default here would beat ``hass.config.language`` for every request."""
    assert "language" not in _validated(monkeypatch, {"type": wsapi.WS_SERVICES_LIST})


def test_omitted_language_requests_the_configured_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prep asks Core for the instance's language, not English."""
    seen: list[str] = []

    async def _fake_desc(hass):
        return {}

    async def _fake_trans(hass, language):
        seen.append(language)
        return {}

    monkeypatch.setattr(wsapi, "_fetch_service_descriptions", _fake_desc)
    monkeypatch.setattr(wsapi, "_fetch_service_translations", _fake_trans)

    msg = _validated(monkeypatch, {"type": wsapi.WS_SERVICES_LIST})
    asyncio.run(
        wsapi._services_list_prep(FakeHass(config=SimpleNamespace(language="de")), msg)
    )

    assert seen == ["de"]
