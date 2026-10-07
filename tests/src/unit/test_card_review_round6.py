"""Regressions reported by Patch76 and reproduced on embedded HA 2026.10."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from .test_card_definitions import _definitions, cc, cd


def test_open_object_struct_keeps_editor_threshold_fields():
    import quickjs

    engine = quickjs.Function("engine", cd._ENGINE_JS)
    engine(
        "struct",
        {
            "src": "return {schema: {severity: {type: 'object'}}}",
            "bindings": {},
            "key": "gauge",
        },
    )
    types = engine("field_types", {"key": "gauge"})["value"]
    fields = [
        {
            "name": "severity",
            "schema": [{"name": "green"}, {"name": "yellow"}, {"name": "red"}],
        }
    ]
    assert cd._stored_fields(fields, types) == fields


def test_alarm_form_can_describe_options_without_selected_states():
    import quickjs

    engine = quickjs.Function("engine", cd._ENGINE_JS)
    result = engine(
        "schema",
        {
            "src": "return (localize, stateObj, states) => [{name: 'states', selector: {select: {options: ['armed_home'].map(s => ({value: s, disabled: !states.includes(s)}))}}}]",
            "bindings": {},
        },
    )
    assert (
        result["value"][0]["selector"]["select"]["options"][0]["value"] == "armed_home"
    )


def test_form_defaults_are_not_overridden_by_empty_selected_options():
    import quickjs

    engine = quickjs.Function("engine", cd._ENGINE_JS)
    result = engine(
        "schema",
        {
            "src": "return (localize, entity, icon = 'mdi:link') => [{name: 'icon', selector: {icon: {placeholder: icon}}}]",
            "bindings": {},
        },
    )
    assert result["value"][0]["selector"]["icon"]["placeholder"] == "mdi:link"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["runtime", "index"])
async def test_failed_initialization_retries_after_cooldown(monkeypatch, failure):
    definitions = object()
    runtime = AsyncMock(
        side_effect=[False, True] if failure == "runtime" else [True, True]
    )
    hass = MagicMock()
    hass.async_create_background_task = lambda coro, name: asyncio.create_task(coro)
    hass.async_add_executor_job = AsyncMock(
        side_effect=[None, definitions] if failure == "index" else [definitions]
    )
    monkeypatch.setattr(cd, "_definitions", None)
    monkeypatch.setattr(cd, "_build_task", None)
    monkeypatch.setattr(cd, "_build_failed_at", None)
    monkeypatch.setattr(cd, "async_ensure_runtime", runtime)
    assert await cd.async_get_definitions(hass) is None
    assert await cd.async_get_definitions(hass) is None
    runtime.assert_awaited_once()
    monkeypatch.setattr(
        cd, "_build_failed_at", cd.time.monotonic() - cd._BUILD_RETRY_SECONDS - 1
    )
    assert await asyncio.gather(
        cd.async_get_definitions(hass), cd.async_get_definitions(hass)
    ) == [definitions, definitions]
    assert runtime.await_count == 2
    assert await cd.async_get_definitions(hass) is definitions


def test_missing_builtin_form_has_no_field_coverage_claim():
    definitions = _definitions({"alert"}, {"alert": None})
    definitions._strings = {}
    definitions._editor_body = MagicMock(return_value="")
    result = definitions.describe("alert")
    assert result["fields"] is None
    assert "field_coverage" not in result and "note" not in result


@pytest.fixture
def broken_editor_bundle():
    dom = """
      globalThis.__linkedom = {
        HTMLElement: class HTMLElement {},
        parseHTML: function () { return {
          document: {}, customElements: {define: function () {}}
        }; }
      };
    """
    source = """
      class Card extends HTMLElement {
        setConfig(c) {if (!c.entity) throw new Error('needs an entity');}
        static getConfigElement() {throw new Error('editor unavailable');}
      }
      customElements.define('test-card', Card);
    """
    bundle = cc._Bundle(dom, source)
    yield bundle
    bundle.close()


@pytest.mark.parametrize("failure", ["throws", "times_out"])
def test_editor_failure_does_not_hide_card_verdict(
    broken_editor_bundle, monkeypatch, failure
):
    bundle = broken_editor_bundle
    if failure == "times_out":
        monkeypatch.setattr(
            bundle, "_prepare_tag", MagicMock(side_effect=TimeoutError("editor budget"))
        )
    expected = [{"source": "card", "message": "needs an entity"}]
    assert bundle.check("test-card", {}) == expected
    assert bundle.check("test-card", {}) == expected
    assert bundle.check("test-card", {"entity": "light.test"}) == []


def test_missing_custom_form_has_no_field_coverage_claim(broken_editor_bundle):
    custom = cc.CustomCards("unused")
    custom._owner = MagicMock(return_value=broken_editor_bundle)
    result = custom.describe("test-card")
    assert result["fields"] is None
    assert "field_coverage" not in result and "note" not in result


def test_looping_editor_cannot_consume_later_cards_budget():
    dom = "globalThis.__linkedom = {HTMLElement: class {}, parseHTML: () => ({document: {}, customElements: {define() {}}})};"
    source = """
      class Card extends HTMLElement {
        setConfig() {throw new Error('card verdict');}
        static getConfigElement() {return {setConfig() {while (true) {}}};}
      }
      customElements.define('looping-editor-card', Card);
    """
    bundle = cc._Bundle(dom, source)
    try:
        started = time.monotonic()
        assert bundle.check("looping-editor-card", {}) == [
            {"source": "card", "message": "card verdict"}
        ]
        assert time.monotonic() - started < 2
        assert bundle.check("looping-editor-card", {}) == [
            {"source": "card", "message": "card verdict"}
        ]
    finally:
        bundle.close()


def test_one_editor_cannot_reserve_the_entire_custom_save_budget(monkeypatch):
    bundle = object.__new__(cc._Bundle)
    bundle._prepared = set()
    bundle._context_call = MagicMock()
    bundle.engine = MagicMock(return_value={"value": True})
    bundle._settle = MagicMock()
    bundle._prepare_tag("slow-editor")
    first_limit = bundle._context_call.call_args_list[0].args[1]
    assert first_limit < cd._CUSTOM_WAIT_SECONDS / 2


@pytest.mark.parametrize(
    "card_type,key,other",
    [
        ("area", "show_name", "show_camera"),
        ("todo-list", "tap_action", "item_tap_action"),
    ],
)
def test_advice_does_not_rename_options_with_different_semantics(card_type, key, other):
    definitions = _definitions({card_type}, {card_type: [other]})
    warning = definitions._explain(card_type, {"path": [key], "type": "never"})
    assert warning == f"'{key}' is not listed in the {card_type} editor schema"
