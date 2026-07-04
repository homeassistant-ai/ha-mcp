import asyncio

from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config


def test_load_hidden_set_reads_config(tmp_path, monkeypatch):
    save_visibility_config(
        tmp_path, VisibilityConfig(enabled=True, exclude_categories=["diagnostic"])
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    reg = {
        "success": True,
        "result": [{"entity_id": "s.b", "entity_category": "diagnostic"}],
    }
    assert asyncio.run(resolver.load_hidden_set(reg))[0] == {"s.b"}


def test_load_hidden_set_fails_open_on_bad_config(tmp_path, monkeypatch):
    (tmp_path / "entity_visibility.json").write_text("{ corrupt")
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    reg = {
        "success": True,
        "result": [{"entity_id": "s.b", "entity_category": "diagnostic"}],
    }
    hidden, warnings = asyncio.run(resolver.load_hidden_set(reg))
    assert hidden == set()
    assert any("could not be loaded" in w for w in warnings)


class _FakeAssistClient:
    """Answers the two Assist-exposure websocket reads (or fails)."""

    def __init__(self, exposed, expose_new, fail=False, new_fails=False):
        self._exposed = exposed
        self._expose_new = expose_new
        self._fail = fail
        self._new_fails = new_fails

    async def send_websocket_message(self, msg):
        if self._fail:
            raise ConnectionError("ws down")
        if msg["type"] == "homeassistant/expose_entity/list":
            return {"success": True, "result": {"exposed_entities": self._exposed}}
        if msg["type"] == "homeassistant/expose_new_entities/get":
            if self._new_fails:
                return {"success": False, "error": {"message": "boom"}}
            return {"success": True, "result": {"expose_new": self._expose_new}}
        raise AssertionError(f"unexpected ws message: {msg}")


def test_load_hidden_set_fetches_assist_and_hides_unexposed(tmp_path, monkeypatch):
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True, exclude_categories=[], respect_assist_exposure=True
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    reg = {
        "success": True,
        "result": [{"entity_id": "light.a"}, {"entity_id": "light.b"}],
    }
    # light.a explicitly exposed, light.b explicitly un-exposed; expose_new off so
    # only the explicit conversation map decides.
    client = _FakeAssistClient(
        exposed={"light.a": {"conversation": True}, "light.b": {"conversation": False}},
        expose_new=False,
    )
    hidden, _ = asyncio.run(resolver.load_hidden_set(reg, None, client))
    assert hidden == {"light.b"}


def test_load_hidden_set_assist_fetch_fails_soft(tmp_path, monkeypatch):
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True,
            exclude_categories=[],
            respect_assist_exposure=True,
            deny_entity_ids=["light.denied"],
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    reg = {"success": True, "result": [{"entity_id": "light.a"}]}
    client = _FakeAssistClient(exposed={}, expose_new=True, fail=True)
    hidden, warnings = asyncio.run(resolver.load_hidden_set(reg, None, client))
    # Assist dimension degrades (skipped) but deny still applies; not fail-closed.
    assert hidden == {"light.denied"}
    assert any("Assist exposure data was unavailable" in w for w in warnings)


def test_load_hidden_set_assist_partial_failure_degrades_dimension(
    tmp_path, monkeypatch
):
    # expose_entity/list succeeds but expose_new/get fails: without the flag the
    # default-exposure branch cannot be computed, so the whole dimension degrades
    # (skipped + warning) rather than assuming expose_new=False and wrongly hiding
    # a default-domain entity that has no explicit override.
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True, exclude_categories=[], respect_assist_exposure=True
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    reg = {"success": True, "result": [{"entity_id": "light.a"}]}
    client = _FakeAssistClient(exposed={}, expose_new=True, new_fails=True)
    hidden, warnings = asyncio.run(resolver.load_hidden_set(reg, None, client))
    assert hidden == set()  # dimension skipped, light.a not wrongly hidden
    assert any("Assist exposure data was unavailable" in w for w in warnings)
