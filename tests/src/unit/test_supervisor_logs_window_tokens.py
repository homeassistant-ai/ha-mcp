"""Page tokens and ignored parameters of ``ha_get_logs`` window reads (#2974).

A ``next_offset`` token is opaque to a caller, who passes it back; these tests
drive the public tool, ``LogTools.get_logs``, over real HTTP against
``FakeGatewayd`` (``_journal_gatewayd.py``) and check what a token may be
used for: which log, which order and window it continues, what it is refused
on, and which parameters the tool flags as ignored. The paging itself is in
``test_supervisor_logs_hours_back.py``, the token's text in
``test_journal_window_reads.py``.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.api import DATA_LOGGING

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.journal_window import EPOCH, Order, WindowToken
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
from ._journal_gatewayd import proxied_client as proxied_client

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# The first line a second page of a 2-hour window of per_minute(300), in 50s.
_SECOND_PAGE_STARTS = {"oldest": "m70", "newest": "m50"}


async def first_page(client: HomeAssistantClient, order: Order) -> dict[str, Any]:
    return await ask(client, limit=50, hours_back=2, order=order)


@pytest.mark.usefixtures("clock")
class TestATokenCarriesItsRead:
    async def test_a_different_hours_back_with_a_token_is_flagged(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client, "oldest")
        second = await ask(
            in_app_client,
            limit=50,
            hours_back=9,
            order="oldest",
            offset=first["next_offset"],
        )
        assert second["hours_back"] == 2
        assert second["window_start"] == first["window_start"]
        assert "hours_back=9 does not match" in second["warnings"][0]
        assert "hours_back=2" in second["pagination_hint"]

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    async def test_a_token_alone_continues_its_read(
        self, order: Order, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # Neither hours_back nor order is repeated: both come from the token.
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client, order)
        second = await ask(in_app_client, limit=50, offset=first["next_offset"])
        assert second["hours_back"] == 2
        assert second["order"] == order
        assert "warnings" not in second
        assert second["log"].splitlines()[0] == _SECOND_PAGE_STARTS[order]
        assert f"order='{order}'" in second["pagination_hint"]

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    async def test_a_different_order_with_a_token_is_flagged(
        self, order: Order, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # Like a different hours_back, it is flagged and the token's is kept.
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client, order)
        other: Order = "newest" if order == "oldest" else "oldest"
        second = await ask(
            in_app_client, limit=50, order=other, offset=first["next_offset"]
        )
        assert second["order"] == order
        assert second["log"].splitlines()[0] == _SECOND_PAGE_STARTS[order]
        assert f"order='{other}' does not match" in second["warnings"][0]

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    @pytest.mark.parametrize(
        ("source", "slug"),
        [("system_service", "core"), ("supervisor", "core_ssh")],
    )
    async def test_a_token_for_another_log_is_refused(
        self,
        order: Order,
        source: str,
        slug: str,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # The cursor names an entry of one journal stream; read against
        # another it would continue from the wrong place without an error.
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client, order)
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
    @pytest.mark.parametrize("order", ["oldest", "newest"])
    async def test_a_token_reaches_the_journal(
        self,
        order: Order,
        source: str,
        slug: str,
        path: str,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # Oldest-first continues 5 entries after the 11th (m49): from m44.
        # Newest-first from the window's first entry (m59) with skip 30: the
        # page is the 30 entries before that position, m59..m30, shown m30 first.
        gatewayd.load(per_minute(60))
        cursor = gatewayd.cursor(10 if order == "oldest" else 0)
        skip = 5 if order == "oldest" else 30
        token = str(WindowToken(order, EPOCH, 2, path, cursor, skip))
        out = await ask(in_app_client, source=source, slug=slug, offset=token)
        first_line = "m44" if order == "oldest" else "m30"
        assert out["log"].splitlines()[0] == first_line
        assert out["order"] == order

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
        token = str(WindowToken("oldest", EPOCH, 2, "host", "s=a;i=1", 5))
        with pytest.raises(ToolError, match="Invalid 'offset' token for source='error"):
            await ask(core_client, source="error_log", slug=None, offset=token)

    async def test_a_window_token_on_the_logbook_is_refused(
        self, core_client: HomeAssistantClient
    ):
        token = str(WindowToken("oldest", EPOCH, 2, "host", "s=a;i=1", 5))
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


def warnings_of(out: dict[str, Any]) -> list[str]:
    warnings: list[str] = out.get("warnings", [])
    return warnings


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
        assert any("'hours_back' only applies" in w for w in warnings_of(out))


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
        assert not any("hours_back" in w for w in warnings_of(out))

    @pytest.mark.parametrize(
        ("hours_back", "order", "search"),
        [(None, "oldest", None), (2, "newest", None), (2, "oldest", "needle")],
    )
    async def test_offset_where_the_journal_cannot_page_is_flagged(
        self,
        hours_back: int | None,
        order: Order,
        search: str | None,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(120))
        out = await ask(
            in_app_client,
            hours_back=hours_back,
            order=order,
            search=search,
            offset=50,
            limit=50,
        )
        assert any("'offset'" in w for w in warnings_of(out))

    async def test_offset_on_an_oldest_first_window_is_not_flagged(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(120))
        out = await ask(
            in_app_client,
            source="supervisor",
            slug="core_ssh",
            hours_back=2,
            order="oldest",
            offset=50,
            limit=50,
        )
        assert not any("offset" in w for w in warnings_of(out))

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    async def test_a_token_alone_is_not_flagged(
        self, order: Order, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client, order)
        out = await ask(in_app_client, limit=50, offset=first["next_offset"])
        assert not any("offset" in w for w in warnings_of(out))

    async def test_a_token_with_a_search_is_flagged(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # A search reads the whole window at once; the token has no use there.
        gatewayd.load(per_minute(300))
        first = await first_page(in_app_client, "oldest")
        out = await ask(
            in_app_client,
            search="m",
            hours_back=2,
            limit=500,
            offset=first["next_offset"],
        )
        assert any("'offset'" in w for w in warnings_of(out))
