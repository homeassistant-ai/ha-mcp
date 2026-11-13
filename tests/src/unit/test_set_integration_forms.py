"""ha_set_integration without an answer: setup, options and subentry forms (#2632).

Every flow below is Core's: the server's client (``core_client``) drives the
config, options and subentry flows of mock integrations on a real ``hass``, and
Home Assistant's own ``frontend/get_translations`` reads their strings.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryDataWithId,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import config_entry_flow_progress
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._tool_errors import rejected_call_body

_DOMAIN = "flow_forms"
_MENU_DOMAIN = "flow_menu"
_PROGRESS_DOMAIN = "flow_progress"

_STRINGS = {
    _DOMAIN: {
        "config": {"step": {"user": {"data_description": {"host": "Device IP."}}}},
        "options": {"step": {"advanced": {"data": {"host": "Host"}}}},
    },
    _PROGRESS_DOMAIN: {"config": {"progress": {"scanning": "Scanning the network."}}},
}
_HOST = probatio.Schema({probatio.Required("host"): str})
_MENU = ["basic", "advanced"]


class _OptionsMenuFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=_MENU)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="advanced", data_schema=_HOST)


class _ConversationFlow(ConfigSubentryFlow):
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_show_menu(step_id="reconfigure", menu_options=_MENU)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_show_form(step_id="advanced", data_schema=_HOST)


class _FormsFlow(ConfigFlow):
    """A setup form with more forms after it, an options menu and subentries."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="user", data_schema=_HOST, last_step=False)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _OptionsMenuFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"conversation": _ConversationFlow}


class _MenuFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=_MENU)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="advanced", data_schema=_HOST)


class _NoOptionsFlow(ConfigFlow):
    VERSION = 1


class _BrokenFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        raise RuntimeError("the integration's flow has a bug")


@pytest.fixture
def integrations(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """The mock integrations, with their translations, and their flows."""
    # Core reads a mocked integration's translations from its relative
    # package path, so the integration's files live under the test's own cwd.
    monkeypatch.chdir(tmp_path)
    for domain, strings in _STRINGS.items():
        folder = tmp_path / "homeassistant" / "components" / domain / "translations"
        folder.mkdir(parents=True)
        (folder / "en.json").write_text(json.dumps(strings), encoding="utf-8")
    for domain in (_DOMAIN, _MENU_DOMAIN, _PROGRESS_DOMAIN, "flow_bare", "flow_bug"):
        mock_integration(
            hass,
            MockModule(domain),
            top_level_files={"translations"} if domain in _STRINGS else set(),
        )
        mock_platform(hass, f"{domain}.config_flow", None)
    with (
        mock_config_flow(_DOMAIN, _FormsFlow),
        mock_config_flow(_MENU_DOMAIN, _MenuFlow),
        mock_config_flow("flow_bare", _NoOptionsFlow),
        mock_config_flow("flow_bug", _BrokenFlow),
    ):
        yield


@pytest.fixture
async def tools(
    hass: HomeAssistant, core_client: HomeAssistantClient, integrations: None
) -> IntegrationTools:
    """The tools on a ``hass`` that serves the flow endpoints and strings."""
    assert await async_setup_component(hass, "config", {})
    websocket_api.async_register_command(hass, websocket_get_translations)
    return IntegrationTools(core_client)


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry of the integration, with one ``conversation`` subentry."""
    entry = MockConfigEntry(
        domain=_DOMAIN,
        subentries_data=[
            ConfigSubentryDataWithId(
                data={},
                subentry_id="sub1",
                subentry_type="conversation",
                title="Chat",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


_FLOW_METHODS = (
    "start_config_flow",
    "submit_config_flow_step",
    "abort_config_flow",
    "start_options_flow",
    "submit_options_flow_step",
    "abort_options_flow",
    "start_config_subentry_flow",
    "submit_config_subentry_flow_step",
    "abort_config_subentry_flow",
)


@contextmanager
def flow_calls(client: HomeAssistantClient) -> Iterator[dict[str, AsyncMock]]:
    """Spy on the client's flow calls; Core still answers each one."""
    with ExitStack() as stack:
        yield {
            name: stack.enter_context(
                patch.object(client, name, wraps=getattr(client, name))
            )
            for name in _FLOW_METHODS
        }


async def test_set_integration_returns_the_setup_form(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: IntegrationTools
) -> None:
    with flow_calls(core_client) as calls:
        resp = await tools.ha_set_integration(domain="Flow_Forms ")

    calls["start_config_flow"].assert_awaited_once_with(_DOMAIN)
    # Nothing is submitted; the flow stays open and flow_id continues it.
    calls["submit_config_flow_step"].assert_not_called()
    calls["abort_config_flow"].assert_not_called()
    assert [flow["flow_id"] for flow in hass.config_entries.flow.async_progress()] == [
        resp["flow_id"]
    ]
    assert resp["step_id"] == "user"
    assert resp["fields"][0]["name"] == "host"
    assert resp["fields"][0]["description"] == "Device IP."
    assert resp["more_forms"] == "yes"


_SUBENTRY_RULE = "subentry_type and subentry_id apply only with entry_id"


@pytest.mark.parametrize(
    ("call", "says"),
    [
        ({"domain": _DOMAIN, "entry_id": "e1"}, "not both"),
        ({"domain": _DOMAIN, "subentry_type": "conversation"}, _SUBENTRY_RULE),
        (
            {"entry_id": "e1", "subentry_type": "conversation", "flow_id": "f1"},
            "drop flow_id",
        ),
        (
            {"entry_id": "e1", "subentry_type": "conversation", "config": {"h": 1}},
            "drop config",
        ),
    ],
)
async def test_set_integration_subentry_form_takes_menu_choice_only(
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    call: dict[str, Any],
    says: str,
) -> None:
    with flow_calls(core_client) as calls:
        body = await rejected_call_body(tools.ha_set_integration(**call))

    error = body["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert says in error["message"]
    calls["start_config_flow"].assert_not_called()
    calls["start_options_flow"].assert_not_called()
    calls["start_config_subentry_flow"].assert_not_called()


async def test_set_integration_describes_the_options_form_behind_a_menu(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    entry: MockConfigEntry,
) -> None:
    with flow_calls(core_client) as calls:
        resp = await tools.ha_set_integration(
            entry_id=entry.entry_id, menu_choice="advanced"
        )

    calls["submit_options_flow_step"].assert_awaited_once_with(
        resp["flow_id"], {"next_step_id": "advanced"}
    )
    # The flow stays open at the form behind the menu.
    calls["abort_options_flow"].assert_not_called()
    assert [
        flow["flow_id"] for flow in hass.config_entries.options.async_progress()
    ] == [resp["flow_id"]]
    assert resp["step_id"] == "advanced"
    assert [(f["name"], f["description"]) for f in resp["fields"]] == [("host", "Host")]
    assert "entry" not in resp


async def test_set_integration_describes_the_subentry_form_behind_a_menu(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    entry: MockConfigEntry,
) -> None:
    with flow_calls(core_client) as calls:
        resp = await tools.ha_set_integration(
            entry_id=entry.entry_id,
            subentry_type="conversation",
            subentry_id="sub1",
            menu_choice="advanced",
        )

    calls["start_config_subentry_flow"].assert_awaited_once_with(
        entry.entry_id, "conversation", subentry_id="sub1", show_advanced_options=False
    )
    submitted = calls["submit_config_subentry_flow_step"]
    submitted.assert_awaited_once()
    assert submitted.await_args is not None
    assert submitted.await_args.args[1] == {"next_step_id": "advanced"}
    calls["abort_config_subentry_flow"].assert_awaited_once_with(
        submitted.await_args.args[0]
    )
    assert hass.config_entries.subentries.async_progress() == []
    assert resp["subentry_schema"]["menu_choice"] == "advanced"
    calls["start_options_flow"].assert_not_called()


async def test_set_integration_options_form_refuses_an_ignored_entry(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: IntegrationTools
) -> None:
    ignored = MockConfigEntry(domain=_DOMAIN, source="ignore")
    ignored.add_to_hass(hass)

    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            tools.ha_set_integration(entry_id=ignored.entry_id)
        )

    assert "ignored" in body["error"]["message"]
    calls["start_options_flow"].assert_not_called()


async def test_set_integration_options_form_fails_when_the_flow_does(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    """An entry whose integration has no options flow: Core refuses to start one."""
    bare = MockConfigEntry(domain="flow_bare")
    bare.add_to_hass(hass)

    await rejected_call_body(tools.ha_set_integration(entry_id=bare.entry_id))


def test_ha_get_integration_takes_no_menu_choice() -> None:
    params = inspect.signature(IntegrationTools.ha_get_integration).parameters
    assert "menu_choice" not in params


async def test_set_integration_of_a_domain_without_a_flow_names_cores_reason(
    tools: IntegrationTools,
) -> None:
    """Core answers 404 Invalid handler for a domain it has no flow for."""
    error = (
        await rejected_call_body(tools.ha_set_integration(domain="no_such_domain"))
    )["error"]

    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert "no_such_domain" in error["message"]
    assert "Invalid handler" in error["message"]
    assert "Home Assistant log" not in json.dumps(error)


async def test_set_integration_whose_dependency_fails_points_at_the_log(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    """Core answers 400 for failed dependencies: the domain is installed."""
    mock_integration(hass, MockModule("flow_dep_broken", async_setup=_refuse_setup))
    mock_integration(
        hass, MockModule("flow_with_dep", dependencies=["flow_dep_broken"])
    )
    # No config_flow platform is loaded yet, so Core loads the integration
    # and sets up its dependencies before it starts the flow.
    with mock_config_flow("flow_with_dep", _FormsFlow):
        error = (
            await rejected_call_body(tools.ha_set_integration(domain="flow_with_dep"))
        )["error"]

    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "flow_with_dep" in error["message"]
    assert "flow_dep_broken" in error["message"]
    assert "Home Assistant log" in json.dumps(error)


async def _refuse_setup(hass: HomeAssistant, config: Any) -> bool:
    return False


async def test_set_integration_whose_flow_errors_is_not_called_unstartable(
    tools: IntegrationTools,
) -> None:
    """Only a 400 or 404 is about the flow start; a 5xx is the tool's own error."""
    error = (await rejected_call_body(tools.ha_set_integration(domain="flow_bug")))[
        "error"
    ]

    assert "could not start a config flow" not in error["message"]
    assert "spelled correctly" in error["suggestion"]


async def test_set_integration_lists_a_setup_menu_then_answers_it(
    core_client: HomeAssistantClient, tools: IntegrationTools
) -> None:
    with flow_calls(core_client) as calls:
        listed = await tools.ha_set_integration(domain=_MENU_DOMAIN)

    assert listed["flow_type"] == "menu"
    assert listed["menu_options"] == _MENU
    calls["submit_config_flow_step"].assert_not_called()

    with flow_calls(core_client) as calls:
        chosen = await tools.ha_set_integration(
            domain=_MENU_DOMAIN, menu_choice="advanced"
        )

    calls["submit_config_flow_step"].assert_awaited_once_with(
        chosen["flow_id"], {"next_step_id": "advanced"}
    )
    assert chosen["step_id"] == "advanced"
    assert chosen["fields"][0]["name"] == "host"


async def test_set_integration_of_a_setup_flow_that_aborts_gives_cores_text(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    MockConfigEntry(domain="sun").add_to_hass(hass)

    body = await rejected_call_body(tools.ha_set_integration(domain="sun"))

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    # Core aborts a second entry of a single-entry integration itself, in its
    # own words.
    strings = await async_get_translations(hass, "en", "config", ["homeassistant"])
    key = "component.homeassistant.config.abort.single_instance_allowed"
    assert body["reason_text"] == strings[key]


@pytest.fixture
def scanning(integrations: None) -> Iterator[asyncio.Event]:
    """A setup flow that opens on a progress step; the event ends its task."""
    done = asyncio.Event()

    class _ProgressFlow(ConfigFlow):
        VERSION = 1
        task: asyncio.Task[None] | None = None

        async def async_step_user(
            self, user_input: dict[str, Any] | None = None
        ) -> ConfigFlowResult:
            if self.task is None:
                self.task = self.hass.async_create_task(_wait(done))
            return self.async_show_progress(
                step_id="user", progress_action="scanning", progress_task=self.task
            )

    with mock_config_flow(_PROGRESS_DOMAIN, _ProgressFlow):
        yield done


async def _wait(done: asyncio.Event) -> None:
    await done.wait()


@pytest.fixture
async def progress_flow(
    hass: HomeAssistant, scanning: asyncio.Event
) -> AsyncIterator[None]:
    try:
        yield
    finally:
        scanning.set()
        for flow in hass.config_entries.flow.async_progress():
            hass.config_entries.flow.async_abort(flow["flow_id"])
        await hass.async_block_till_done()


async def test_set_integration_of_a_progress_step_keeps_the_flow_open(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    progress_flow: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_entry_flow_progress, "PROGRESS_BUDGET_S", 0.0)

    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            tools.ha_set_integration(domain=_PROGRESS_DOMAIN)
        )

    assert body["error"]["code"] == "TIMEOUT_OPERATION"
    assert [flow["flow_id"] for flow in hass.config_entries.flow.async_progress()] == [
        body["flow_id"]
    ]
    assert body["progress_action"] == "scanning"
    assert body["progress_action_text"] == "Scanning the network."
    calls["abort_config_flow"].assert_not_called()


async def test_set_integration_warns_when_the_strings_cannot_be_read(
    hass: HomeAssistant, core_client: HomeAssistantClient, integrations: None
) -> None:
    """Without ``frontend/get_translations`` Core answers unknown_command."""
    assert await async_setup_component(hass, "config", {})

    resp = await IntegrationTools(core_client).ha_set_integration(domain=_DOMAIN)

    assert resp["fields"][0]["name"] == "host"
    assert any(
        f"strings for {_DOMAIN} could not be read" in w for w in resp["warnings"]
    )
