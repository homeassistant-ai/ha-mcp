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
    ``expose_entity/list`` read and the ``expose_new`` read, or fails."""

    def __init__(self, expose_new=False, fail=False, new_fails=False):
        self._expose_new = expose_new
        self._fail = fail
        self._new_fails = new_fails

    async def send_websocket_message(self, msg):
        if self._fail:
            raise ConnectionError("ws down")
        if msg["type"] == "homeassistant/expose_entity/list":
            return {"success": True, "result": {"exposed_entities": {}}}
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
    # Faithful seam: each registry entry's explicit should_expose (incl. the False
    # un-expose) lives in the ``options`` the registry list payload already carries;
    # expose_entity/list only ever reports the True one. expose_new off -> only the
    # explicit setting decides.
    reg = {
        "success": True,
        "result": [
            {
                "entity_id": "light.a",
                "options": {"conversation": {"should_expose": True}},
            },
            {
                "entity_id": "light.b",
                "options": {"conversation": {"should_expose": False}},
            },
        ],
    }
    client = _FakeExposeSeamClient(
        exposed={"light.a": {"conversation": True}}, expose_new=False
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


class _FakeExposeSeamClient:
    """Faithful Assist seam: serves the two reads the resolver actually makes —
    ``homeassistant/expose_entity/list`` (the True-only exposed set) and
    ``homeassistant/expose_new_entities/get``. A registry entry's explicit
    ``should_expose`` (True *or* False) is NOT served here: it lives in the
    ``config/entity_registry/list`` payload's ``options`` (HA's ``as_partial_dict``
    carries it), which the caller puts in ``reg``. A stray
    ``config/entity_registry/get_entries`` call is a regression to the redundant
    round-trip and fails the test.
    """

    def __init__(self, exposed=None, expose_new=False):
        # exposed: {entity_id: {assistant: True}} — HA's expose_entity/list shape.
        self._exposed = exposed or {}
        self._expose_new = expose_new

    async def send_websocket_message(self, msg):
        msg_type = msg["type"]
        if msg_type == "homeassistant/expose_new_entities/get":
            return {"success": True, "result": {"expose_new": self._expose_new}}
        if msg_type == "homeassistant/expose_entity/list":
            return {
                "success": True,
                "result": {"exposed_entities": self._exposed},
            }
        raise AssertionError(f"unexpected ws message: {msg}")


def test_load_hidden_set_assist_reads_explicit_unexpose_from_registry_options(
    tmp_path, monkeypatch
):
    # Regression for the round-2 finding: an entity explicitly un-exposed from the
    # conversation assistant is ABSENT from expose_entity/list (which only ever
    # returns True), so the filter reads the explicit should_expose=False from the
    # registry entry ``options`` in the list payload to honor it. expose_new=True is
    # HA's conversation default, and light is a default-exposed domain — so without
    # the options read light.b wrongly stays visible.
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True, exclude_categories=[], respect_assist_exposure=True
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    reg = {
        "success": True,
        "result": [
            {"entity_id": "light.a", "options": {}},  # no explicit setting
            {
                "entity_id": "light.b",
                "options": {"conversation": {"should_expose": False}},
            },
        ],
    }
    # expose_entity/list reports neither (light.a has no explicit True, light.b is
    # explicitly False) — the False must come from the registry payload options.
    client = _FakeExposeSeamClient(exposed={}, expose_new=True)
    hidden, _ = asyncio.run(resolver.load_hidden_set(reg, None, client))
    # light.b explicitly un-exposed -> hidden; light.a default-exposed -> visible.
    assert hidden == {"light.b"}


def test_load_hidden_set_assist_states_only_explicit_expose_stays_visible(
    tmp_path, monkeypatch
):
    # A states-only entity (no registry entry) that the user explicitly exposed to
    # the conversation assistant appears in expose_entity/list (True) and must stay
    # visible — the True-only list is the only exposure source for non-registry ids.
    # A sibling states-only entity in a non-default domain with no explicit expose
    # falls to its default (not exposed) and is hidden, so the assertion pins that
    # the list-True is what saves the exposed one.
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True, exclude_categories=[], respect_assist_exposure=True
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    reg = {"success": True, "result": []}  # neither entity has a registry entry
    states = [
        {"entity_id": "sensor.exposed", "attributes": {}},
        {"entity_id": "sensor.plain", "attributes": {}},
    ]
    client = _FakeExposeSeamClient(
        exposed={"sensor.exposed": {"conversation": True}}, expose_new=True
    )
    hidden, _ = asyncio.run(resolver.load_hidden_set(reg, states, client))
    # sensor.exposed: explicit True from the list -> visible. sensor.plain: sensor
    # with no default-exposed device_class -> not default-exposed -> hidden.
    assert hidden == {"sensor.plain"}
