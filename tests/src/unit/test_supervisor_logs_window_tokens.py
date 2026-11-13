"""Page tokens and ignored parameters of ``ha_get_logs`` window reads (#2974).

A ``next_offset`` token is opaque to a caller, who passes it back; these tests
drive the public tool, ``LogTools.get_logs``, over real HTTP against
``FakeGatewayd`` (``_journal_gatewayd.py``) and check what a token may be
used for: which log and window it continues, what it is refused
on, and which parameters the tool flags as ignored. The paging itself is in
``test_supervisor_logs_hours_back.py``, the token's text in
``test_journal_window_reads.py``.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.api import DATA_LOGGING

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.journal_window import EPOCH, WindowToken
from ha_mcp.client.rest_client import HomeAssistantClient

from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._journal_gatewayd import (
    FakeGatewayd,
    ask,
    per_minute,
)
from ._journal_gatewayd import (
    clock as clock,
)
from ._journal_gatewayd import gatewayd as gatewayd
from ._journal_gatewayd import in_app_client as in_app_client
from ._journal_gatewayd import journal_proxy as journal_proxy

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def first_page(client: HomeAssistantClient) -> dict[str, Any]:
    """The first 50 entries of a 2-hour window; per_minute(300) continues at m70."""
    return await ask(client, limit=50, hours_back=2)


@pytest.mark.usefixtures("clock")
class TestATokenCarriesItsRead:
    async def test_a_different_hours_back_with_a_token_is_flagged(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client)
        second = await ask(
            in_app_client,
            limit=50,
            hours_back=9,
            offset=first["next_offset"],
        )
        assert second["hours_back"] == 2
        assert second["window_start"] == first["window_start"]
        assert "hours_back=9 does not match" in second["warnings"][0]
        assert "hours_back=2" in second["pagination_hint"]

    async def test_a_token_alone_continues_its_read(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # hours_back is not repeated: it comes from the token, unflagged.
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client)
        second = await ask(in_app_client, limit=50, offset=first["next_offset"])
        assert second["hours_back"] == 2
        assert second["order"] == "oldest"
        assert "warnings" not in second
        assert second["log"].splitlines()[0] == "m70"
        assert "hours_back=2" in second["pagination_hint"]

    @pytest.mark.parametrize(
        ("source", "slug"),
        [("system_service", "core"), ("supervisor", "core_ssh")],
    )
    async def test_a_token_for_another_log_is_refused(
        self,
        source: str,
        slug: str,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # The cursor names an entry of one journal stream; read against
        # another it would continue from the wrong place without an error.
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client)
        sent = len(gatewayd.requests)
        with pytest.raises(ToolError, match="continues the log of 'host'") as raised:
            await ask(
                in_app_client,
                source=source,
                slug=slug,
                limit=50,
                offset=first["next_offset"],
            )
        assert "VALIDATION_INVALID_PARAMETER" in str(raised.value)
        assert len(gatewayd.requests) == sent  # refused before any read

    @pytest.mark.parametrize(
        ("source", "slug", "path"),
        [
            ("supervisor", "core_ssh", "addons/core_ssh"),
            ("system_service", "host", "host"),
        ],
    )
    async def test_a_token_reaches_the_journal(
        self,
        source: str,
        slug: str,
        path: str,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # The page continues 5 entries after the 11th (m49): from m44.
        gatewayd.load(per_minute(60))
        token = str(WindowToken(EPOCH, 2, path, gatewayd.cursor(10), 5))
        out = await ask(in_app_client, source=source, slug=slug, offset=token)
        assert out["log"].splitlines()[0] == "m44"
        assert gatewayd.requests[-1].path.endswith(f"/{path}/logs")

    async def test_a_malformed_token_is_refused(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        with pytest.raises(ToolError, match="Invalid 'offset' token"):
            await ask(in_app_client, hours_back=2, offset="window:x")
        assert gatewayd.requests == []


class TestATokenOnTheWrongSource:
    """Each source takes only the token it returns; nothing is read first."""

    async def test_a_window_token_on_the_error_log_is_refused(
        self, core_client: HomeAssistantClient
    ):
        token = str(WindowToken(EPOCH, 2, "host", "s=a;i=1", 5))
        with pytest.raises(ToolError, match="Invalid 'offset' token for source='error"):
            await ask(core_client, source="error_log", slug=None, offset=token)

    async def test_a_window_token_on_the_logbook_is_refused(
        self, core_client: HomeAssistantClient
    ):
        token = str(WindowToken(EPOCH, 2, "host", "s=a;i=1", 5))
        with pytest.raises(ToolError, match="Cursor tokens only apply"):
            await ask(core_client, source="logbook", slug=None, offset=token)

    @pytest.mark.parametrize(
        ("source", "slug"), [("supervisor", "core_ssh"), ("system_service", "host")]
    )
    async def test_an_error_log_token_on_a_journal_source_is_refused(
        self,
        source: str,
        slug: str,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        with pytest.raises(
            ToolError, match=f"Invalid 'offset' token for source='{source}'"
        ):
            await ask(
                in_app_client,
                source=source,
                slug=slug,
                hours_back=2,
                offset="cursor:s=a;i=1:0",
            )
        assert gatewayd.requests == []


@pytest.fixture
def error_log_file(hass: HomeAssistant, tmp_path: Path) -> None:
    """Name the file Core serves at /api/error_log, as its logging setup does.

    Core registers the route only when the file is named, so a test takes this
    before ``core_client``.
    """
    log = tmp_path / "home-assistant.log"
    log.write_text("a line\n", encoding="utf-8")
    hass.data[DATA_LOGGING] = str(log)


class TestIgnoredParameterOnCore:
    """Real time: Core's own error log read has timeouts that a frozen clock stops."""

    async def test_hours_back_on_a_source_that_ignores_it_is_flagged(
        self, error_log_file: None, core_client: HomeAssistantClient
    ):
        out = await ask(core_client, source="error_log", slug=None, hours_back=3)
        assert any("'hours_back' only applies" in w for w in out.get("warnings", []))


@pytest.mark.usefixtures("clock")
class TestIgnoredParameterWarning:
    @pytest.mark.parametrize(
        ("source", "slug"), [("supervisor", "core_ssh"), ("system_service", "host")]
    )
    async def test_hours_back_where_it_applies_is_not_flagged(
        self,
        source: str,
        slug: str,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(10))
        out = await ask(in_app_client, source=source, slug=slug, hours_back=3)
        assert not any("hours_back" in w for w in out.get("warnings", []))

    @pytest.mark.parametrize(("hours_back", "search"), [(None, None), (2, "needle")])
    async def test_offset_where_the_journal_cannot_page_is_flagged(
        self,
        hours_back: int | None,
        search: str | None,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(120))
        out = await ask(
            in_app_client,
            hours_back=hours_back,
            search=search,
            offset=50,
            limit=50,
        )
        assert any("'offset'" in w for w in out.get("warnings", []))

    async def test_an_integer_offset_on_a_page_read_is_not_flagged(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(120))
        out = await ask(
            in_app_client,
            source="supervisor",
            slug="core_ssh",
            hours_back=2,
            offset=50,
            limit=50,
        )
        assert not any("offset" in w for w in out.get("warnings", []))

    async def test_a_token_with_a_search_is_flagged(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # A search reads the whole window at once; the token has no use there.
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client)
        out = await ask(
            in_app_client,
            search="m",
            hours_back=2,
            limit=500,
            offset=first["next_offset"],
        )
        assert any("'offset'" in w for w in out.get("warnings", []))
