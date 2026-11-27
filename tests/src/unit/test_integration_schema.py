"""ha_get_integration's options and subentry flow schemas (#2632).

The tool opens the flow, describes its form (or lists a menu's branches) and
aborts it. Every flow below is a mock integration's, run by Core's flow
managers through the server's own client on a real ``hass``; Home Assistant's
own ``frontend/get_translations`` reads its strings. Branch and setup forms:
test_set_integration_forms.py.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import AsyncMock, patch

import anyio
import pytest
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, mock_config_flow

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.redaction import REDACTED_SET
from ha_mcp.tools import integration_schema
from ha_mcp.tools.tools_integrations import (
    IntegrationTools,
    fetch_entry_options_with_status,
)

from ._command_faults import received
from ._mock_flow_integration import (
    answer_translations,
    mock_flow_integration,
    open_flow_ids,
)
from ._tool_errors import rejected_call_body

_DOMAIN = "schema_demo"
_OTHER = "schema_other"
_STRINGS = {
    "options": {
        "step": {"init": {"data_description": {"host": "Device IP."}}},
        "abort": {"not_supported": "{model} has no options."},
        "progress": {"scanning": "Scanning 3 hubs."},
    },
    "config_subentries": {
        "conversation": {
            "step": {"user": {"data_description": {"host": "Ollama server."}}}
        },
        "device": {"abort": {"not_supported": "{model} has no options."}},
    },
}
_OTHER_STRINGS = {"options": {"abort": {"reauth_required": "Sign in again."}}}
_MENU = ["basic", "advanced"]
_HOST = vol.Schema({vol.Required("host"): TextSelector()})
_SECRET = vol.Schema(
    {
        vol.Required("host"): TextSelector(),
        vol.Required("api_key"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)
_INTROSPECT_LOG = "ha_mcp.tools.config_entry_flow_introspect"

OptionsStep = Callable[[OptionsFlow], ConfigFlowResult]
SubentryStep = Callable[[ConfigSubentryFlow], SubentryFlowResult]


def _options(first: OptionsStep) -> type[OptionsFlow]:
    """An options flow whose first step is ``first``."""

    class _Options(OptionsFlow):
        async def async_step_init(
            self, user_input: dict[str, Any] | None = None
        ) -> ConfigFlowResult:
            return first(self)

    return _Options


def _subentry(
    first: SubentryStep, advanced: SubentryStep | None = None
) -> type[ConfigSubentryFlow]:
    """A subentry flow that opens with ``first``; its menu branch
    ``advanced`` runs ``advanced``."""

    class _Subentry(ConfigSubentryFlow):
        async def async_step_user(
            self, user_input: dict[str, Any] | None = None
        ) -> SubentryFlowResult:
            return first(self)

        async def async_step_advanced(
            self, user_input: dict[str, Any] | None = None
        ) -> SubentryFlowResult:
            assert advanced is not None
            return advanced(self)

    return _Subentry


def _config_flow(
    options: type[OptionsFlow] | None,
    subentries: dict[str, type[ConfigSubentryFlow]],
) -> type[ConfigFlow]:
    class _Flow(ConfigFlow):
        VERSION = 1

        @classmethod
        @callback
        def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
            return options is not None

        @staticmethod
        @callback
        def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
            assert options is not None
            return options()

        @classmethod
        @callback
        def async_get_supported_subentry_types(
            cls, config_entry: ConfigEntry
        ) -> dict[str, type[ConfigSubentryFlow]]:
            return subentries

    return _Flow


def _form(schema: vol.Schema) -> OptionsStep:
    """An options form of ``schema`` showing the entry's stored options."""

    def first(flow: OptionsFlow) -> ConfigFlowResult:
        return flow.async_show_form(
            step_id="init",
            data_schema=flow.add_suggested_values_to_schema(
                schema, flow.config_entry.options
            ),
        )

    return first


def _menu(flow: OptionsFlow) -> ConfigFlowResult:
    return flow.async_show_menu(step_id="init", menu_options=_MENU)


def _subentry_menu(flow: ConfigSubentryFlow) -> SubentryFlowResult:
    return flow.async_show_menu(step_id="user", menu_options=_MENU)


def _host_form(flow: ConfigSubentryFlow) -> SubentryFlowResult:
    return flow.async_show_form(step_id="user", data_schema=_HOST)


def _not_supported(flow: OptionsFlow | ConfigSubentryFlow) -> Any:
    return flow.async_abort(
        reason="not_supported", description_placeholders={"model": "X1"}
    )


def _no_options(flow: OptionsFlow) -> ConfigFlowResult:
    return flow.async_abort(reason="no_options")


def _broken(flow: OptionsFlow | ConfigSubentryFlow) -> Any:
    raise RuntimeError("the integration's flow has a bug")


class Mount(Protocol):
    def __call__(
        self,
        *,
        options: type[OptionsFlow] | None = None,
        subentries: dict[str, type[ConfigSubentryFlow]] | None = None,
        stored: dict[str, Any] | None = None,
    ) -> MockConfigEntry:
        """Set the integration up and return its entry."""


@pytest.fixture
async def mount(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Mount]:
    """Add an entry of the mock integration whose options and subentry flows
    are the given ones; Core serves its config API and log levels."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "logger", {})
    mock_flow_integration(hass, _OTHER, tmp_path, monkeypatch, _OTHER_STRINGS)
    mock_flow_integration(hass, _DOMAIN, tmp_path, monkeypatch, _STRINGS)
    with ExitStack() as stack:

        def add(
            *,
            options: type[OptionsFlow] | None = None,
            subentries: dict[str, type[ConfigSubentryFlow]] | None = None,
            stored: dict[str, Any] | None = None,
        ) -> MockConfigEntry:
            stack.enter_context(
                mock_config_flow(_DOMAIN, _config_flow(options, subentries or {}))
            )
            entry = MockConfigEntry(domain=_DOMAIN, options=stored or {})
            entry.add_to_hass(hass)
            return entry

        yield add


@pytest.fixture
def tools(
    hass: HomeAssistant, core_client: HomeAssistantClient, mount: Mount
) -> IntegrationTools:
    """The tools without the component; Core serves the flow strings."""
    answer_translations(hass)
    return IntegrationTools(core_client)


@pytest.fixture
def component_tools(
    component_core_client: HomeAssistantClient, tools: IntegrationTools
) -> IntegrationTools:
    """The same tools on a ``hass`` with the component, which serves the
    entry's stored options."""
    return tools


@pytest.fixture
def unreadable_strings(
    core_client: HomeAssistantClient, mount: Mount
) -> IntegrationTools:
    """The tools on a ``hass`` without ``frontend/get_translations``: Core
    answers it with ``unknown_command``."""
    return IntegrationTools(core_client)


@contextmanager
def _unanswered(client: HomeAssistantClient, name: str) -> Iterator[AsyncMock]:
    """The client's ``name`` gets no answer: the connection is lost."""
    lost = AsyncMock(side_effect=HomeAssistantConnectionError("connection lost"))
    with patch.object(client, name, lost):
        yield lost


@contextmanager
def _cancelled_on(
    hass: HomeAssistant, command: str, scope: anyio.CancelScope
) -> Iterator[None]:
    """``scope`` is cancelled as soon as Core receives ``command``; Core's
    handler still answers it."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]

    def cancel(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        scope.cancel()
        core_handler(core, connection, msg)

    websocket_api.async_register_command(hass, command, cancel, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)


def _subentry_read(subentry_type: str = "device") -> dict[str, Any]:
    return {"include_subentry_schema": True, "subentry_type": subentry_type}


_READS: dict[str, dict[str, Any]] = {
    "options": {"include_schema": True},
    "subentry": _subentry_read(),
}
_ABORTS = {
    "entry_options": "abort_options_flow",
    "options": "abort_options_flow",
    "subentry": "abort_config_subentry_flow",
}


def _mount_first(
    mount: Mount, kind: str, first: Callable[[Any], Any]
) -> MockConfigEntry:
    """An entry whose options flow (``options``, ``entry_options``) or
    ``device`` subentry flow (``subentry``) opens with ``first``."""
    if kind == "subentry":
        return mount(subentries={"device": _subentry(first)})
    return mount(options=_options(first))


def _mount_form(mount: Mount, kind: str) -> MockConfigEntry:
    """An entry whose flow of ``kind`` opens with a ``host`` form."""
    return _mount_first(mount, kind, _host_form if kind == "subentry" else _form(_HOST))


async def _read(
    tools: IntegrationTools, client: HomeAssistantClient, kind: str, entry_id: str
) -> dict[str, Any]:
    if kind == "entry_options":
        options, ok = await fetch_entry_options_with_status(client, entry_id)
        return {"options": options, "ok": ok}
    resp: dict[str, Any] = await tools.ha_get_integration(
        entry_id=entry_id, **_READS[kind]
    )
    return resp


def _abort_levels(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.levelname
        for r in caplog.records
        if r.name == _INTROSPECT_LOG and "abort" in r.getMessage()
    ]


# --- the options form ----------------------------------------------------------


async def test_options_form_is_described_with_help_and_current_values(
    hass: HomeAssistant, tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(options=_options(_form(_HOST)), stored={"host": "127.0.0.1"})

    resp = await tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)

    schema = resp["options_schema"]
    assert schema["flow_type"] == "form"
    assert schema["step_id"] == "init"
    assert schema["fields"] == [
        {
            "name": "host",
            "required": True,
            "type": "text",
            "description": "Device IP.",
            "current": "127.0.0.1",
        }
    ]
    assert "data_schema" not in schema
    assert resp["entry"]["options"] == {"host": "127.0.0.1"}
    assert open_flow_ids(hass) == []


async def test_a_password_field_stays_recognisable_as_secret(
    tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(options=_options(_form(_SECRET)))

    resp = await tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)

    fields = {f["name"]: f for f in resp["options_schema"]["fields"]}
    assert fields["api_key"]["input_type"] == "password"
    assert "input_type" not in fields["host"]


async def test_a_select_without_option_labels_from_core_is_described_as_given(
    tools: IntegrationTools, mount: Mount
) -> None:
    """Core labels a select's options only under its translation_key, in the
    integration's selector strings; with neither there is nothing to add."""
    selects = vol.Schema(
        {
            vol.Required("speed"): SelectSelector(
                SelectSelectorConfig(options=["low", "high"], translation_key="speed")
            ),
            vol.Required("color"): SelectSelector(
                SelectSelectorConfig(options=["red", "blue"])
            ),
        }
    )
    entry = mount(options=_options(_form(selects)))

    resp = await tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)

    assert resp["options_schema"]["fields"] == [
        {
            "name": "speed",
            "required": True,
            "type": "select",
            "options": ["low", "high"],
        },
        {
            "name": "color",
            "required": True,
            "type": "select",
            "options": ["red", "blue"],
        },
    ]


async def test_menu_without_a_choice_lists_the_branches_and_points_at_menu_choice(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    mount: Mount,
) -> None:
    entry = mount(options=_options(_menu))

    with patch.object(
        core_client,
        "submit_options_flow_step",
        wraps=core_client.submit_options_flow_step,
    ) as submitted:
        resp = await tools.ha_get_integration(
            entry_id=entry.entry_id, include_schema=True
        )

    schema = resp["options_schema"]
    assert schema["flow_type"] == "menu"
    assert schema["menu_options"] == _MENU
    assert "menu_choice to ha_set_integration" in schema["note"]
    assert "describe=" not in schema["note"]
    # A menu_choice is submitted:
    # test_a_subentry_menu_choice_that_finishes_the_flow_is_reported_as_saved.
    submitted.assert_not_called()
    assert open_flow_ids(hass) == []


async def test_options_behind_a_menu_are_reported_unread_on_the_legacy_read(
    tools: IntegrationTools, mount: Mount
) -> None:
    """The branch form shows one branch's fields, not the entry's options."""
    entry = mount(options=_options(_menu), stored={"host": "127.0.0.1"})

    resp = await tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)

    assert "options" not in resp["entry"]
    assert (
        f"options of {entry.entry_id} not read: the options flow opens with a "
        "'menu' step, not a form"
    ) in resp["warnings"]


async def test_a_component_read_behind_a_menu_keeps_its_options_without_a_warning(
    component_tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(options=_options(_menu), stored={"a": 1})

    resp = await component_tools.ha_get_integration(
        entry_id=entry.entry_id, include_schema=True
    )

    assert resp["options_schema"]["flow_type"] == "menu"
    assert resp["entry"]["options"] == {"a": 1}
    assert "warnings" not in resp


# --- closing the flow ------------------------------------------------------------


@pytest.mark.parametrize("kind", ["options", "subentry"])
async def test_a_cancelled_read_still_closes_the_flow_it_opened(
    hass: HomeAssistant, tools: IntegrationTools, mount: Mount, kind: str
) -> None:
    """The caller's cancellation arrives while the form's strings are read."""
    entry = _mount_form(mount, kind)

    with (
        anyio.CancelScope() as scope,
        _cancelled_on(hass, "frontend/get_translations", scope),
    ):
        await tools.ha_get_integration(entry_id=entry.entry_id, **_READS[kind])

    assert scope.cancelled_caught
    assert open_flow_ids(hass) == []


@pytest.mark.parametrize("kind", list(_ABORTS))
async def test_closing_a_flow_core_already_closed_is_no_warning(
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    mount: Mount,
    kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A flow that aborts at once is gone: Core answers its abort with 404."""
    entry = _mount_first(mount, kind, _not_supported)

    with (
        caplog.at_level("DEBUG", logger=_INTROSPECT_LOG),
        patch.object(
            core_client, _ABORTS[kind], wraps=getattr(core_client, _ABORTS[kind])
        ) as aborted,
    ):
        await _read(tools, core_client, kind, entry.entry_id)

    aborted.assert_awaited_once()
    assert _abort_levels(caplog) == ["DEBUG"]


@pytest.mark.parametrize("kind", list(_ABORTS))
async def test_an_abort_without_an_answer_is_a_warning_and_keeps_the_read(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    mount: Mount,
    kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = _mount_form(mount, kind)

    with (
        caplog.at_level("DEBUG", logger=_INTROSPECT_LOG),
        _unanswered(core_client, _ABORTS[kind]) as aborted,
    ):
        resp = await _read(tools, core_client, kind, entry.entry_id)

    (flow_id,) = open_flow_ids(hass)
    aborted.assert_awaited_once_with(flow_id)
    assert _abort_levels(caplog) == ["WARNING"]
    if kind == "entry_options":
        assert resp == {"options": {}, "ok": True}
    else:
        assert resp[f"{kind}_schema"]["fields"][0]["name"] == "host"
        assert not any("abort" in w for w in resp.get("warnings", []))


async def test_a_menu_choice_a_form_does_not_offer_is_refused_and_closes_the_flow(
    hass: HomeAssistant, tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(subentries={"device": _subentry(_host_form)})

    body = await rejected_call_body(
        tools.ha_set_integration(
            entry_id=entry.entry_id, subentry_type="device", menu_choice="advanced"
        )
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "menu_choice 'advanced' does not apply" in body["error"]["message"]
    assert open_flow_ids(hass) == []


async def test_a_menu_choice_whose_step_fails_returns_cores_error_and_closes_the_flow(
    hass: HomeAssistant, tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(subentries={"device": _subentry(_subentry_menu, _broken)})

    body = await rejected_call_body(
        tools.ha_set_integration(
            entry_id=entry.entry_id, subentry_type="device", menu_choice="advanced"
        )
    )

    assert "500" in body["error"]["message"]
    assert open_flow_ids(hass) == []


async def test_a_subentry_menu_choice_that_finishes_the_flow_is_reported_as_saved(
    tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(
        subentries={
            "device": _subentry(
                _subentry_menu,
                lambda flow: flow.async_create_entry(title="Kitchen", data={}),
            )
        }
    )

    resp = await tools.ha_set_integration(
        entry_id=entry.entry_id, subentry_type="device", menu_choice="advanced"
    )

    assert resp["subentry_schema"] == {
        "subentry_type": "device",
        "subentry_id": None,
        "flow_type": "create_entry",
        "menu_choice": "advanced",
        "title": "Kitchen",
    }
    assert "Kitchen" in [s.title for s in entry.subentries.values()]


# --- a failed options flow -------------------------------------------------------


async def test_a_failed_options_probe_keeps_cores_message_in_the_warning(
    tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(options=_options(_broken), stored={"host": "127.0.0.1"})

    resp = await tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)

    assert "options_schema" not in resp
    assert "options" not in resp["entry"]
    (warning,) = [w for w in resp["warnings"] if "probe failed" in w]
    assert warning.startswith(
        f"options schema probe failed for {entry.entry_id}: HomeAssistantAPIError: "
        "API error: 500"
    )


async def test_a_swallowed_options_flow_failure_logs_its_traceback(
    tools: IntegrationTools, mount: Mount, caplog: pytest.LogCaptureFixture
) -> None:
    entry = mount(options=_options(_broken))

    with caplog.at_level("WARNING", logger="ha_mcp.tools.integration_schema"):
        await tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)

    (record,) = [
        r for r in caplog.records if r.name == "ha_mcp.tools.integration_schema"
    ]
    assert isinstance(record.exc_info, tuple)
    assert isinstance(record.exc_info[1], HomeAssistantAPIError)


# --- redact_secrets on the component's options -----------------------------------


@pytest.mark.parametrize("first", [_menu, _broken], ids=["menu", "flow-raises"])
async def test_a_component_read_without_a_readable_form_withholds_every_option(
    redact_on: None,
    component_tools: IntegrationTools,
    mount: Mount,
    first: OptionsStep,
) -> None:
    """Without the form the password fields cannot be told apart."""
    entry = mount(
        options=_options(first), stored={"host": "127.0.0.1", "api_key": "sk-LIVE"}
    )

    resp = await component_tools.ha_get_integration(
        entry_id=entry.entry_id, include_schema=True
    )

    assert resp["entry"]["options"] == {"host": REDACTED_SET, "api_key": REDACTED_SET}
    assert any("no options schema readable" in w for w in resp["warnings"])


async def test_a_redaction_error_still_closes_the_options_flow(
    hass: HomeAssistant,
    redact_on: None,
    component_tools: IntegrationTools,
    mount: Mount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flow's first step may act, so a bug in the redaction must not leave
    the flow it was opened for open; the error itself fails the read."""

    def redaction_bug(*_args: Any) -> None:
        raise RuntimeError("redaction bug")

    monkeypatch.setattr(integration_schema, "redact_options_by_form", redaction_bug)
    entry = mount(options=_options(_form(_SECRET)), stored={"api_key": "sk-LIVE"})

    await rejected_call_body(
        component_tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)
    )

    assert open_flow_ids(hass) == []


async def test_component_read_keeps_its_options_and_redacts_the_secret(
    redact_on: None, component_tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(
        options=_options(_form(_SECRET)),
        stored={"host": "127.0.0.1", "api_key": "sk-LIVE"},
    )

    resp = await component_tools.ha_get_integration(
        entry_id=entry.entry_id, include_schema=True
    )

    assert resp["entry"]["options"] == {"host": "127.0.0.1", "api_key": REDACTED_SET}
    fields = {f["name"]: f for f in resp["options_schema"]["fields"]}
    assert fields["api_key"]["current"] == REDACTED_SET
    assert "sk-LIVE" not in json.dumps(resp)


# --- aborts, finished flows and steps without a form -----------------------------


@pytest.mark.parametrize(
    ("kind", "category"), [("options", "options"), ("subentry", "config_subentries")]
)
async def test_a_flow_that_aborts_at_once_returns_cores_reason_text(
    hass: HomeAssistant,
    tools: IntegrationTools,
    mount: Mount,
    kind: str,
    category: str,
) -> None:
    entry = _mount_first(mount, kind, _not_supported)

    with received(hass, "frontend/get_translations") as seen:
        resp = await tools.ha_get_integration(entry_id=entry.entry_id, **_READS[kind])

    schema = resp[f"{kind}_schema"]
    assert schema["flow_type"] == "abort"
    assert schema["aborted"] == "not_supported"
    assert schema["reason_text"] == "X1 has no options."
    assert [
        (m["category"], m["integration"]) for m in seen["frontend/get_translations"]
    ] == [(category, [_DOMAIN])]


async def test_options_abort_reads_the_reason_under_its_translation_domain(
    tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(
        options=_options(
            lambda flow: flow.async_abort(
                reason="reauth_required", translation_domain=_OTHER
            )
        )
    )

    resp = await tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)

    assert resp["options_schema"]["reason_text"] == "Sign in again."


@pytest.mark.parametrize("kind", ["options", "subentry"])
async def test_an_entry_flow_that_finishes_gets_the_no_form_error(
    tools: IntegrationTools, mount: Mount, kind: str
) -> None:
    entry = _mount_first(
        mount, kind, lambda flow: flow.async_create_entry(title="Chat", data={})
    )

    body = await rejected_call_body(
        tools.ha_get_integration(entry_id=entry.entry_id, **_READS[kind])
    )

    message = body["error"]["message"]
    assert "'create_entry' step, which has no form" in message
    assert message.endswith("the flow is being aborted")
    assert "warnings" not in body


async def _forever() -> None:
    await asyncio.Event().wait()


async def test_options_flow_in_progress_is_aborted_and_reported(
    hass: HomeAssistant, tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(
        options=_options(
            lambda flow: flow.async_show_progress(
                step_id="init",
                progress_action="scanning",
                progress_task=flow.hass.async_create_task(_forever()),
            )
        )
    )

    try:
        body = await rejected_call_body(
            tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)
        )

        assert body["progress_action"] == "scanning"
        assert body["progress_action_text"] == "Scanning 3 hubs."
        assert open_flow_ids(hass) == []
    finally:
        # A flow left open would keep its scan running past the test.
        for flow_id in open_flow_ids(hass):
            hass.config_entries.options.async_abort(flow_id)
        await hass.async_block_till_done()


async def test_subentry_flow_at_an_external_step_is_aborted_and_named(
    hass: HomeAssistant, tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(
        subentries={
            "device": _subentry(
                lambda flow: flow.async_external_step(
                    step_id="user", url="https://example.com/auth"
                )
            )
        }
    )

    body = await rejected_call_body(
        tools.ha_get_integration(entry_id=entry.entry_id, **_READS["subentry"])
    )

    assert "'external'" in body["error"]["message"]
    # A progress step names its action: the test above.
    assert "progress_action" not in body
    assert open_flow_ids(hass) == []


# --- help text and Core's strings ------------------------------------------------


async def test_subentry_form_reads_help_under_its_subentry_type(
    hass: HomeAssistant, tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(subentries={"conversation": _subentry(_host_form)})

    with received(hass, "frontend/get_translations") as seen:
        resp = await tools.ha_get_integration(
            entry_id=entry.entry_id, **_subentry_read("conversation")
        )

    schema = resp["subentry_schema"]
    assert schema["subentry_type"] == "conversation"
    assert schema["fields"][0]["description"] == "Ollama server."
    assert [
        (m["category"], m["integration"]) for m in seen["frontend/get_translations"]
    ] == [("config_subentries", [_DOMAIN])]
    assert open_flow_ids(hass) == []


async def test_list_mode_include_schema_is_ignored_and_points_to_set(
    core_client: HomeAssistantClient, tools: IntegrationTools, mount: Mount
) -> None:
    mount(options=_options(_form(_HOST)))

    with (
        patch.object(
            core_client, "start_config_flow", wraps=core_client.start_config_flow
        ) as setup,
        patch.object(
            core_client, "start_options_flow", wraps=core_client.start_options_flow
        ) as options,
    ):
        resp = await tools.ha_get_integration(include_schema=True)

    # ha_set_integration(domain=...) starts the setup flow: test_set_integration_forms.py.
    setup.assert_not_called()
    options.assert_not_called()
    assert any("ha_set_integration(domain=" in w for w in resp["warnings"])
    assert not any("describe=" in w for w in resp["warnings"])


_UNREAD = f"strings for {_DOMAIN} could not be read"


@pytest.mark.parametrize("kind", ["options", "subentry"])
async def test_a_failed_strings_read_is_a_top_level_warning_on_the_described_form(
    unreadable_strings: IntegrationTools, mount: Mount, kind: str
) -> None:
    entry = _mount_form(mount, kind)

    resp = await unreadable_strings.ha_get_integration(
        entry_id=entry.entry_id, **_READS[kind]
    )

    schema = resp[f"{kind}_schema"]
    assert schema["fields"][0]["name"] == "host"
    assert "warnings" not in schema
    assert any(_UNREAD in w for w in resp["warnings"])


async def test_a_failed_strings_read_is_a_warning_on_an_abort(
    unreadable_strings: IntegrationTools, mount: Mount
) -> None:
    entry = mount(options=_options(_no_options))

    resp = await unreadable_strings.ha_get_integration(
        entry_id=entry.entry_id, include_schema=True
    )

    schema = resp["options_schema"]
    assert schema["aborted"] == "no_options"
    assert "warnings" not in schema
    assert any(_UNREAD in w for w in resp["warnings"])


async def test_a_string_core_does_not_ship_is_no_warning(
    tools: IntegrationTools, mount: Mount
) -> None:
    entry = mount(options=_options(_no_options))

    resp = await tools.ha_get_integration(entry_id=entry.entry_id, include_schema=True)

    assert not any("strings for" in w for w in resp.get("warnings", []))
    assert "warnings" not in resp["options_schema"]
    assert "reason_text" not in resp["options_schema"]
