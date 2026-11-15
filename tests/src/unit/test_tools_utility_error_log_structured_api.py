"""``ha_get_logs(source='error_log')`` parameters, warnings and failures.

Covers how the tool takes ``structured``, ``top_n``, ``limit``, ``level`` and
``offset``, the warnings it adds when a parameter does not apply, how an empty
fetch is reported, and how a refused token surfaces. The summary's content is in
``test_tools_utility_error_log_structured.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._error_log_core import (
    call_logs,
    error_log,
    expired_token_client,
    numbered_log,
    serve_log,
    structured,
)
from ._error_log_core import log_file as log_file
from ._error_log_samples import (
    EMPTY_LOG,
    SAMPLE_LOG,
    UNPARSEABLE_LOG,
)
from ._tool_errors import rejected_call_body


@pytest.fixture
async def core_logs(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Core's ``system_log`` and ``logger`` answering ``system`` and ``logger``."""
    assert await async_setup_component(hass, "system_log", {})
    assert await async_setup_component(hass, "logger", {})


@pytest.fixture
async def logbook(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's logbook on a recorder, as the tool reads it."""
    # The logbook registers a sidebar panel, which needs the frontend build.
    mock_integration(hass, MockModule("frontend"))
    assert await async_setup_component(hass, "logbook", {})
    await async_wait_recording_done(hass)


class TestStructuredParameter:
    async def test_structured_true_returns_parsed_response(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, core_client, SAMPLE_LOG, structured=True)
        assert result["success"] is True
        assert result["structured"] is True
        assert "top_issues" in result
        assert "by_component" in result
        assert "log" not in result

    async def test_structured_false_returns_raw_log(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, core_client, SAMPLE_LOG, structured=False)
        assert "Device timeout" in result["log"]
        assert "top_issues" not in result

    async def test_structured_default_is_false(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, core_client, SAMPLE_LOG)
        assert "Device timeout" in result["log"]
        assert "top_issues" not in result

    @pytest.mark.parametrize(
        ("raw_value", "summary"), [("true", True), ("false", False)]
    )
    async def test_structured_string_is_coerced_by_the_schema(
        self,
        log_file: Path,
        core_client: HomeAssistantClient,
        raw_value: str,
        summary: bool,
    ) -> None:
        """AI tools often pass booleans as strings; "false" must not read as true."""
        result = await error_log(
            log_file, core_client, SAMPLE_LOG, structured=raw_value
        )
        assert ("top_issues" in result) is summary
        assert ("log" in result) is not summary

    async def test_structured_on_non_error_log_source_emits_warning(
        self, core_logs: None, core_client: HomeAssistantClient
    ) -> None:
        """structured=True on source='system' warns and is ignored."""
        logging.getLogger("homeassistant.components.zha").error("Device timeout")
        result = await call_logs(core_client, source="system", structured=True)
        assert any("structured" in w for w in result["warnings"])
        assert [e["message"] for e in result["entries"]] == [["Device timeout"]]

    async def test_structured_on_logbook_emits_warning(
        self, logbook: None, core_client: HomeAssistantClient
    ) -> None:
        result = await call_logs(core_client, source="logbook", structured=True)
        assert any("structured" in w for w in result["warnings"])
        assert "top_issues" not in result


class TestTopNParameter:
    async def test_top_n_without_structured_emits_warning(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """top_n on the raw path does nothing, the obvious caller mistake.

        The warning must name the piece that is missing: the source is already
        right, so blaming it would tell the caller their correct argument is
        the problem.
        """
        result = await error_log(log_file, core_client, SAMPLE_LOG, top_n=5)
        assert "Device timeout" in result["log"]
        warning = next(w for w in result["warnings"] if "top_n" in w)
        assert "ignored because structured=False" in warning
        assert "ignored for source=" not in warning

    async def test_top_n_on_other_source_emits_warning(
        self, core_logs: None, core_client: HomeAssistantClient
    ) -> None:
        result = await call_logs(core_client, source="system", top_n=5)
        # Here the source IS the problem, so the tail must still name it.
        warning = next(w for w in result["warnings"] if "top_n" in w)
        assert "ignored for source='system'" in warning

    async def test_top_n_in_structured_mode_does_not_warn(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, core_client, SAMPLE_LOG, top_n=5)
        assert len(result["top_issues"]) == 5
        assert not any("top_n" in w for w in result.get("warnings", []))

    async def test_a_top_n_below_one_is_refused_by_name(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """The refusal must name the parameter the caller passed, not `limit`."""
        await serve_log(log_file, SAMPLE_LOG)
        with pytest.raises(ToolError, match="top_n") as refused:
            await call_logs(core_client, source="error_log", structured=True, top_n=0)
        assert "limit" not in str(refused.value)


class TestLimitAndLevel:
    async def test_limit_zero_is_accepted_in_structured_mode(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """`limit` is validated only on the raw path, and this pins why.

        The summary ranks the whole fetched window, so `limit` has no meaning
        there and validating it would reject a value that changes nothing.
        """
        result = await structured(log_file, core_client, SAMPLE_LOG, limit=0)
        assert result["structured"] is True
        assert result["summary"]["parsed_entries"] == 8
        # Accepted, but not silently: the caller still learns it did nothing.
        assert any("limit" in w for w in result["warnings"])

    async def test_limit_zero_is_still_rejected_on_the_raw_path(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """The counterpart: where `limit` does apply, 0 is still invalid."""
        await serve_log(log_file, SAMPLE_LOG)
        with pytest.raises(ToolError):
            await call_logs(core_client, source="error_log", limit=0)

    async def test_level_critical_is_accepted(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """CRITICAL is a valid level, so CRITICAL lines can be isolated."""
        result = await structured(log_file, core_client, SAMPLE_LOG, level="CRITICAL")
        assert [i["message"] for i in result["top_issues"]] == ["DB error"]


class TestRefusedToken:
    async def test_auth_error_surfaces_as_structured_tool_error(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """A 401 must not escape raw.

        ``HomeAssistantAuthError`` is a sibling of ``HomeAssistantAPIError``, not
        a subclass, so an ``except (HomeAssistantAPIError, ...)`` tuple lets it
        through to FastMCP without a structured ``code``.
        """
        await serve_log(log_file, SAMPLE_LOG)
        async with expired_token_client(core_client) as stale:
            body = await rejected_call_body(call_logs(stale, source="error_log"))
        assert body["success"] is False
        assert body["error"]["code"]
        assert body.get("source") == "error_log"

    async def test_expired_token_on_a_supervised_install_reaches_the_auth_handler(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A 401 on the install-class probe must reach the auth handler.

        With no log file Core registers no ``/api/error_log``, as under the
        Supervisor. The probe used to swallow the 401 and answer "not
        supervised", so the call fell through to that missing route and the
        dead token came back as a 404 with connection advice.
        """
        async with expired_token_client(core_client) as stale:
            body = await rejected_call_body(call_logs(stale, source="error_log"))
        assert body["success"] is False
        assert body["error"]["code"] == "AUTH_INVALID_TOKEN"
        assert "404" not in str(body)


class TestWarningsAreMergedNotClobbered:
    """Three layers can warn on one structured call; none may overwrite another.

    The parser (format drift), the structured builder (ignored limit/order) and
    ``get_logs`` (wrong-source params) each append to the same top-level list.
    An overwrite silently drops the "this summary is NOT an all-clear" notice,
    the one warning that must never be lost.
    """

    async def test_drift_warning_survives_ignored_param_warning(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, core_client, UNPARSEABLE_LOG, limit=5, order="oldest"
        )
        joined = " ".join(result["warnings"])
        assert "NOT evidence" in joined
        assert "do not apply when structured=True" in joined

    async def test_wrong_source_param_warning_does_not_clobber_drift(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, core_client, UNPARSEABLE_LOG, entity_id="light.kitchen"
        )
        joined = " ".join(result["warnings"])
        assert "only apply to source='logbook'" in joined
        assert "NOT evidence" in joined


class TestEmptyFetch:
    @pytest.mark.parametrize("raw", [EMPTY_LOG, "\n\n   \n"])
    async def test_empty_fetch_on_the_default_path_warns_too(
        self, log_file: Path, core_client: HomeAssistantClient, raw: str
    ) -> None:
        """The same fetch must not be blessed by one mode and refused by the other.

        `structured=False` is the default, so most agents are on this path: an
        empty fetch there used to answer success with an empty `log`, which
        reads back to the user as "no errors in the log".
        """
        result = await error_log(log_file, core_client, raw)
        assert result["success"] is True
        assert result["log"].strip() == ""
        assert "empty or failed fetch" in result["warnings"][0]
        assert "NOT evidence" in result["warnings"][0]

    async def test_empty_filter_result_is_not_reported_as_an_empty_fetch(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """An empty *filter result* keeps its own story.

        The fetch worked and the filter excluded everything, which
        `filters_applied` already discloses; warning about a failed fetch would
        send the caller after an infrastructure problem that does not exist.
        """
        result = await error_log(
            log_file, core_client, SAMPLE_LOG, search="nothing-matches-this"
        )
        assert result["total_lines"] == 0
        assert result["filters_applied"] == {"search": "nothing-matches-this"}
        assert not any("empty or failed fetch" in w for w in result.get("warnings", []))


class TestOffsetParameterIncompatibilityWarning:
    """`offset` applies to source='logbook' and source='error_log'. Anywhere
    else it must be flagged rather than silently dropped."""

    @pytest.mark.parametrize("source", ["system", "logger"])
    async def test_offset_on_an_unsupported_source_warns(
        self, core_logs: None, core_client: HomeAssistantClient, source: str
    ) -> None:
        result = await call_logs(core_client, source=source, offset=50)
        warnings = result.get("warnings", [])
        assert any("offset" in w and source in w for w in warnings), warnings

    @pytest.mark.parametrize("structured_mode", [False, True])
    async def test_offset_on_error_log_does_not_warn(
        self,
        log_file: Path,
        core_client: HomeAssistantClient,
        structured_mode: bool,
    ) -> None:
        result = await error_log(
            log_file,
            core_client,
            numbered_log(100),
            structured=structured_mode,
            offset=50,
        )
        assert not any("offset" in w for w in result.get("warnings", []))

    async def test_zero_offset_never_warns(
        self, core_logs: None, core_client: HomeAssistantClient
    ) -> None:
        """The default stays silent: a warning on every `system` call would
        train agents to ignore the channel."""
        result = await call_logs(core_client, source="system")
        assert not any("offset" in w for w in result.get("warnings", []))
