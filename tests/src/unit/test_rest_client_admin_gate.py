"""A non-admin token must not get the ha-mcp host IP-banned (#2546).

Home Assistant answers a non-admin user's request to an admin-only REST route
with a raised 401, which ``http.ban`` counts as a failed login. The server's
client asks ``auth/current_user`` once per token, refuses admin-only routes
locally for a non-admin answer, and calls services over WebSocket for a
non-admin token. Every test runs on a real ``hass`` whose ``http.ban`` counts
failed logins; a test that leaves ``auth`` unloaded gets no answer to the
probe, as when Home Assistant cannot be asked.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.components.http import ban
from homeassistant.components.websocket_api.const import (
    SIGNAL_WEBSOCKET_CONNECTED,
    SIGNAL_WEBSOCKET_DISCONNECTED,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockUser, async_capture_events
from pytest_homeassistant.typing import ClientSessionGenerator

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.admin_routes import is_admin_only_route
from ha_mcp.client.rest_client import (
    NON_ADMIN_TOKEN_WARNING,
    HomeAssistantAdminRequiredError,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
)
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._client_spies import sent_messages
from ._code_mode import run_custom_tool
from ._command_faults import received
from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._error_log_core import log_file as log_file
from ._tool_errors import error_body
from ._ws_transport import until

EVENT = "ha_mcp_admin_gate"
# An admin-only service: Core refuses it to a non-admin before running it.
ADMIN_SERVICE = ("automation", "reload")


@pytest.fixture
def failed_logins(caplog: pytest.LogCaptureFixture) -> Callable[[], int]:
    """How many failed logins Core's ``http.ban`` recorded meanwhile.

    Read from its log: a later successful login resets its per-address count.
    """
    caplog.set_level(logging.WARNING, logger=ban.__name__)
    return lambda: sum(
        r.name == ban.__name__ and r.funcName == ban.process_wrong_login.__name__
        for r in caplog.records
    )


@pytest.fixture
async def probe_answered(hass: HomeAssistant) -> None:
    """Core answers ``auth/current_user``, as on every install."""
    assert await async_setup_component(hass, "auth", {})


@pytest.fixture
async def sockets(hass: HomeAssistant) -> AsyncIterator[dict[str, int]]:
    """How many WebSocket connections Core accepted and closed meanwhile."""
    seen = {"opened": 0, "closed": 0}

    def count(key: str) -> Callable[[], None]:
        def bump() -> None:
            seen[key] += 1

        return bump

    unsubs = [
        async_dispatcher_connect(hass, SIGNAL_WEBSOCKET_CONNECTED, count("opened")),
        async_dispatcher_connect(hass, SIGNAL_WEBSOCKET_DISCONNECTED, count("closed")),
    ]
    yield seen
    for unsub in unsubs:
        unsub()


@pytest.fixture
async def admin_service(hass: HomeAssistant) -> None:
    """``automation.reload`` loaded, with a configuration it can reload."""
    await write_config_files(hass, {"configuration.yaml": "automation: []\n"})
    await setup_from_configuration(hass, "automation")


async def _demote(hass: HomeAssistant, user: MockUser) -> None:
    """The token's user stops being an administrator mid-session."""
    await hass.auth.async_update_user(user, group_ids=[GROUP_ID_USER])
    assert not user.is_admin


# --- The route table ---------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "endpoint"),
    [
        # Admin-only in Core, one case per pattern and method.
        ("POST", "/states/sensor.x"),
        ("DELETE", "/states/sensor.x"),
        ("POST", "/events/my_event"),
        ("GET", "/stream"),
        ("POST", "/template"),
        ("GET", "/error_log?lines=50"),
        ("GET", "/config/automation/config/123"),
        ("POST", "/config/automation/config/123"),
        ("DELETE", "config/scene/config/456"),
        ("GET", "config/script/config/morning"),
        ("POST", "/config/core/check_config"),
        ("GET", "/diagnostics/config_entry/abc"),
        ("GET", "/diagnostics/config_entry/abc/device/dev1"),
        ("DELETE", "/config/config_entries/entry/abc"),
        ("POST", "/config/config_entries/entry/abc/reload"),
        ("POST", "/config/config_entries/flow"),
        ("GET", "/config/config_entries/flow/f1"),
        ("POST", "/config/config_entries/flow/f1"),
        ("POST", "/config/config_entries/options/flow"),
        ("POST", "/config/config_entries/options/flow/f1"),
        ("POST", "/repairs/issues/fix"),
        ("POST", "/repairs/issues/fix/f1"),
        ("POST", "/config/config_entries/subentries/flow"),
        ("GET", "/config/config_entries/subentries/flow/f1"),
        # Open to every user, or refused before the admin gate.
        ("GET", "/config"),
        ("GET", "/states/sensor.x"),
        ("GET", "/config/config_entries/entry"),
        ("DELETE", "/config/config_entries/flow/f1"),
        ("DELETE", "/config/config_entries/options/flow/f1"),
        ("DELETE", "/repairs/issues/fix/f1"),
        ("GET", "/config/config_entries/flow"),
    ],
)
@pytest.mark.usefixtures("log_file")
async def test_the_admin_route_table_is_what_core_counts_as_a_failed_login(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_read_only_access_token: str,
    failed_logins: Callable[[], int],
    method: str,
    endpoint: str,
) -> None:
    """A route missing from the table gets a non-admin host banned; a route
    wrongly in it refuses a non-admin what Core would give."""
    for domain in ("api", "config", "diagnostics", "repairs"):
        assert await async_setup_component(hass, domain, {})
    client = await hass_client(hass_read_only_access_token)

    response = await client.request(method, f"/api/{endpoint.lstrip('/')}")

    counted = failed_logins() > 0
    assert counted == is_admin_only_route(method, endpoint), response.status


# --- Admin-only REST routes -------------------------------------------------


@pytest.mark.usefixtures("probe_answered")
async def test_a_non_admin_tokens_admin_only_request_never_reaches_core(
    hass: HomeAssistant,
    read_only_client: HomeAssistantClient,
    failed_logins: Callable[[], int],
) -> None:
    """Callers that handle an auth error see the refusal as one."""
    fired = async_capture_events(hass, EVENT)

    with pytest.raises(HomeAssistantAuthError) as exc:
        await read_only_client.fire_event(EVENT)

    assert isinstance(exc.value, HomeAssistantAdminRequiredError)
    assert f"POST /api/events/{EVENT} is admin-only" in str(exc.value)
    assert (failed_logins(), fired) == (0, [])
    assert read_only_client.admin_route_refused is True


@pytest.mark.usefixtures("probe_answered")
async def test_a_non_admin_token_reads_open_routes_without_a_probe(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    hass.states.async_set("sensor.kitchen", "21")

    with received(hass, "auth/current_user") as probes:
        states = await read_only_client.get_states()

    assert "sensor.kitchen" in [s["entity_id"] for s in states]
    assert probes["auth/current_user"] == []


@pytest.mark.usefixtures("probe_answered")
async def test_an_admin_token_reaches_admin_only_routes(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    fired = async_capture_events(hass, EVENT)

    await core_client.fire_event(EVENT)

    assert len(fired) == 1
    assert core_client.known_is_admin is True
    assert core_client.admin_route_refused is False


@pytest.mark.usefixtures("probe_answered")
async def test_one_probe_answers_every_admin_only_request_of_a_client(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    with received(hass, "auth/current_user") as probes:
        for _ in range(3):
            with pytest.raises(HomeAssistantAdminRequiredError):
                await read_only_client.fire_event(EVENT)

    assert len(probes["auth/current_user"]) == 1


async def test_a_successful_admin_only_request_settles_an_unanswered_probe(
    hass: HomeAssistant, core_client: HomeAssistantClient, sockets: dict[str, int]
) -> None:
    """A 2xx from an admin-only route proves the token is an admin's, so
    later requests neither probe again nor wait in line."""
    fired = async_capture_events(hass, EVENT)

    await core_client.fire_event(EVENT)
    await core_client.fire_event(EVENT)

    assert len(fired) == 2
    assert sockets["opened"] == 1
    assert core_client.known_is_admin is True


async def test_a_refusal_before_the_admin_gate_settles_nothing(
    hass: HomeAssistant, core_client: HomeAssistantClient, sockets: dict[str, int]
) -> None:
    """Core's router answers 404 for a flow that does not exist before any
    admin check, so it proves nothing about the token."""
    assert await async_setup_component(hass, "config", {})

    for _ in range(2):
        with pytest.raises(HomeAssistantAPIError) as exc:
            await core_client.submit_config_flow_step("missing", {})
        assert exc.value.status_code == 404

    assert sockets["opened"] == 2
    assert core_client.known_is_admin is None


@pytest.mark.usefixtures("probe_answered")
async def test_the_probe_asks_on_a_connection_of_its_own(
    hass: HomeAssistant, read_only_client: HomeAssistantClient, sockets: dict[str, int]
) -> None:
    """The pooled connection may be running the caller's own bus handler,
    which would wait on itself; the probe's connection is closed after."""
    with (
        sent_messages(read_only_client) as pooled,
        received(hass, "auth/current_user") as probes,
    ):
        assert await read_only_client.token_is_admin() is False

    assert len(probes["auth/current_user"]) == 1
    pooled.assert_not_awaited()
    await hass.async_block_till_done()
    assert sockets == {"opened": 1, "closed": 1}


async def test_an_unanswered_probe_reports_no_status_and_closes_its_socket(
    hass: HomeAssistant, core_client: HomeAssistantClient, sockets: dict[str, int]
) -> None:
    assert await core_client.token_is_admin() is None

    await hass.async_block_till_done()
    assert sockets == {"opened": 1, "closed": 1}
    assert core_client.known_is_admin is None


async def test_a_probe_that_cannot_connect_reports_no_status(
    unreachable_client: HomeAssistantClient,
) -> None:
    assert await unreachable_client.token_is_admin() is None
    assert unreachable_client.known_is_admin is None


async def test_a_401_on_an_admin_only_route_names_the_admin_requirement(
    read_only_client: HomeAssistantClient,
) -> None:
    with pytest.raises(HomeAssistantAuthError) as exc:
        await read_only_client.fire_event(EVENT)

    assert not isinstance(exc.value, HomeAssistantAdminRequiredError)
    assert "administrator" in str(exc.value)


async def test_a_401_on_an_admin_only_route_stops_further_ones_but_not_open_routes(
    hass: HomeAssistant,
    read_only_client: HomeAssistantClient,
    failed_logins: Callable[[], int],
) -> None:
    hass.states.async_set("sensor.kitchen", "21")
    with pytest.raises(HomeAssistantAuthError) as exc:
        await read_only_client.fire_event(EVENT)
    assert "returned 401" in str(exc.value)

    for _ in range(2):
        with pytest.raises(HomeAssistantAdminRequiredError) as refused:
            await read_only_client.fire_event(EVENT)
        assert "not sent" in str(refused.value)

    assert failed_logins() == 1
    assert read_only_client.admin_route_refused is True
    states = await read_only_client.get_states()
    assert "sensor.kitchen" in [s["entity_id"] for s in states]


async def test_parallel_admin_only_requests_on_an_unanswered_probe_cost_one_failed_login(
    read_only_client: HomeAssistantClient,
    failed_logins: Callable[[], int],
    sockets: dict[str, int],
) -> None:
    """Ten parallel ``ha_search`` fetches must not all reach Home Assistant
    before the first 401 latches, nor each probe again (a failed handshake
    for an invalid token, which ``http.ban`` counts too)."""
    results = await asyncio.gather(
        *(read_only_client.fire_event(EVENT) for _ in range(10)),
        return_exceptions=True,
    )

    assert all(isinstance(r, HomeAssistantAuthError) for r in results)
    assert failed_logins() == 1
    assert sockets["opened"] == 1


@pytest.mark.usefixtures("probe_answered")
async def test_a_confirmed_admins_admin_only_requests_do_not_wait_in_line(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An event stream stays open; a confirmed admin's other admin-only
    request goes out beside it."""
    assert await core_client.token_is_admin() is True
    fired = async_capture_events(hass, EVENT)
    streaming = hass.bus.async_listeners().get("*", 0)
    stream = asyncio.create_task(core_client.guarded_request("GET", "/stream"))
    await until(lambda: hass.bus.async_listeners().get("*", 0) > streaming)

    await asyncio.wait_for(core_client.fire_event(EVENT), 5)

    assert len(fired) == 1
    stream.cancel()
    await asyncio.wait([stream])
    assert stream.cancelled()


@pytest.mark.usefixtures("probe_answered")
async def test_an_admin_demoted_mid_session_stops_sending_admin_only_requests(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_admin_user: MockUser,
    failed_logins: Callable[[], int],
) -> None:
    await core_client.fire_event(EVENT)
    await _demote(hass, hass_admin_user)

    with pytest.raises(HomeAssistantAuthError):
        await core_client.fire_event(EVENT)
    with pytest.raises(HomeAssistantAdminRequiredError):
        await core_client.fire_event(EVENT)

    assert failed_logins() == 1


@pytest.mark.usefixtures("probe_answered")
async def test_a_client_seeded_as_admin_asks_no_probe(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_access_token: str,
    sockets: dict[str, int],
) -> None:
    fired = async_capture_events(hass, EVENT)
    async with HomeAssistantClient(
        base_url=core_client.base_url,
        token=hass_access_token,
        verify_ssl=False,
        is_admin=True,
    ) as seeded:
        await seeded.fire_event(EVENT)
        assert seeded.admin_route_refused is False

    assert len(fired) == 1
    assert sockets["opened"] == 0


@pytest.mark.usefixtures("probe_answered")
async def test_a_client_seeded_with_a_refusal_sends_no_admin_only_request(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_access_token: str,
    sockets: dict[str, int],
) -> None:
    """Another client for the same token already got a 401."""
    fired = async_capture_events(hass, EVENT)
    async with HomeAssistantClient(
        base_url=core_client.base_url,
        token=hass_access_token,
        verify_ssl=False,
        admin_route_refused=True,
    ) as seeded:
        assert seeded.admin_route_refused is True
        with pytest.raises(HomeAssistantAdminRequiredError):
            await seeded.fire_event(EVENT)

    assert fired == []
    assert sockets["opened"] == 0


# --- What the caller is told ------------------------------------------------


@pytest.mark.usefixtures("probe_answered")
async def test_a_tool_refused_for_a_non_admin_token_asks_for_an_administrator(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    await write_config_files(
        hass,
        {
            "automations.yaml": "- id: '171'\n  alias: Morning\n"
            "  triggers: []\n  actions: []\n",
            "configuration.yaml": "automation: !include automations.yaml\n",
        },
    )
    await setup_from_configuration(hass, "automation")

    with pytest.raises(ToolError) as exc:
        await AutomationConfigTools(read_only_client).ha_config_get_automation(
            entity_id="automation.morning"
        )

    body = error_body(exc)
    assert body["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
    assert body["warnings"] == [NON_ADMIN_TOKEN_WARNING]


@pytest.mark.usefixtures("probe_answered")
async def test_the_first_non_admin_answer_logs_the_unsupported_warning_once(
    read_only_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ha_mcp.client.rest_client"):
        assert await read_only_client.token_is_admin() is False
        assert await read_only_client.token_is_admin() is False

    warnings = [r for r in caplog.records if NON_ADMIN_TOKEN_WARNING in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "code",
    [
        "await api_get('/config/automation/config/1')",
        "await api_post('/template', {'template': '{{ 1 }}'})",
    ],
    ids=["api_get", "api_post"],
)
@pytest.mark.usefixtures("probe_answered")
async def test_code_mode_admin_only_route_is_refused_with_the_warning(
    read_only_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    failed_logins: Callable[[], int],
    code: str,
) -> None:
    result = await run_custom_tool(read_only_client, monkeypatch, code)

    assert "admin-only" in result["error"]
    assert result["warnings"] == [NON_ADMIN_TOKEN_WARNING]
    assert failed_logins() == 0


@pytest.mark.usefixtures("probe_answered")
async def test_code_mode_open_route_still_reaches_core(
    hass: HomeAssistant,
    read_only_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass.states.async_set("sensor.kitchen", "21")

    states = await run_custom_tool(
        read_only_client, monkeypatch, "await api_get('/states')"
    )

    assert "sensor.kitchen" in [s["entity_id"] for s in states]


# --- Service calls ----------------------------------------------------------


def _register_echo(hass: HomeAssistant) -> list[ServiceCall]:
    """A service open to every user that answers with what it was sent."""
    calls: list[ServiceCall] = []

    async def echo(call: ServiceCall) -> ServiceResponse:
        calls.append(call)
        return {"echo": dict(call.data)}

    hass.services.async_register(
        "ha_mcp_test", "echo", echo, supports_response=SupportsResponse.OPTIONAL
    )
    return calls


@pytest.mark.usefixtures("probe_answered")
async def test_a_non_admin_service_call_goes_over_websocket(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    """Over REST an admin-only service would answer 401, counted by
    ``http.ban``."""
    calls = _register_echo(hass)

    with sent_messages(read_only_client) as sent:
        result = await read_only_client.call_service(
            "ha_mcp_test", "echo", {"word": "hi"}
        )

    assert result == []
    assert [c.data for c in calls] == [{"word": "hi"}]
    assert [c.args[0]["type"] for c in sent.call_args_list] == ["call_service"]


@pytest.mark.usefixtures("probe_answered")
async def test_a_non_admin_service_call_keeps_the_response_shape(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    _register_echo(hass)

    result = await read_only_client.call_service(
        "ha_mcp_test", "echo", {"word": "hi"}, return_response=True
    )

    assert result == {
        "changed_states": [],
        "service_response": {"echo": {"word": "hi"}},
    }


@pytest.mark.usefixtures("probe_answered", "admin_service")
async def test_an_admin_only_service_refused_to_a_non_admin_raises_admin_required(
    read_only_client: HomeAssistantClient, failed_logins: Callable[[], int]
) -> None:
    with pytest.raises(HomeAssistantAdminRequiredError) as exc:
        await read_only_client.call_service(*ADMIN_SERVICE)

    assert "automation.reload is admin-only" in str(exc.value)
    assert failed_logins() == 0


@pytest.mark.usefixtures("probe_answered")
async def test_other_websocket_service_failures_raise_api_errors(
    read_only_client: HomeAssistantClient,
) -> None:
    with pytest.raises(HomeAssistantAPIError) as exc:
        await read_only_client.call_service("light", "nope")

    assert not isinstance(exc.value, HomeAssistantAuthError)
    assert exc.value.status_code == 400
    assert "not found" in str(exc.value)


@pytest.mark.usefixtures("probe_answered", "admin_service")
async def test_an_admin_service_call_stays_on_rest(
    core_client: HomeAssistantClient,
) -> None:
    with sent_messages(core_client) as sent:
        await core_client.call_service(*ADMIN_SERVICE)

    assert "call_service" not in [c.args[0]["type"] for c in sent.call_args_list]


@pytest.mark.usefixtures("admin_service")
async def test_parallel_admin_only_service_calls_on_an_unanswered_probe_cost_one_failed_login(
    read_only_client: HomeAssistantClient, failed_logins: Callable[[], int]
) -> None:
    with sent_messages(read_only_client) as sent:
        results = await asyncio.gather(
            *(read_only_client.call_service(*ADMIN_SERVICE) for _ in range(4)),
            return_exceptions=True,
        )

    assert all(isinstance(r, HomeAssistantAdminRequiredError) for r in results)
    assert failed_logins() == 1
    sent_types = [c.args[0]["type"] for c in sent.call_args_list]
    assert sent_types == ["call_service"] * 4


@pytest.mark.usefixtures("probe_answered", "admin_service")
async def test_a_demoted_admins_service_calls_fall_back_to_websocket(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_admin_user: MockUser,
    failed_logins: Callable[[], int],
) -> None:
    """A token demoted after the probe answered must not keep sending 401s."""
    await core_client.call_service(*ADMIN_SERVICE)
    await _demote(hass, hass_admin_user)

    for _ in range(2):
        with pytest.raises(HomeAssistantAdminRequiredError):
            await core_client.call_service(*ADMIN_SERVICE)

    assert failed_logins() == 1
    assert core_client.admin_route_refused is True
