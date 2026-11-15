"""Human-review regressions of the dashboard card checks, on real Home Assistant Core.

The card index runs on a small frontend build written to disk, custom cards in
their real QuickJS sandbox, and the describe tool and the ``dashboard_cards``
command on a real ``hass`` (the E2E suite runs the real frontend and DOM). The
optional QuickJS runtime is read from real ``importlib.metadata`` distributions
in ``.dist-info`` folders and a Core package directory holding the constraints,
and the unit environment installs the QuickJS build the component accepts.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from importlib import metadata
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import homeassistant
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from packaging.utils import canonicalize_name
from pytest_homeassistant.typing import WebSocketGenerator

from custom_components.ha_mcp_tools import card_runtime as runtime
from custom_components.ha_mcp_tools import websocket_api
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import invalidate_caps
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools

from ._card_definitions_core import (
    FrontendOnDisk,
    InstallCustomCard,
    add_card_names,
    build_definitions,
    card_file,
    card_file_with_editor,
    cc,
    cd,
    describe_mod,
    write_frontend,
)
from ._card_definitions_core import custom_cards as custom_cards
from ._card_definitions_core import frontend as frontend
from ._card_definitions_core import idle_custom_cards as idle_custom_cards
from ._card_definitions_core import installed_custom_card as installed_custom_card
from ._card_definitions_core import stalled_card_download as stalled_card_download
from ._component_core import ws_result
from ._core_client import refused_url

_PER_CARD = 16 * 1024 * 1024  # one sandbox's heap allowance in these tests
_ADVISORY = "editor schema advisory; runtime support may differ"
# A custom card's editor that rejects every option the way the frontend's
# superstruct does.
_REJECT_EVERY_OPTION = (
    "for (const key of Object.keys(c)) { if (key !== 'type') throw new Error("
    "'At path: ' + key + ' -- Expected a value of type `never`, but received: `1`'); }"
)


def _views(*cards: dict[str, Any]) -> dict[str, Any]:
    return {"views": [{"cards": list(cards)}]}


async def _describe(
    frontend: FrontendOnDisk,
    client: HomeAssistantClient,
    editor: str,
    card_type: str = "tile",
) -> dict[str, Any]:
    """What the describe tool says of ``card_type`` once its editor is ``editor``."""
    frontend.write({"tile": editor})
    return await describe_mod.describe_card_response(client, card_type)


_PROVIDERS = ("quickjs", "quickjs-ng")


def _distribution(
    root: Path, name: str, version: str, requires: Sequence[str]
) -> metadata.Distribution:
    folder = root / f"{name.replace('-', '_')}-{version}.dist-info"
    folder.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    (folder / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return metadata.Distribution.at(folder)


@contextmanager
def installed_packages(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    packages: Mapping[str, str],
    *,
    requires: Sequence[str] = (),
) -> Iterator[dict[str, str]]:
    """Only ``packages`` (name: version) provide QuickJS, and the install holds
    one more package that ``requires`` these requirements. The yielded dict is
    the providers installed now: a test adds one to install it."""
    dists = [
        _distribution(root, name, version, ()) for name, version in packages.items()
    ]
    dists.append(_distribution(root, "another-package", "1.0", requires))
    versions: dict[str, str] = {
        str(canonicalize_name(name)): version for name, version in packages.items()
    }
    real_version, real_find_spec = metadata.version, importlib.util.find_spec

    def version(name: str) -> str:
        key = canonicalize_name(name)
        if key in versions:
            return versions[key]
        if key in _PROVIDERS:
            raise metadata.PackageNotFoundError(name)
        return real_version(name)

    def find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
        if name == "quickjs":
            return ModuleSpec(name, None) if set(_PROVIDERS) & set(versions) else None
        return real_find_spec(name, package)

    with monkeypatch.context() as patch:
        patch.setattr(metadata, "version", version)
        patch.setattr(metadata, "distributions", lambda: dists)
        patch.setattr(importlib.util, "find_spec", find_spec)
        yield versions


def constrained_core(
    monkeypatch: pytest.MonkeyPatch, root: Path, constraints: str
) -> None:
    """Home Assistant's package constraints are ``constraints``."""
    (root / "package_constraints.txt").write_text(constraints, encoding="utf-8")
    monkeypatch.setattr(homeassistant, "__file__", str(root / "__init__.py"))


# --- custom cards ----------------------------------------------------------


@pytest.mark.usefixtures("idle_custom_cards")
def test_a_custom_editors_rejection_is_advisory_and_card_mod_is_accepted(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    """The editor's struct says what the editor lists, not what the card
    accepts: the warning must not claim the option is not a card option."""
    definitions = build_definitions(tmp_path)
    tag = "mushroom-entity-card"
    custom_cards.refresh(
        [
            card_file_with_editor(
                tmp_path / "mushroom.js", tag, editor_set_config=_REJECT_EVERY_OPTION
            )
        ]
    )
    config = _views(
        {"type": f"custom:{tag}", "disabled": True},
        {"type": f"custom:{tag}", "card_mod": {"style": "x"}},
    )

    warnings = definitions.validate(config, custom_cards)

    assert warnings == [
        f"views[0].cards[0] (custom:{tag}): {_ADVISORY}: "
        f"'disabled' is not listed in the {tag} editor schema"
    ]


def test_a_cards_own_error_hides_the_editors_copy_of_it(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    """An editor message the card did not give is kept as the editor's; one
    both give is reported once, as the card's."""
    custom_cards.refresh(
        [
            card_file_with_editor(
                tmp_path / "x-card.js",
                "x-card",
                set_config="if (c.reject) throw new Error('shared error');",
                editor_set_config=(
                    "throw new Error(c.reject ? 'shared error' : 'editor only');"
                ),
            )
        ]
    )

    assert custom_cards.check("x-card", {}) == [
        {"source": "editor", "message": "editor only"}
    ]
    assert custom_cards.check("x-card", {"reject": True}) == [
        {"source": "card", "message": "shared error"}
    ]


def test_a_resource_skipped_for_memory_is_named_in_the_coverage(
    tmp_path: Path, custom_cards: cc.CustomCards, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc, "_BUNDLE_MEMORY", _PER_CARD)
    monkeypatch.setattr(cc, "_TOTAL_MEMORY", _PER_CARD)  # room for one card
    files = [card_file(tmp_path / f"card-{i}.js", f"card-{i}") for i in range(2)]

    custom_cards.refresh(files)

    assert custom_cards.status() == {
        "state": "partial",
        "resources": [{"resource": str(files[1]), "reason": "memory limit"}],
    }


async def test_a_failed_dom_download_does_not_turn_a_custom_card_into_an_unknown_one(
    hass: HomeAssistant,
    frontend: FrontendOnDisk,
    idle_custom_cards: None,
    hass_ws_client: WebSocketGenerator,
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no resources the inventory is empty, so a custom card is reported as
    not found; once a resource names one the library it needs is missing, and
    the empty inventory says nothing about the card."""
    assert await async_setup_component(hass, "lovelace", {})
    ws = await hass_ws_client(hass)
    monkeypatch.setattr(cc, "LINKEDOM_URL", refused_url())
    config = _views({"type": "custom:installed"})

    [warning] = await cd.async_card_warnings(hass, config)
    assert "not found in dashboard resources" in warning
    await ws_result(
        ws, "lovelace/resources/create", res_type="js", url="/local/installed.js"
    )

    assert await cd.async_card_warnings(hass, config) == []


async def test_a_slow_refresh_still_answers_with_the_published_custom_cards(
    hass: HomeAssistant,
    stalled_card_download: None,
    hass_ws_client: WebSocketGenerator,
) -> None:
    ws = await hass_ws_client(hass)
    [resource] = await ws_result(ws, "lovelace/resources")
    await ws_result(ws, "lovelace/resources/delete", resource_id=resource["id"])
    published = await cc.async_get_custom_cards(hass, timeout=10)
    assert published is not None
    await ws_result(
        ws, "lovelace/resources/create", res_type="module", url="/local/card.js"
    )

    assert await cc.async_get_custom_cards(hass, timeout=0.05) is published
    assert cc.custom_load_status(published)["state"] == "loading"


# --- the index ---------------------------------------------------------------


def test_a_lazy_config_form_module_supplies_the_schema_and_the_assertion(
    tmp_path: Path,
) -> None:
    """A card whose ``getConfigForm`` loads a lazy module has its fields from
    that module's form, and its options checked by that module's assertion."""
    modules = (
        '40(m,e,r){(0,e.EM)("hui-entity-card");class A{setConfig(c){}'
        "static async getConfigForm(){return(await r.e(2).then(r.bind(r,41))).default}}},"
        "41(m,e,r){var t=r(6),v=(0,t.object)({type:t.string,entity:t.string});"
        'const s=[{name:"entity",selector:{entity:{}}}],'
        "form={schema:s,assertConfig:c=>(0,t.assert)(c,v)}}"
    )
    definitions = build_definitions(tmp_path, modules=modules)

    described = definitions.describe("entity")
    warnings = definitions.validate(_views({"type": "entity", "colour": "red"}))

    assert described is not None
    assert described["field_coverage"] == "partial"
    assert described["fields"] == [{"name": "entity", "selector": {"entity": {}}}]
    assert warnings == [
        f"views[0].cards[0] (entity): {_ADVISORY}: "
        "'colour' is not listed in the entity editor schema"
    ]


def test_an_export_of_a_module_that_cannot_run_is_read_without_running_it(
    tmp_path: Path,
) -> None:
    """The struct comes from ``refine`` in a module that needs a browser to run
    (it throws here), found through the export's own definition."""
    modules = (
        "3(m,e,r){var s=r(4);const refine=(x)=>s.wrap(x);"
        "r.d(e,{},{f:refine});window.startBrowser()},"
        "4(m,e,r){r.d(e,{},{wrap:(x)=>({schema:{entity:x}})})}"
    )
    editor = (
        "var d=r(3),t=r(6),v=d.f(t.string);class C{setConfig(c){(0,t.assert)(c,v)}}"
    )
    definitions = build_definitions(tmp_path, {"tile": editor}, modules=modules)

    described = definitions.describe("tile")

    assert described is not None
    assert described["fields"] == [{"name": "entity", "type": "string"}]


def test_the_index_keeps_inherited_cards_but_excludes_dialogs(tmp_path: Path) -> None:
    """A card with no translated name is a card when it implements, or inherits,
    ``setConfig``; a dialog registered under a ``hui-*-card`` tag is not one."""
    modules = (
        '40(m,e,r){(0,e.EM)("hui-horizontal-stack-card");'
        "class Dialog{showDialog(t){}}},"
        '41(m,e,r){(0,e.EM)("hui-dialog-edit-card");class Dialog{showDialog(t){}}},'
        '42(m,e,r){(0,e.EM)("hui-energy-internal-card");'
        "class Card{setConfig(t){this.config=t}}},"
        '43(m,e,r){(0,e.EM)("hui-shopping-list-card");var c=r(123);'
        "class Alias extends c.Card{}},"
        "123(m,e,r){class Card{setConfig(t){this.config=t}}}"
    )
    write_frontend(tmp_path, modules=modules)
    add_card_names(tmp_path, {"horizontal-stack": "Horizontal stack"})
    definitions = cd.CardDefinitions(tmp_path)
    cards = [
        {"type": "horizontal-stack"},
        {"type": "dialog-edit"},
        {"type": "energy-internal"},
        {"type": "shopping-list"},
    ]

    assert definitions.validate(_views(*cards)) == [
        "views[0].cards[1]: unknown card type 'dialog-edit'"
    ]


async def test_a_frontend_with_no_cards_is_unavailable_not_an_empty_list(
    frontend: FrontendOnDisk, component_core_client: HomeAssistantClient
) -> None:
    frontend.write(files={"cards.js": "export const __webpack_modules__={};"})

    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(component_core_client, "tile")

    assert "card definitions are unavailable" in str(err.value)


# --- the editor form as the tool describes it ---------------------------------


async def test_a_malformed_select_does_not_abort_the_description(
    frontend: FrontendOnDisk, component_core_client: HomeAssistantClient
) -> None:
    editor = (
        "class E{constructor(){this._schema=(0,m.A)(e=>["
        '{name:"classes",selector:{select:{options:!1}}}])}}'
    )

    described = await _describe(frontend, component_core_client, editor)

    assert described["fields"] == [{"name": "classes", "type": "select"}]


async def test_a_named_grid_stays_a_section_and_a_flattened_one_is_inlined(
    frontend: FrontendOnDisk, component_core_client: HomeAssistantClient
) -> None:
    editor = (
        "class E{constructor(){this._schema=(0,m.A)(e=>["
        '{name:"content",type:"expandable",flatten:!0,schema:['
        '{name:"entity",selector:{entity:{}}}]},'
        '{name:"limits",type:"grid",schema:['
        '{name:"min",selector:{number:{}}},{name:"max",selector:{number:{}}}]}])}}'
    )

    described = await _describe(frontend, component_core_client, editor)

    assert described["fields"] == [
        {"name": "entity", "type": "entity"},
        {
            "name": "limits",
            "type": "section",
            "fields": [
                {"name": "min", "type": "number"},
                {"name": "max", "type": "number"},
            ],
        },
    ]


async def test_editor_only_fields_and_types_follow_the_stored_struct(
    frontend: FrontendOnDisk, component_core_client: HomeAssistantClient
) -> None:
    """``content_layout`` is not stored, and ``detail`` is a number there although
    the editor shows a switch: describing it as the editor shows it would have
    the caller write a config the card rejects."""
    editor = (
        "var s=r(6),v=(0,s.object)({type:s.string,vertical:{type:'boolean'},"
        "detail:{type:'number'},entity:s.string});"
        "class C{constructor(){this._schema=(0,s.memo)(e=>["
        '{name:"content",type:"expandable",flatten:!0,schema:['
        '{name:"content_layout",selector:{select:{options:["vertical"]}}},'
        '{name:"detail",selector:{boolean:{}},default:!0},'
        '{name:"entity",selector:{entity:{}}}]}])}'
        "setConfig(c){(0,s.assert)(c,v)}}"
    )

    described = await _describe(frontend, component_core_client, editor)

    assert described["fields"] == [
        {"name": "detail", "type": "number"},
        {"name": "entity", "type": "entity"},
        {"name": "vertical", "type": "boolean"},
    ]


async def test_stored_options_the_form_lacks_are_added_once(
    frontend: FrontendOnDisk, component_core_client: HomeAssistantClient
) -> None:
    editor = (
        "var s=r(6),v=(0,s.object)({type:s.string,entity:s.string,"
        "features:{type:'array'},vertical:{type:'boolean'}});"
        "class C{constructor(){this._schema=(0,s.memo)(e=>["
        '{name:"content",type:"expandable",flatten:!0,schema:['
        '{name:"entity",selector:{entity:{}}}]}])}'
        "setConfig(c){(0,s.assert)(c,v)}}"
    )

    described = await _describe(frontend, component_core_client, editor)

    assert described["fields"] == [
        {"name": "entity", "type": "entity"},
        {"name": "features", "type": "array"},
        {"name": "vertical", "type": "boolean"},
    ]


async def test_a_blank_card_type_lists_the_cards(
    frontend: FrontendOnDisk,
    stalled_card_download: None,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cd, "_CUSTOM_WAIT_SECONDS", 0.05)

    result = await describe_mod.describe_card_response(component_core_client, "")

    assert [card["type"] for card in result["card_types"]] == ["entities", "tile"]


async def test_a_custom_card_without_a_form_is_not_described_as_a_container(
    frontend: FrontendOnDisk,
    installed_custom_card: InstallCustomCard,
    component_core_client: HomeAssistantClient,
) -> None:
    frontend.write(cards=("vertical-stack",))
    await installed_custom_card("example")

    custom = await describe_mod.describe_card_response(
        component_core_client, "custom:example"
    )
    stack = await describe_mod.describe_card_response(
        component_core_client, "vertical-stack"
    )

    assert custom["fields"] is None
    assert "inspect" in custom["note"]
    assert "stack" not in custom["note"]
    assert "nests other cards" in stack["note"]


async def test_describing_a_builtin_card_does_not_load_the_custom_resources(
    frontend: FrontendOnDisk,
    stalled_card_download: None,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cd, "_CUSTOM_WAIT_SECONDS", 0.05)

    described = await describe_mod.describe_card_response(component_core_client, "tile")
    assert described["name"] == "Tile"
    assert cc.custom_load_status(None)["state"] == "unavailable"

    await describe_mod.describe_card_response(component_core_client, None)
    assert cc.custom_load_status(None)["state"] == "loading"


async def test_the_static_card_guide_comes_with_a_write_only_without_the_component(
    hass: HomeAssistant,
    frontend: FrontendOnDisk,
    core_client: HomeAssistantClient,
) -> None:
    """Without ``dashboard_cards`` a caller cannot describe cards, so the write
    carries the static guide; with it the caller has the describe tool."""
    assert await async_setup_component(hass, "lovelace", {})
    tools = DashboardConfigTools(core_client)
    guide = "references/dashboard-cards.md"

    without = await tools.ha_config_set_dashboard(
        url_path="card-guide-a", title="A", config={"views": []}
    )
    websocket_api.async_register_commands(hass)
    invalidate_caps(core_client)
    with_component = await tools.ha_config_set_dashboard(
        url_path="card-guide-b", title="B", config={"views": []}
    )

    assert guide in without["skill_content"]
    assert guide not in with_component["skill_content"]


# --- validating a saved dashboard --------------------------------------------


def test_a_custom_cards_child_is_not_checked_against_the_editor_schema(
    tmp_path: Path,
) -> None:
    """A wrapper's child is a template the wrapper fills in, so its options are
    not a final card's; the same card outside the wrapper is checked."""
    definitions = build_definitions(tmp_path)
    child = {"type": "tile", "entity": '${"light.test"}', "vertical": "${true}"}

    wrapped = definitions.validate(
        _views({"type": "custom:config-template-card", "card": child})
    )
    plain = definitions.validate(_views(child))

    assert wrapped == []
    assert plain == [
        f"views[0].cards[0] (tile): {_ADVISORY}: "
        "'vertical' is not listed in the tile editor schema"
    ]


def test_a_partial_custom_child_may_omit_its_type_but_a_typo_is_reported(
    tmp_path: Path,
) -> None:
    definitions = build_definitions(tmp_path)
    config = _views(
        {"type": "custom:auto-entities", "card": {"title": "Lights on"}},
        {"type": "custom:auto-entities", "card": {"type": "tyle"}},
        {"title": "Missing real type"},
    )

    assert definitions.validate(config) == [
        "views[0].cards[1].card: unknown card type 'tyle'",
        "views[0].cards[2]: no card type configured",
    ]


@pytest.mark.parametrize(
    "view", [{"cards": 5}, {"sections": 5}, {"sections": [{"cards": 5}]}]
)
def test_a_malformed_view_does_not_hide_the_other_card_warnings(
    tmp_path: Path, view: dict[str, Any]
) -> None:
    definitions = build_definitions(tmp_path)
    config = {"views": [{"cards": [{"type": "tyle"}]}, view]}

    assert definitions.validate(config) == [
        "views[0].cards[0]: unknown card type 'tyle'"
    ]


# --- the optional QuickJS runtime ---------------------------------------------


def _requirements(monkeypatch: pytest.MonkeyPatch, hass: HomeAssistant) -> AsyncMock:
    """Home Assistant's requirement manager, replaced so that nothing installs."""
    install = AsyncMock()
    monkeypatch.setattr(
        "homeassistant.requirements.async_process_requirements", install
    )
    hass.config.skip_pip = False
    return install


async def test_the_other_quickjs_provider_is_never_replaced(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install = _requirements(monkeypatch, hass)

    with installed_packages(monkeypatch, tmp_path, {"quickjs": "1.19.4"}):
        assert await runtime.async_ensure_runtime(hass) is False

    install.assert_not_awaited()
    assert "already owns the quickjs module" in caplog.text


async def test_an_older_pin_nothing_else_needs_moves_to_the_components(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An updated component finds the quickjs-ng its last version installed;
    refusing it would turn the card advice off for every user who had it."""
    install = _requirements(monkeypatch, hass)

    with installed_packages(
        monkeypatch, tmp_path, {"quickjs-ng": "0.16.2.1"}
    ) as installed:

        async def provide(*args: Any, **kwargs: Any) -> None:
            installed["quickjs-ng"] = runtime.QUICKJS_VERSION

        install.side_effect = provide
        assert await runtime.async_ensure_runtime(hass) is True

    install.assert_awaited_once_with(
        hass,
        "ha_mcp_tools_dashboard_cards",
        [runtime.QUICKJS_REQUIREMENT],
        is_built_in=False,
    )


@pytest.mark.parametrize(
    "installed", [{}, {"quickjs-ng": "0.16.2.1"}], ids=["missing", "older"]
)
async def test_a_package_that_needs_another_provider_keeps_the_runtime_off(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    installed: dict[str, str],
) -> None:
    install = _requirements(monkeypatch, hass)

    with installed_packages(
        monkeypatch, tmp_path, installed, requires=["quickjs-ng==0.16.2.1"]
    ):
        assert await runtime.async_ensure_runtime(hass) is False

    install.assert_not_awaited()
    assert "another installed package" in caplog.text


async def test_home_assistants_constraints_cannot_be_overridden_by_the_runtime(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install = _requirements(monkeypatch, hass)
    constrained_core(monkeypatch, tmp_path, "quickjs-ng==0.0.0\n")

    with installed_packages(monkeypatch, tmp_path / "site", {}):
        assert await runtime.async_ensure_runtime(hass) is False

    install.assert_not_awaited()
    assert "Home Assistant requires" in caplog.text


async def test_a_failed_install_leaves_the_advice_off_and_nothing_else(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _requirements(monkeypatch, hass)
    install.side_effect = RuntimeError("no compatible wheel")

    with installed_packages(monkeypatch, tmp_path, {}):
        assert await runtime.async_ensure_runtime(hass) is False

    install.assert_awaited_once_with(
        hass,
        "ha_mcp_tools_dashboard_cards",
        [runtime.QUICKJS_REQUIREMENT],
        is_built_in=False,
    )


async def test_the_runtime_is_ready_once_home_assistant_has_installed_it(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _requirements(monkeypatch, hass)

    with installed_packages(monkeypatch, tmp_path, {}) as installed:

        async def provide(*args: Any, **kwargs: Any) -> None:
            installed["quickjs-ng"] = runtime.QUICKJS_VERSION

        install.side_effect = provide
        assert await runtime.async_ensure_runtime(hass) is True

    install.assert_awaited_once()


@pytest.mark.parametrize(
    ("installed", "ready"),
    [({}, False), ({"quickjs-ng": runtime.QUICKJS_VERSION}, True)],
    ids=["missing", "compatible"],
)
async def test_skip_pip_never_installs_but_allows_a_compatible_provider(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: dict[str, str],
    ready: bool,
) -> None:
    install = _requirements(monkeypatch, hass)
    hass.config.skip_pip = True

    with installed_packages(monkeypatch, tmp_path, installed):
        assert await runtime.async_ensure_runtime(hass) is ready

    install.assert_not_awaited()


# --- the installed QuickJS ----------------------------------------------


async def test_the_dev_group_installs_the_quickjs_the_component_accepts(
    hass: HomeAssistant,
) -> None:
    """A dev pin that drifts from the component's makes its own runtime check
    refuse the installed build, so the card tests would run a QuickJS no user gets."""
    assert await runtime.async_ensure_runtime(hass)
