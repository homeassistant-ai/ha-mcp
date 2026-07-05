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
    """Assist-exposure seam double for the fetch's failure modes: answers the
    registry ``get_entries`` read and the ``expose_new`` read, or fails."""

    def __init__(self, expose_new=False, fail=False, new_fails=False):
        self._expose_new = expose_new
        self._fail = fail
        self._new_fails = new_fails

    async def send_websocket_message(self, msg):
        if self._fail:
            raise ConnectionError("ws down")
        if msg["type"] == "config/entity_registry/get_entries":
            return {"success": True, "result": {}}
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
    # Faithful seam: expose_entity/list only ever reports True, so the explicit
    # should_expose (incl. the False un-expose) is read from the registry entry
    # options via get_entries. expose_new off -> only the explicit setting decides.
    client = _FakeRegistryOptionsClient(
        entries=[
            {
                "entity_id": "light.a",
                "options": {"conversation": {"should_expose": True}},
            },
            {
                "entity_id": "light.b",
                "options": {"conversation": {"should_expose": False}},
            },
        ],
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
    client = _FakeAssistClient(expose_new=True, fail=True)
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
    client = _FakeAssistClient(expose_new=True, new_fails=True)
    hidden, warnings = asyncio.run(resolver.load_hidden_set(reg, None, client))
    assert hidden == set()  # dimension skipped, light.a not wrongly hidden
    assert any("Assist exposure data was unavailable" in w for w in warnings)


class _FakeRegistryOptionsClient:
    """Faithful Assist seam. The explicit per-entity ``should_expose`` (including
    ``False`` and HA's persisted computed defaults) lives in the entity-registry
    entry ``options`` and is reachable only via
    ``config/entity_registry/get_entries`` (extended_dict), not the partial
    ``config/entity_registry/list`` — and never via ``expose_entity/list``, which
    HA-core only ever returns ``True`` entries in (verified against a live
    158-entity instance — 0 ``False``). A stray ``expose_entity/list`` call is
    therefore a regression to the buggy reconstruction and fails the test.
    """

    def __init__(self, entries, expose_new):
        self._entries = entries  # extended registry dicts carrying ``options``
        self._expose_new = expose_new

    async def send_websocket_message(self, msg):
        msg_type = msg["type"]
        if msg_type == "homeassistant/expose_new_entities/get":
            return {"success": True, "result": {"expose_new": self._expose_new}}
        if msg_type == "config/entity_registry/get_entries":
            wanted = set(msg.get("entity_ids", []))
            return {
                "success": True,
                "result": {
                    e["entity_id"]: e for e in self._entries if e["entity_id"] in wanted
                },
            }
        raise AssertionError(f"unexpected ws message: {msg}")


def test_load_hidden_set_assist_reads_explicit_unexpose_from_registry_options(
    tmp_path, monkeypatch
):
    # Regression for the round-2 finding: an entity explicitly un-exposed from the
    # conversation assistant is ABSENT from expose_entity/list (which only ever
    # returns True), so the filter must read the explicit should_expose=False from
    # the registry entry options (get_entries) to honor it. expose_new=True is HA's
    # conversation default, and light is a default-exposed domain — so without the
    # options read light.b wrongly stays visible.
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
    client = _FakeRegistryOptionsClient(
        entries=[
            {"entity_id": "light.a", "options": {}},  # no explicit setting
            {
                "entity_id": "light.b",
                "options": {"conversation": {"should_expose": False}},
            },
        ],
        expose_new=True,
    )
    hidden, _ = asyncio.run(resolver.load_hidden_set(reg, None, client))
    # light.b explicitly un-exposed -> hidden; light.a default-exposed -> visible.
    assert hidden == {"light.b"}
