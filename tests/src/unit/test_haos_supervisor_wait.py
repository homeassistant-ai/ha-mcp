"""The HAOS image builder's Supervisor readiness and image-variant steps.

The image pins stable OS/Core and a Supervisor minimum; Supervisor self-updates
to the channel head after boot. Until that finishes ``update_available`` is
True and store operations are blocked. The builder waits for readiness, selects
the requested channel, and installs an exact Core version. Here it runs against
real Core with the Supervisor stand-in behind ``supervisor/api``
(``_haos_build_core``); its clock is the test's, so waits take no time.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.haos_image_build.build_image import (
    HAWebSocket,
    OAuthCredentials,
    WSCommandError,
    _configure_core_image_variant,
    _configure_supervisor_image_variant,
    _reconnect_during_supervisor_update,
    _SupervisorReadinessTimeout,
    _wait_supervisor_channel_metadata,
    _wait_supervisor_running,
    wait_core_version,
    wait_supervisor_ready,
)

from ._haos_build_core import RESTARTING, Haos, core_info, root_info, supervisor_info
from ._haos_build_core import build_supervisor as build_supervisor
from ._haos_build_core import haos as haos
from ._haos_build_core import target_app as target_app

SETUP_STATE = "System is not ready with state: setup"
CORE_JOB_BUSY = "Another job is running for job group home_assistant_core"
PENDING = supervisor_info(version="2026.05.1", update_available=True)
BETA_PENDING = supervisor_info(
    version="2026.07.5",
    version_latest="2026.08.0",
    update_available=True,
    channel="beta",
)
BETA_SETTLED = supervisor_info(
    version="2026.08.0", version_latest="2026.08.0", channel="beta"
)
CORE_OLD = core_info("2026.8.2", version_latest="2026.8.3", update_available=True)
CORE_NEW = core_info("2026.8.3", version_latest="2026.8.3", update_available=False)


def _answers(haos: Haos, method: str, path: str, *answers: Any) -> None:
    """Supervisor's answers to ``method path``, in order; the last one stays."""
    for answer in answers:
        if answer is RESTARTING:
            haos.supervisor.restarting(method, path)
        elif isinstance(answer, str):
            haos.supervisor.refuse(method, path, answer)
        else:
            haos.supervisor.answer(method, path, answer)


def _beta_channel(haos: Haos, *infos: Any) -> None:
    """Supervisor accepts the channel switch and reload, then answers ``infos``."""
    haos.supervisor.answer("POST", "/supervisor/options", {})
    haos.supervisor.answer("POST", "/supervisor/reload", {})
    _answers(haos, "GET", "/supervisor/info", *infos)


def _offline() -> HAWebSocket:
    """A builder client never connected: any command it sent would fail."""
    return HAWebSocket(
        "http://127.0.0.1:1", OAuthCredentials(access_token="", refresh_token="")
    )


# -- wait_supervisor_ready ---------------------------------------------------


async def test_a_settled_supervisor_is_ready_on_the_first_read(haos: Haos) -> None:
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    ws = await haos.connected()

    info = await haos.run(wait_supervisor_ready, ws)

    assert info == supervisor_info()
    assert haos.supervisor.requests == [("GET", "/supervisor/info")]
    assert haos.clock.sleeps == []


async def test_a_setup_state_on_the_first_read_is_retried_after_a_reconnect(
    haos: Haos,
) -> None:
    _answers(haos, "GET", "/supervisor/info", SETUP_STATE, supervisor_info())
    ws = await haos.connected()

    info = await haos.run(wait_supervisor_ready, ws)

    assert info == supervisor_info()
    assert haos.clock.sleeps == [10.0]
    assert haos.relay.websockets == 2


async def test_polls_until_the_pending_update_clears(haos: Haos) -> None:
    _answers(haos, "GET", "/supervisor/info", PENDING, PENDING, supervisor_info())
    ws = await haos.connected()

    info = await haos.run(wait_supervisor_ready, ws)

    assert info == supervisor_info()
    assert haos.clock.sleeps == [10.0, 10.0]


async def test_restart_errors_during_the_update_keep_polling(haos: Haos) -> None:
    """A self-updating Supervisor answers as restarting, then Core drops the
    builder's socket; both are waited out with a reconnect each."""
    _answers(haos, "GET", "/supervisor/info", PENDING, RESTARTING, supervisor_info())
    haos.clock.on_sleep[1] = haos.relay.cut
    ws = await haos.connected()

    info = await haos.run(wait_supervisor_ready, ws)

    assert info == supervisor_info()
    assert haos.clock.sleeps == [10.0, 10.0, 10.0]
    assert haos.relay.websockets == 3


async def test_an_answer_after_the_deadline_is_not_accepted(haos: Haos) -> None:
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    haos.supervisor.when("GET", "/supervisor/info", lambda: haos.clock.advance(15.0))
    ws = await haos.connected()

    with pytest.raises(_SupervisorReadinessTimeout):
        await haos.run(wait_supervisor_ready, ws, update_timeout=10.0)

    assert haos.supervisor.requests == [("GET", "/supervisor/info")]


async def test_no_budget_left_makes_no_request(haos: Haos) -> None:
    ws = await haos.connected()

    with pytest.raises(TimeoutError):
        await haos.run(wait_supervisor_ready, ws, update_timeout=0.0)

    assert haos.supervisor.requests == []


async def test_a_readiness_timeout_names_the_last_restart_error(haos: Haos) -> None:
    _answers(haos, "GET", "/supervisor/info", PENDING, RESTARTING)
    ws = await haos.connected()

    with pytest.raises(
        _SupervisorReadinessTimeout, match=r"last error.*WSCommandError"
    ):
        await haos.run(wait_supervisor_ready, ws, update_timeout=25.0)


async def test_a_readiness_timeout_names_a_failed_reconnect(haos: Haos) -> None:
    """When the reconnect itself fails, its error is the one that explains."""
    haos.supervisor.answer("GET", "/supervisor/info", PENDING)

    def signed_out_and_cut() -> None:
        haos.revoke_login()
        haos.relay.cut()

    haos.clock.on_sleep[0] = signed_out_and_cut
    ws = await haos.connected()

    with pytest.raises(_SupervisorReadinessTimeout, match="HTTPError 400"):
        await haos.run(wait_supervisor_ready, ws, update_timeout=15.0)


async def test_a_supervisor_refusal_is_not_retried(haos: Haos) -> None:
    _answers(haos, "GET", "/supervisor/info", PENDING, "invalid request")
    ws = await haos.connected()

    with pytest.raises(WSCommandError, match="invalid request"):
        await haos.run(wait_supervisor_ready, ws)

    assert haos.clock.sleeps == [10.0]
    assert haos.relay.websockets == 1


async def test_waits_for_the_requested_channel(haos: Haos) -> None:
    stable = supervisor_info(version="2026.08.0", version_latest="2026.08.0")
    _answers(haos, "GET", "/supervisor/info", stable, BETA_SETTLED)
    ws = await haos.connected()

    info = await haos.run(
        wait_supervisor_ready, ws, expected_channel="beta", minimum_version="2026.08.0"
    )

    assert info["channel"] == "beta"
    assert haos.clock.sleeps == [10.0]


async def test_waits_for_the_requested_minimum_version(haos: Haos) -> None:
    below = supervisor_info(
        version="2026.07.5", version_latest="2026.08.0", channel="beta"
    )
    _answers(haos, "GET", "/supervisor/info", below, BETA_SETTLED)
    ws = await haos.connected()

    info = await haos.run(
        wait_supervisor_ready, ws, expected_channel="beta", minimum_version="2026.08.0"
    )

    assert info["version"] == "2026.08.0"
    assert haos.clock.sleeps == [10.0]


# -- _wait_supervisor_running ------------------------------------------------


async def test_a_running_supervisor_is_ready_on_the_first_read(haos: Haos) -> None:
    haos.supervisor.answer("GET", "/info", root_info("running"))
    ws = await haos.connected()

    assert (await haos.run(_wait_supervisor_running, ws))["state"] == "running"
    assert haos.supervisor.requests == [("GET", "/info")]
    assert haos.clock.sleeps == []


async def test_polls_through_supervisors_startup(haos: Haos) -> None:
    states = (root_info("setup"), root_info("startup"), root_info("running"))
    _answers(haos, "GET", "/info", *states)
    ws = await haos.connected()

    await haos.run(_wait_supervisor_running, ws)

    assert haos.clock.sleeps == [10.0, 10.0]
    assert haos.relay.websockets == 1


async def test_a_setup_state_error_before_info_answers_is_retried(haos: Haos) -> None:
    _answers(haos, "GET", "/info", SETUP_STATE, root_info("running"))
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    ws = await haos.connected()

    await haos.run(_wait_supervisor_running, ws)

    assert haos.clock.sleeps == [10.0]
    assert haos.relay.websockets == 2


async def test_a_refusal_while_waiting_for_running_is_not_retried(haos: Haos) -> None:
    haos.supervisor.refuse("GET", "/info", "invalid request")
    ws = await haos.connected()

    with pytest.raises(WSCommandError, match="invalid request"):
        await haos.run(_wait_supervisor_running, ws)

    assert haos.clock.sleeps == []


async def test_a_never_running_supervisor_times_out_naming_its_state(
    haos: Haos,
) -> None:
    haos.supervisor.answer("GET", "/info", root_info("startup"))
    ws = await haos.connected()

    with pytest.raises(_SupervisorReadinessTimeout, match=r"last state='startup'"):
        await haos.run(_wait_supervisor_running, ws, timeout=10.0)

    assert haos.supervisor.requests == [("GET", "/info")]


# -- channel metadata and reconnects during a Supervisor update ----------------


async def test_a_channel_reload_timeout_names_the_last_restart_error(
    haos: Haos,
) -> None:
    haos.supervisor.restarting("GET", "/supervisor/info")
    ws = await haos.connected()

    with pytest.raises(TimeoutError, match=r"last error.*WSCommandError"):
        await haos.run(
            _wait_supervisor_channel_metadata,
            ws,
            channel="beta",
            minimum_version="2026.08.0",
            deadline=1.0,
        )

    assert haos.clock.sleeps == [1.0]


async def test_a_channel_reload_timeout_names_a_failed_reconnect(haos: Haos) -> None:
    ws = await haos.connected()
    haos.revoke_login()
    haos.relay.cut()

    with pytest.raises(TimeoutError, match="HTTPError 400"):
        await haos.run(
            _wait_supervisor_channel_metadata,
            ws,
            channel="beta",
            minimum_version="2026.08.0",
            deadline=1.0,
        )


async def test_a_reconnect_past_the_build_deadline_reports_instead_of_raising(
    haos: Haos,
) -> None:
    """During a Supervisor restart a failed reconnect is a diagnostic, not a crash."""
    ws = await haos.connected()
    haos.clock.advance(5.0)

    error = await haos.run(
        _reconnect_during_supervisor_update, ws, context="during test", deadline=1.0
    )

    assert isinstance(error, TimeoutError)
    assert haos.relay.websockets == 1


# -- _configure_supervisor_image_variant --------------------------------------


def test_the_stable_image_changes_nothing() -> None:
    _configure_supervisor_image_variant(_offline(), channel=None, minimum_version=None)


@pytest.mark.parametrize(
    ("settings", "refused"),
    [
        pytest.param(
            {"channel": None, "minimum_version": "2026.08.0"},
            "require a Supervisor channel",
            id="minimum-without-channel",
        ),
        pytest.param(
            {"channel": "dev", "minimum_version": "2026.09.0.dev1234"},
            "Unsupported Supervisor channel",
            id="dev-channel",
        ),
        pytest.param(
            {
                "channel": "beta",
                "minimum_version": "2026.08.0",
                "core_version": "2026.8.3",
            },
            "requires the Home Assistant base URL",
            id="core-without-base-url",
        ),
        pytest.param(
            {
                "base_url": "http://127.0.0.1:18123",
                "channel": "beta",
                "minimum_version": "2026.08.0",
                "core_version": "../latest",
            },
            "Invalid Core version",
            id="invalid-core-version",
        ),
    ],
)
def test_invalid_variant_settings_fail_before_any_supervisor_change(
    settings: dict[str, Any], refused: str
) -> None:
    with pytest.raises(ValueError, match=refused):
        _configure_supervisor_image_variant(_offline(), **settings)


async def test_a_current_beta_image_only_switches_the_channel(haos: Haos) -> None:
    _beta_channel(haos, BETA_SETTLED)
    ws = await haos.connected()

    await haos.run(
        _configure_supervisor_image_variant,
        ws,
        channel="beta",
        minimum_version="2026.08.0",
    )

    assert haos.supervisor.requests == [
        ("POST", "/supervisor/options"),
        ("POST", "/supervisor/reload"),
        ("GET", "/supervisor/info"),
    ]
    assert haos.supervisor.sent("POST", "/supervisor/options") == [{"channel": "beta"}]


async def test_an_advertised_beta_is_installed_and_waited_for(haos: Haos) -> None:
    _beta_channel(haos, BETA_PENDING, BETA_SETTLED)
    haos.supervisor.answer("POST", "/supervisor/update", {})
    ws = await haos.connected()

    await haos.run(
        _configure_supervisor_image_variant,
        ws,
        channel="beta",
        minimum_version="2026.08.0",
    )

    assert ("POST", "/supervisor/update") in haos.supervisor.requests
    assert haos.supervisor.requests[-1] == ("GET", "/supervisor/info")


@pytest.mark.parametrize(
    ("update", "infos", "reconnects"),
    [
        pytest.param(
            {}, (BETA_PENDING, RESTARTING, BETA_SETTLED), 1, id="restart-while-polling"
        ),
        pytest.param(
            RESTARTING, (BETA_PENDING, BETA_SETTLED), 0, id="restart-answers-the-update"
        ),
        pytest.param(
            {},
            (BETA_PENDING, SETUP_STATE, BETA_SETTLED),
            1,
            id="setup-state-while-polling",
        ),
    ],
)
async def test_a_supervisor_restart_cannot_abort_a_requested_beta_update(
    haos: Haos, update: Any, infos: tuple[Any, ...], reconnects: int
) -> None:
    _beta_channel(haos, *infos)
    _answers(haos, "POST", "/supervisor/update", update)
    ws = await haos.connected()

    await haos.run(
        _configure_supervisor_image_variant,
        ws,
        channel="beta",
        minimum_version="2026.08.0",
    )

    assert haos.supervisor.requests[-1] == ("GET", "/supervisor/info")
    assert haos.relay.websockets == 1 + reconnects


async def test_a_refused_beta_update_is_not_mistaken_for_a_restart(haos: Haos) -> None:
    """A write's refusal stays an error even when its message names a state a
    read would wait out: the update may not have happened."""
    _beta_channel(haos, BETA_PENDING)
    haos.supervisor.refuse("POST", "/supervisor/update", SETUP_STATE)
    ws = await haos.connected()

    with pytest.raises(WSCommandError, match="not ready"):
        await haos.run(
            _configure_supervisor_image_variant,
            ws,
            channel="beta",
            minimum_version="2026.08.0",
        )

    assert len(haos.supervisor.requests) == 4


async def test_a_beta_that_never_settles_times_out_with_its_versions(
    haos: Haos,
) -> None:
    _beta_channel(haos, BETA_PENDING)
    haos.supervisor.answer("POST", "/supervisor/update", {})
    ws = await haos.connected()

    with pytest.raises(
        TimeoutError, match=r"did not finish self-updating.*2026\.07\.5"
    ):
        await haos.run(
            _configure_supervisor_image_variant,
            ws,
            channel="beta",
            minimum_version="2026.08.0",
            timeout=1.0,
        )


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("2026.09.0.dev1234", id="dev-build"),
        pytest.param("2026.09.0", id="final-release-for-a-dev-minimum"),
    ],
)
async def test_a_dev_supervisor_minimum_is_met(haos: Haos, version: str) -> None:
    _beta_channel(
        haos,
        supervisor_info(version=version, version_latest=version, channel="beta"),
    )
    ws = await haos.connected()

    await haos.run(
        _configure_supervisor_image_variant,
        ws,
        channel="beta",
        minimum_version="2026.09.0.dev1234",
    )

    assert len(haos.supervisor.requests) == 3


# -- exact Core version ------------------------------------------------------


async def test_a_beta_image_installs_and_verifies_the_exact_core_version(
    haos: Haos,
) -> None:
    """Core restarts during its own update: the request is not repeated, the
    version is polled until it settles."""
    _beta_channel(haos, BETA_SETTLED)
    haos.supervisor.answer("GET", "/info", root_info("running"))
    _answers(haos, "GET", "/core/info", CORE_OLD, CORE_OLD, CORE_NEW)
    haos.supervisor.answer("POST", "/core/update", {})
    haos.supervisor.when("POST", "/core/update", haos.relay.cut)
    ws = await haos.connected()

    await haos.run(
        _configure_supervisor_image_variant,
        ws,
        base_url=haos.base_url,
        channel="beta",
        minimum_version="2026.08.0",
        core_version="2026.8.3",
    )

    assert haos.supervisor.sent("POST", "/core/update") == [
        {"version": "2026.8.3", "backup": False}
    ]
    assert haos.supervisor.requests[-1] == ("GET", "/core/info")
    assert haos.clock.sleeps == [5.0]


async def test_a_busy_core_job_is_retried_until_admitted(haos: Haos) -> None:
    _answers(
        haos,
        "GET",
        "/core/info",
        core_info("2026.9.1", version_latest="2026.9.1", update_available=False),
        core_info("2026.9.0", version_latest="2026.9.1", update_available=False),
    )
    _answers(haos, "POST", "/core/update", CORE_JOB_BUSY, {})
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    ws = await haos.connected()

    await haos.run(
        _configure_core_image_variant,
        ws,
        base_url=haos.base_url,
        core_version="2026.9.0",
    )

    updates = haos.supervisor.sent("POST", "/core/update")
    assert updates == [{"version": "2026.9.0", "backup": False}] * 2
    assert haos.clock.sleeps == [5.0]


async def test_a_stuck_core_job_gives_up_at_the_admission_deadline(haos: Haos) -> None:
    haos.supervisor.answer(
        "GET",
        "/core/info",
        core_info("2026.9.1", version_latest="2026.9.1", update_available=False),
    )
    haos.supervisor.refuse("POST", "/core/update", CORE_JOB_BUSY)
    haos.supervisor.when("POST", "/core/update", lambda: haos.clock.advance(600.0))
    ws = await haos.connected()

    with pytest.raises(TimeoutError, match="Core job admission"):
        await haos.run(
            _configure_core_image_variant,
            ws,
            base_url=haos.base_url,
            core_version="2026.9.0",
        )

    assert haos.supervisor.requests == [("GET", "/core/info"), ("POST", "/core/update")]


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param("Another job is running for job group supervisor", id="other-job"),
        pytest.param("Core update blocked because system is unhealthy", id="unhealthy"),
    ],
)
async def test_other_core_update_refusals_are_not_retried(
    haos: Haos, refusal: str
) -> None:
    haos.supervisor.answer(
        "GET",
        "/core/info",
        core_info("2026.9.1", version_latest="2026.9.1", update_available=False),
    )
    haos.supervisor.refuse("POST", "/core/update", refusal)
    ws = await haos.connected()

    with pytest.raises(WSCommandError, match=refusal):
        await haos.run(
            _configure_core_image_variant,
            ws,
            base_url=haos.base_url,
            core_version="2026.9.0",
        )

    assert haos.clock.sleeps == []


async def test_a_core_version_that_never_arrives_is_reported(haos: Haos) -> None:
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    haos.supervisor.answer("GET", "/core/info", CORE_OLD)
    ws = await haos.connected()

    with pytest.raises(TimeoutError) as raised:
        await haos.run(wait_core_version, ws, "2026.8.3", timeout=1.0)

    assert "expected='2026.8.3'" in str(raised.value)
    assert repr(CORE_OLD) in str(raised.value)


async def test_the_right_core_version_answered_too_late_is_not_accepted(
    haos: Haos,
) -> None:
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    haos.supervisor.answer("GET", "/core/info", CORE_NEW)
    haos.supervisor.when("GET", "/core/info", lambda: haos.clock.advance(1.0))
    ws = await haos.connected()

    with pytest.raises(TimeoutError, match="receive exceeded its deadline"):
        await haos.run(wait_core_version, ws, "2026.8.3", timeout=1.0)


async def test_no_core_version_read_starts_after_the_reconnect_spent_the_budget(
    haos: Haos,
) -> None:
    haos.supervisor.answer("GET", "/supervisor/info", supervisor_info())
    haos.supervisor.when("GET", "/supervisor/info", lambda: haos.clock.advance(1.0))
    ws = await haos.connected()

    with pytest.raises(TimeoutError):
        await haos.run(wait_core_version, ws, "2026.8.3", timeout=1.0)

    assert ("GET", "/core/info") not in haos.supervisor.requests


async def test_a_restarting_answer_to_the_core_update_falls_through_to_polling(
    haos: Haos,
) -> None:
    _beta_channel(haos, BETA_SETTLED)
    haos.supervisor.answer("GET", "/info", root_info("running"))
    _answers(haos, "GET", "/core/info", CORE_OLD, CORE_NEW)
    haos.supervisor.restarting("POST", "/core/update")
    ws = await haos.connected()

    await haos.run(
        _configure_supervisor_image_variant,
        ws,
        base_url=haos.base_url,
        channel="beta",
        minimum_version="2026.08.0",
        core_version="2026.8.3",
    )

    assert haos.supervisor.requests[-1] == ("GET", "/core/info")
    assert haos.relay.websockets == 2


async def test_a_refused_core_update_stops_the_image_setup(haos: Haos) -> None:
    _beta_channel(haos, BETA_SETTLED)
    haos.supervisor.answer("GET", "/info", root_info("running"))
    haos.supervisor.answer("GET", "/core/info", CORE_OLD)
    haos.supervisor.refuse("POST", "/core/update", SETUP_STATE)
    ws = await haos.connected()

    with pytest.raises(WSCommandError, match="not ready"):
        await haos.run(
            _configure_supervisor_image_variant,
            ws,
            base_url=haos.base_url,
            channel="beta",
            minimum_version="2026.08.0",
            core_version="2026.8.3",
        )

    assert haos.relay.websockets == 1
