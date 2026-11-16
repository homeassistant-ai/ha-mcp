"""When enforce mode applies at all: the config gate, ``ha_report_issue`` and a bad config.

The middleware pair runs chained as the server registers it, on the server's
own client connected to a real ``hass``: the hidden set it computes comes from
Core's registry and states. The config-load tests write ``entity_visibility.json``
into the data dir the resolver reads and break it there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import VISIBILITY_FILENAME, save_visibility_config

from .._tool_errors import error_body
from .._visibility_core import error_code, unreached_call_next
from ._chained_enforcement import (
    _ChainedEnforcement,
    _returns,
    make_context,
    text_result,
)

_ENFORCEMENT_LOGGER = "ha_mcp.visibility.enforcement"


@pytest.fixture(autouse=True)
async def registry_api(hass: HomeAssistant) -> None:
    """``config`` answers the registry list the hidden set is computed from."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def mw(core_client: HomeAssistantClient) -> _ChainedEnforcement:
    return _ChainedEnforcement(lambda: core_client)


def report_context(*, proxied: bool = False) -> Any:
    arguments = {"fields": ["recent_logs"]}
    if proxied:
        return make_context(
            "ha_call_read_tool", {"name": "ha_report_issue", "arguments": arguments}
        )
    return make_context("ha_report_issue", arguments)


def _bypass_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.message for r in caplog.records if "bypassing ha_report_issue" in r.message
    ]


class TestInactivePassthrough:
    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(
                {
                    "enabled": False,
                    "enforce": True,
                    "deny_entity_ids": ["sensor.hidden"],
                },
                id="filter-disabled",
            ),
            pytest.param(
                {
                    "enabled": True,
                    "enforce": False,
                    "deny_entity_ids": ["sensor.hidden"],
                },
                id="enforce-off",
            ),
            pytest.param({"enabled": True, "enforce": True}, id="nothing-hidden"),
        ],
    )
    async def test_inactive_enforcement_runs_the_tool_without_reading_core(
        self,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        config: dict[str, Any],
    ) -> None:
        """A config that hides nothing costs no registry or state read."""
        set_config(**config)

        with patch.object(
            core_client, "get_states", wraps=core_client.get_states
        ) as states:
            result = await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
                _returns(text_result("body")),
            )

        assert result.content[0].text == "body"
        states.assert_not_awaited()


class TestReportIssueRestriction:
    """The diagnostics tool stays reachable unless explicitly restricted."""

    @pytest.mark.parametrize("proxied", [False, True])
    async def test_report_issue_bypasses_active_enforcement_by_default(
        self,
        core_client: HomeAssistantClient,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        proxied: bool,
    ) -> None:
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])

        with patch.object(
            core_client, "get_states", wraps=core_client.get_states
        ) as states:
            result = await mw.on_call_tool(
                report_context(proxied=proxied),
                _returns(text_result("diagnostics mention sensor.hidden")),
            )

        assert result.content[0].text == "diagnostics mention sensor.hidden"
        states.assert_not_awaited()

    async def test_report_issue_stays_reachable_while_home_assistant_is_down(
        self,
        unreachable_client: HomeAssistantClient,
        set_config: Callable[..., VisibilityConfig],
    ) -> None:
        """The diagnostic escape hatch must not touch unavailable HA inputs."""
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        mw = _ChainedEnforcement(lambda: unreachable_client)

        result = await mw.on_call_tool(
            report_context(), _returns(text_result("diagnostics remain reachable"))
        )

        assert result.content[0].text == "diagnostics remain reachable"

    async def test_report_issue_opt_in_is_inert_while_enforce_is_off(
        self, mw: _ChainedEnforcement, set_config: Callable[..., VisibilityConfig]
    ) -> None:
        set_config(
            enabled=True,
            enforce=False,
            deny_entity_ids=["sensor.hidden"],
            restrict_report_issue=True,
        )

        result = await mw.on_call_tool(
            report_context(),
            _returns(text_result("diagnostics mention sensor.hidden")),
        )

        assert result.content[0].text == "diagnostics mention sensor.hidden"

    async def test_default_bypass_is_logged(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        set_config(enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])

        with caplog.at_level("INFO", logger=_ENFORCEMENT_LOGGER):
            await mw.on_call_tool(
                report_context(),
                _returns(text_result("diagnostics mention sensor.hidden")),
            )

        assert _bypass_logs(caplog) == [
            "visibility enforce: bypassing ha_report_issue because "
            "restrict_report_issue=false"
        ]

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(
                {
                    "enabled": True,
                    "enforce": False,
                    "deny_entity_ids": ["sensor.hidden"],
                },
                id="enforce-off",
            ),
            pytest.param({"enabled": True, "enforce": True}, id="nothing-hidden"),
        ],
    )
    async def test_default_bypass_is_not_logged_while_nothing_is_enforced(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        caplog: pytest.LogCaptureFixture,
        config: dict[str, Any],
    ) -> None:
        set_config(**config)

        with caplog.at_level("INFO", logger=_ENFORCEMENT_LOGGER):
            result = await mw.on_call_tool(
                report_context(), _returns(text_result("diagnostics"))
            )

        assert result.content[0].text == "diagnostics"
        assert _bypass_logs(caplog) == []

    @pytest.mark.parametrize("proxied", [False, True])
    async def test_report_issue_is_scanned_when_operator_opts_in(
        self,
        mw: _ChainedEnforcement,
        set_config: Callable[..., VisibilityConfig],
        proxied: bool,
    ) -> None:
        set_config(
            enabled=True,
            enforce=True,
            deny_entity_ids=["sensor.hidden"],
            restrict_report_issue=True,
        )

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                report_context(proxied=proxied),
                _returns(text_result("diagnostics mention sensor.hidden")),
            )

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The data dir the resolver reads ``entity_visibility.json`` from."""
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    return tmp_path


def _save(data_dir: Path, **kwargs: Any) -> None:
    kwargs.setdefault("exclude_categories", [])
    save_visibility_config(data_dir, VisibilityConfig(**kwargs))


def _corrupt(data_dir: Path) -> None:
    (data_dir / VISIBILITY_FILENAME).write_text("{ corrupt", encoding="utf-8")


def _load_failures(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "config load failed" in r.message]


class TestConfigLoadFailure:
    async def test_corrupt_config_with_no_prior_load_refuses(
        self, mw: _ChainedEnforcement, data_dir: Path
    ) -> None:
        """A corrupt file neither crashes every call nor switches enforcement off.

        With no last-known-good config the call fails closed with the
        structured enforce-mode error naming the file to fix.
        """
        _corrupt(data_dir)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                unreached_call_next,
            )

        body = error_body(exc)
        assert body["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert VISIBILITY_FILENAME in body["error"]["message"]

    async def test_corrupt_config_with_no_prior_load_keeps_report_issue_available(
        self,
        mw: _ChainedEnforcement,
        data_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _corrupt(data_dir)

        with caplog.at_level("WARNING", logger=_ENFORCEMENT_LOGGER):
            result = await mw.on_call_tool(
                report_context(), _returns(text_result("diagnostics still available"))
            )

        assert result.content[0].text == "diagnostics still available"
        assert [r.message for r in _load_failures(caplog)] == [
            "visibility enforce: config load failed; allowing ha_report_issue because "
            "its diagnostic exemption defaults to unrestricted"
        ]

    async def test_corrupt_config_after_enforce_off_load_passes_through(
        self,
        mw: _ChainedEnforcement,
        data_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-enforce install whose file breaks mid-session keeps working."""
        _save(data_dir)
        first = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.any"}),
            _returns(text_result("first")),
        )
        assert first.content[0].text == "first"
        _corrupt(data_dir)

        with caplog.at_level("WARNING", logger=_ENFORCEMENT_LOGGER):
            second = await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                _returns(text_result("second")),
            )

        assert second.content[0].text == "second"
        warnings = _load_failures(caplog)
        assert [r.message for r in warnings] == [
            "visibility enforce: config load failed; using last-known-good config"
        ]
        assert warnings[0].exc_info is not None

    async def test_config_breaking_between_the_two_halves_is_logged_once(
        self,
        mw: _ChainedEnforcement,
        data_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The outbound half reports its own load failure with the traceback."""
        _save(data_dir)
        await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.any"}),
            _returns(text_result("prime")),
        )

        async def _break_file_then_run_outbound(context: Any) -> Any:
            _corrupt(data_dir)
            return await mw.outbound.on_call_tool(
                context, _returns(text_result("second"))
            )

        with caplog.at_level("WARNING", logger=_ENFORCEMENT_LOGGER):
            result = await mw.inbound.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.any"}),
                _break_file_then_run_outbound,
            )

        assert result.content[0].text == "second"
        warnings = _load_failures(caplog)
        assert [r.message for r in warnings] == [
            "visibility enforce: config load failed; using last-known-good config"
        ]
        assert warnings[0].exc_info is not None

    async def test_corrupt_config_after_enforce_on_load_stays_enforced(
        self, mw: _ChainedEnforcement, data_dir: Path
    ) -> None:
        """An enforce install whose file breaks mid-session keeps concealing."""
        _save(data_dir, enabled=True, enforce=True, deny_entity_ids=["sensor.hidden"])
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.visible"}),
            _returns(text_result("fine")),
        )
        assert result.content[0].text == "fine"
        _corrupt(data_dir)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "sensor.hidden"}),
                unreached_call_next,
            )

        assert error_code(exc) == "ENTITY_NOT_FOUND"

    async def test_corrupt_config_after_report_opt_in_stays_enforced(
        self, mw: _ChainedEnforcement, data_dir: Path
    ) -> None:
        _save(
            data_dir,
            enabled=True,
            enforce=True,
            deny_entity_ids=["sensor.hidden"],
            restrict_report_issue=True,
        )
        result = await mw.on_call_tool(
            report_context(), _returns(text_result("clean diagnostics"))
        )
        assert result.content[0].text == "clean diagnostics"
        _corrupt(data_dir)

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(
                report_context(),
                _returns(text_result("diagnostics mention sensor.hidden")),
            )

        assert error_code(exc) == "ENTITY_VISIBILITY_ENFORCED"
