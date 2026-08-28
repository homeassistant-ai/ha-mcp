"""Unit tests for ``HomeAssistantClient.get_error_log`` three-way branch.

The three branches:

- **Addon context** (``is_running_in_addon()`` True) — Supervisor REST.
  Covered by existing tests for ``_supervisor_logs_get``; we just
  regression-check that the branch is still entered.
- **External → Supervised/HAOS** (probe of ``/api/config`` returns
  ``"hassio" in components``) — HA Core hassio proxy at
  ``/api/hassio/core/logs``. New branch added in #1349 item 4 fix.
- **External → Container/pip** (no hassio in components) — historical
  ``/api/error_log`` path, now using ``_raw_request`` so plain-text
  responses aren't lossily JSON-parsed.

Also covers the ``_is_supervised_install`` cache invariant: both
positive and negative outcomes of a successful probe are cached
(definitive signals), only probe FAILURES leave the cache unset so the
next call can re-probe.

Every branch must fetch the caller's window and nothing more (#2279): an
unconditional 20,000-line journald request hung the call for 15+ minutes on a
Supervisor-backed install.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.client.rest_client import (
    MIN_LOG_WINDOW_LINES,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)

# Any bounded window; the point of every assertion below is that the request
# carries THIS number rather than a hard-coded one of the client's own.
_WINDOW = 100


@pytest.fixture
def client():
    """``HomeAssistantClient`` with stubbed internals — no real network.

    Mirrors the fixture pattern in ``test_tools_utility_supervisor_logs.py``;
    sets every attribute the real ``__init__`` sets so production code can
    use direct attribute access (no defensive ``getattr`` needed to paper
    over a test-fixture omission).
    """
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        c = HomeAssistantClient()
        c.base_url = "http://test.local:8123"
        c.token = "test-token"
        c.timeout = 30
        c.verify_ssl = True
        c.httpx_client = MagicMock()
        c._supervised_detected = None
        return c


@pytest.fixture
def supervisor_http():
    """Capture the wire-level request the direct-Supervisor branch issues.

    Patches ``httpx.AsyncClient`` (what ``make_supervisor_httpx_client``
    constructs), so the assertions see the real headers and query params rather
    than a stubbed ``_supervisor_logs_get`` call signature.
    """
    inner = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.text = "addon-log-content"
    inner.get = AsyncMock(return_value=response)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner)
    cm.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("httpx.AsyncClient", return_value=cm),
        patch.dict("os.environ", {"SUPERVISOR_TOKEN": "supervisor-token-test"}),
    ):
        yield inner


# ----- journald Range window -----


class TestJournaldRangeHeader:
    """``Range: entries=<cursor>:<num_skip>:<num_entries>`` — the only syntax
    Supervisor offers for an offset window (``?lines=`` can only take the
    newest N)."""

    def test_newest_window_matches_supervisors_own_lines_translation(self):
        """Supervisor builds ``entries=:-{N-1}:{N}`` for its own ``?lines=N``."""
        assert HomeAssistantClient._journald_range_header(100, 0) == "entries=:-99:100"

    def test_offset_moves_the_window_further_back(self):
        """The window ends ``offset`` entries before the newest one."""
        assert (
            HomeAssistantClient._journald_range_header(100, 250) == "entries=:-349:100"
        )

    def test_one_entry_window_is_expressible_only_with_an_offset(self):
        """The probe's shape: a single entry, ``offset`` back from the newest.

        At offset 0 the same formula degenerates to ``entries=:0:1``, whose
        non-negative skip reads from the OLDEST entry — which is why callers
        anchoring at the newest end floor their window instead.
        """
        assert HomeAssistantClient._journald_range_header(1, 250) == "entries=:-250:1"
        assert HomeAssistantClient._journald_range_header(1, 0) == "entries=:0:1"


@pytest.mark.asyncio
async def test_get_error_log_floors_a_one_line_window(client, supervisor_http):
    """Every route serves at least ``MIN_LOG_WINDOW_LINES``.

    Supervisor coerces its own ``?lines=1`` to 2 because the Range syntax
    cannot anchor a one-entry window at the newest end; flooring in one place
    keeps container installs from answering with a different slice than
    Supervisor-backed ones for the same call.
    """
    assert MIN_LOG_WINDOW_LINES == 2
    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=True):
        await client.get_error_log(lines=1)

    _, kwargs = supervisor_http.get.call_args
    assert kwargs["headers"]["Range"] == "entries=:-1:2"


# ----- _is_supervised_install probe semantics -----


@pytest.mark.asyncio
async def test_is_supervised_returns_true_when_hassio_loaded(client):
    """`hassio` in /api/config["components"] → True, cached."""
    client._request = AsyncMock(return_value={"components": ["sun", "hassio", "demo"]})
    assert await client._is_supervised_install() is True
    assert client._supervised_detected is True


@pytest.mark.asyncio
async def test_is_supervised_returns_false_when_hassio_absent_and_caches(client):
    """`hassio` absent → False, CACHED (definitive non-supervised signal).

    A successful /api/config response that doesn't list hassio is a
    definitive Container/pip signal — caching it avoids re-probing on
    every subsequent get_error_log call. Only probe FAILURES leave the
    cache unset (see test_is_supervised_fails_open_on_probe_error).
    """
    client._request = AsyncMock(return_value={"components": ["sun", "demo"]})
    assert await client._is_supervised_install() is False
    assert client._supervised_detected is False
    # Second call must NOT re-probe — cached negative is reused.
    assert await client._is_supervised_install() is False
    assert client._request.await_count == 1


@pytest.mark.asyncio
async def test_is_supervised_caches_positive_result(client):
    """One probe per session once True — repeated calls don't re-GET /config."""
    client._request = AsyncMock(return_value={"components": ["hassio"]})
    assert await client._is_supervised_install() is True
    assert await client._is_supervised_install() is True
    assert await client._is_supervised_install() is True
    assert client._request.await_count == 1


@pytest.mark.asyncio
async def test_is_supervised_fails_open_on_probe_error(client):
    """Probe transport-layer error → False, NOT cached (next call re-probes)."""
    client._request = AsyncMock(side_effect=httpx.ConnectError("boom"))
    assert await client._is_supervised_install() is False
    assert client._supervised_detected is None


@pytest.mark.asyncio
async def test_is_supervised_fails_open_on_ha_api_error(client):
    """Probe HomeAssistantAPIError (non-2xx) → False, not cached."""
    client._request = AsyncMock(
        side_effect=HomeAssistantAPIError("503 Service Unavailable")
    )
    assert await client._is_supervised_install() is False
    assert client._supervised_detected is None


@pytest.mark.asyncio
async def test_is_supervised_propagates_auth_error(client):
    """A 401 on the probe must NOT fail open — it is a verdict, not a glitch.

    Failing open here sends a Supervised install down the Container
    branch to ``/api/error_log``, a route HA Core never registers under
    ``SUPERVISOR``. The 401 would come back as a 404, and the caller
    would answer a dead token with "check your connection".
    """
    client._request = AsyncMock(side_effect=HomeAssistantAuthError("401 Unauthorized"))
    with pytest.raises(HomeAssistantAuthError):
        await client._is_supervised_install()
    # Nothing definitive was learned about the install class, so the next
    # call with a working token must still be able to probe.
    assert client._supervised_detected is None


@pytest.mark.asyncio
async def test_is_supervised_propagates_runtime_bugs(client):
    """Programming errors (TypeError, etc.) must NOT be swallowed by fail-open.

    Narrow-except contract: only catch HTTP/transport layer errors;
    runtime bugs like ``TypeError`` from a misshaped mock or
    ``AttributeError`` from a misuse signal real issues and must surface
    loudly.
    """
    client._request = AsyncMock(side_effect=TypeError("oops"))
    with pytest.raises(TypeError):
        await client._is_supervised_install()


@pytest.mark.asyncio
async def test_is_supervised_handles_unexpected_response_shape(client):
    """Probe returns dict without `components` key → False (cached).

    ``_request`` returns ``{}`` on a JSON-empty response (rest_client.py
    line 240); that's the realistic edge case. The branch defensively
    handles missing or wrong-typed ``components`` without raising.
    """
    client._request = AsyncMock(return_value={})
    assert await client._is_supervised_install() is False
    # Empty-dict response IS a successful probe — counts as definitive non-supervised.
    assert client._supervised_detected is False

    # Wrong-typed components — also fail-closed without raising.
    client2_response = {"components": "not a list"}
    client._supervised_detected = None
    client._request = AsyncMock(return_value=client2_response)
    assert await client._is_supervised_install() is False
    assert client._supervised_detected is False


# ----- get_error_log three-way branch -----


@pytest.mark.asyncio
async def test_get_error_log_addon_branch(client, supervisor_http):
    """Addon (`is_running_in_addon()` True) → Supervisor REST, windowed.

    Asserted on the wire rather than on ``_supervisor_logs_get``'s signature:
    Supervisor checks ``if "lines" in request.query`` first and ignores the
    Range header whenever the query param is present, so sending both would
    silently serve the wrong window.
    """
    client._request = AsyncMock()
    client._raw_request = AsyncMock()

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=True):
        page = await client.get_error_log(lines=_WINDOW, offset=250)

    assert page.text == "addon-log-content"
    args, kwargs = supervisor_http.get.call_args
    assert args[0] == "/core/logs"
    assert kwargs["headers"]["Range"] == f"entries=:-349:{_WINDOW}"
    assert kwargs["params"] is None, "a `lines` query param would win over Range"
    # Must NOT have touched the external-branch paths.
    client._request.assert_not_called()
    client._raw_request.assert_not_called()


@pytest.mark.asyncio
async def test_get_error_log_addon_branch_never_requests_the_old_fixed_window(
    client, supervisor_http
):
    """The 20,000-line window is gone — the request carries the caller's."""
    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=True):
        await client.get_error_log(lines=_WINDOW)

    _, kwargs = supervisor_http.get.call_args
    assert "20000" not in kwargs["headers"]["Range"]
    assert kwargs["headers"]["Range"] == f"entries=:-99:{_WINDOW}"


@pytest.mark.asyncio
async def test_get_error_log_supervised_external_branch(client):
    """External + hassio loaded → `/hassio/core/logs` via _raw_request.

    HA Core's hassio proxy forwards the ``Range`` header for log paths
    (``PATHS_LOGS`` in ``homeassistant/components/hassio/http.py``), so the
    proxied route requests the same window the add-on route does — and no
    ``?lines=``, which would take precedence over it at the Supervisor end.
    """
    response = SimpleNamespace(text="supervised-log-content")
    client._raw_request = AsyncMock(return_value=response)
    client._request = AsyncMock(return_value={"components": ["hassio", "sun"]})
    client._supervisor_logs_get = AsyncMock()

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        page = await client.get_error_log(lines=_WINDOW, offset=250)

    assert page.text == "supervised-log-content"
    # /api/config probe (1) + /hassio/core/logs fetch (1).
    assert client._request.await_count == 1
    client._request.assert_awaited_with("GET", "/config")
    client._raw_request.assert_awaited_once_with(
        "GET",
        "/hassio/core/logs",
        headers={"Accept": "text/plain", "Range": f"entries=:-349:{_WINDOW}"},
    )
    endpoint = client._raw_request.call_args.args[1]
    assert "lines=" not in endpoint
    assert "20000" not in endpoint
    # Must NOT have entered the addon branch.
    client._supervisor_logs_get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
@pytest.mark.parametrize("route", ["addon_logs", "system_service"])
async def test_proxied_log_routes_share_the_overall_deadline(client, route):
    """The hassio-proxy log routes are hang-proof for the same reason.

    ``get_addon_logs`` and ``_get_system_service_logs`` proxy branches ride the
    same per-I/O httpx timeout that let #2279 trickle forever, so they carry
    the same wall-clock deadline as ``get_error_log``.
    """
    client.timeout = 0.01

    async def _hang_forever(*_args, **_kwargs):
        await asyncio.Event().wait()

    client._raw_request = AsyncMock(side_effect=_hang_forever)

    with (
        patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False),
        pytest.raises(HomeAssistantConnectionError) as exc_info,
    ):
        if route == "addon_logs":
            await client.get_addon_logs("core_mosquitto", lines=5)
        else:
            await client._get_system_service_logs("host", lines=5)

    assert "after 0.01s" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.timeout(10)
@pytest.mark.parametrize("supervised", [True, False])
async def test_get_error_log_has_one_overall_deadline(client, supervised):
    """A server that stalls mid-body must fail fast on every route.

    ``httpx.Timeout`` applies per I/O operation, so a peer trickling bytes
    while it assembles the window resets the read timeout forever and no
    per-attempt timeout ever fires — the mechanism behind #2279's 15-minute
    hang. Only a deadline over the whole call bounds it, and it has to cover
    the container route too, which still transfers the entire log file.
    """
    client.timeout = 0.01
    components = ["hassio"] if supervised else ["sun"]
    client._request = AsyncMock(return_value={"components": components})

    async def _hang_forever(*_args, **_kwargs):
        await asyncio.Event().wait()

    client._raw_request = AsyncMock(side_effect=_hang_forever)

    with (
        patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False),
        pytest.raises(HomeAssistantConnectionError) as exc_info,
    ):
        await client.get_error_log(lines=_WINDOW)

    assert "after 0.01s: TimeoutError" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_get_error_log_deadline_covers_the_install_class_probe(client):
    """The probe is inside the deadline, not before it.

    A stalled ``/api/config`` is the same hang with an earlier trigger.
    """
    client.timeout = 0.01

    async def _hang_forever(*_args, **_kwargs):
        await asyncio.Event().wait()

    client._request = AsyncMock(side_effect=_hang_forever)
    client._raw_request = AsyncMock()

    with (
        patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False),
        pytest.raises(HomeAssistantConnectionError) as exc_info,
    ):
        await client.get_error_log(lines=_WINDOW)

    assert "after 0.01s" in str(exc_info.value)
    client._raw_request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_get_error_log_deadline_allows_a_gateway_retry(client):
    """A transient 502 must still recover, not become a hard timeout.

    ``_raw_request`` backs off and retries 502/503/504 for safe methods. That
    retry now runs inside the overall deadline, so this pins that a gateway
    blip still resolves — the hang fix must not trade away the recovery it
    sits on top of.
    """
    client.timeout = 30
    client._request = AsyncMock(return_value={"components": ["sun"]})

    bad_gateway = MagicMock(status_code=502)
    bad_gateway.json.return_value = {"message": "Bad Gateway"}
    recovered = MagicMock(status_code=200)
    recovered.text = "recovered line\n"
    client.httpx_client.request = AsyncMock(side_effect=[bad_gateway, recovered])

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        page = await client.get_error_log(lines=_WINDOW)

    assert page.text == "recovered line"
    assert client.httpx_client.request.await_count == 2


@pytest.mark.asyncio
async def test_get_error_log_container_external_branch(client):
    """External + hassio NOT loaded → ``/error_log`` via _raw_request.

    The Container/pip branch must use ``_raw_request`` so the plain-text
    response from ``/api/error_log`` reaches the caller verbatim. The
    older implementation used ``_request`` which JSON-parses the body and
    silently returned ``"{}"`` on the JSONDecodeError fallback — fixed
    by the Gemini-flagged HIGH-priority bug in PR #1360.
    """
    container_response = SimpleNamespace(text="real container log line\n")
    client._request = AsyncMock(return_value={"components": ["sun", "demo"]})
    client._raw_request = AsyncMock(return_value=container_response)
    client._supervisor_logs_get = AsyncMock()

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        page = await client.get_error_log(lines=_WINDOW)

    assert page.text == "real container log line"
    # Probe via _request, log fetch via _raw_request — symmetry with supervised branch.
    client._request.assert_awaited_once_with("GET", "/config")
    client._raw_request.assert_awaited_once_with(
        "GET", "/error_log", headers={"Accept": "text/plain"}
    )
    client._supervisor_logs_get.assert_not_called()


class TestContainerBranchWindowing:
    """``/api/error_log`` serves the whole file with no Range support, so the
    window is applied client-side — same tail semantics as the journald Range
    the Supervisor-backed branches send, keeping the tool layer
    install-agnostic."""

    @pytest.fixture
    def container_client(self, client):
        whole_file = "".join(f"line{i}\n" for i in range(10))
        client._request = AsyncMock(return_value={"components": ["sun"]})
        client._raw_request = AsyncMock(return_value=SimpleNamespace(text=whole_file))
        return client

    async def _fetch(self, container_client, **kwargs):
        with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
            return await container_client.get_error_log(**kwargs)

    @pytest.mark.asyncio
    async def test_offset_window_is_taken_from_the_tail(self, container_client):
        """lines=3, offset=2 → the 3 lines ending 2 back from the newest."""
        page = await self._fetch(container_client, lines=3, offset=2)
        assert page.text == "line5\nline6\nline7"
        assert page.has_more is True

    @pytest.mark.asyncio
    async def test_zero_offset_returns_the_newest_lines(self, container_client):
        page = await self._fetch(container_client, lines=3)
        assert page.text == "line7\nline8\nline9"
        assert page.has_more is True

    @pytest.mark.asyncio
    async def test_window_larger_than_the_file_returns_all(self, container_client):
        page = await self._fetch(container_client, lines=500)
        assert page.text.splitlines() == [f"line{i}" for i in range(10)]
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_offset_past_the_start_of_the_file_returns_nothing(
        self, container_client
    ):
        """The page after the last one is empty, not a wrapped-around window."""
        for offset in (10, 99):
            page = await self._fetch(container_client, lines=3, offset=offset)
            assert page.text == ""
            assert page.has_more is False

    @pytest.mark.asyncio
    async def test_partial_last_page_returns_what_remains(self, container_client):
        page = await self._fetch(container_client, lines=5, offset=8)
        assert page.text == "line0\nline1"
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_window_landing_exactly_on_the_file_start_ends_paging(
        self, container_client
    ):
        """The exact-fit page is the last one: nothing precedes it.

        Off by one here either loops forever on an empty page or drops the
        oldest lines — paging is exact on this route, so pin the boundary.
        """
        page = await self._fetch(container_client, lines=5, offset=5)
        assert page.text == "line0\nline1\nline2\nline3\nline4"
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_one_line_short_of_the_start_still_has_more(self, container_client):
        page = await self._fetch(container_client, lines=4, offset=5)
        assert page.text == "line1\nline2\nline3\nline4"
        assert page.has_more is True


class TestJournaldHasMoreProbe:
    """``has_more`` on the Supervisor-backed routes.

    systemd's journal-gatewayd does not guard its negative-skip branch
    (``sd_journal_previous_skip(-n_skip + 1)`` never reports END_OF_STREAM the
    way the positive branch does), so an offset past the start of the journal
    CLAMPS to the oldest entry and still answers with a full window. A
    count-based ``has_more`` therefore never goes false once the offset
    overshoots, and an agent following the pagination hint loops on identical
    pages — on exactly the install class #2279 is about.
    """

    @pytest.fixture
    def addon_client(self, client):
        client._request = AsyncMock()
        client._raw_request = AsyncMock()
        return client

    @staticmethod
    def _serve(client, *bodies):
        """Answer successive Supervisor fetches with the given bodies."""
        client._supervisor_logs_get = AsyncMock(side_effect=list(bodies))

    async def _fetch(self, client, **kwargs):
        with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=True):
            return await client.get_error_log(**kwargs)

    @pytest.mark.asyncio
    async def test_short_window_needs_no_probe(self, addon_client):
        """Nothing clamps a window smaller than asked, so short means done."""
        self._serve(addon_client, "a\nb\n")
        page = await self._fetch(addon_client, lines=10)
        assert page.has_more is False
        assert addon_client._supervisor_logs_get.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_window_needs_no_probe(self, addon_client):
        self._serve(addon_client, "")
        page = await self._fetch(addon_client, lines=10)
        assert page.has_more is False
        assert addon_client._supervisor_logs_get.await_count == 1

    @pytest.mark.asyncio
    async def test_saturated_window_probes_the_block_behind_it(self, addon_client):
        """A differing block one step further back proves deeper history."""
        self._serve(addon_client, "a\nb\nc\n", "older entry\n")
        page = await self._fetch(addon_client, lines=3, offset=6)
        assert page.text == "a\nb\nc\n"
        assert page.has_more is True
        probe = addon_client._supervisor_logs_get.await_args
        assert probe.args == ("core",)
        assert probe.kwargs == {"lines": 8, "offset": 9}

    @pytest.mark.asyncio
    async def test_clamped_probe_matching_the_window_ends_paging(self, addon_client):
        """The clamp's signature: the probe returns the window's own first entry.

        Past the start of the journal the gateway hands back the oldest entry
        instead of nothing, so identity — not emptiness — is what says stop.
        """
        self._serve(addon_client, "oldest\nb\nc\n", "oldest\n")
        page = await self._fetch(addon_client, lines=3, offset=900)
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_empty_probe_ends_paging(self, addon_client):
        self._serve(addon_client, "a\nb\nc\n", "")
        page = await self._fetch(addon_client, lines=3)
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_stripped_range_header_terminates_paging(self, addon_client):
        """An intermediary may drop ``Range`` (RFC 7233 allows it).

        Supervisor then serves its default window for every request, so window
        and probe come back identical — and the identity check stops paging
        instead of handing the agent the same page forever.
        """
        default_window = "".join(f"line{i}\n" for i in range(100))
        self._serve(addon_client, default_window, default_window)
        page = await self._fetch(addon_client, lines=100, offset=400)
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_probe_compares_entries_not_counts(self, addon_client):
        """A multi-line traceback must not read as 'more history'.

        The line count over-estimates entries, so it can only over-trigger the
        probe; the probe's identity check is what answers.
        """
        window = "oldest\n  frame one\n  frame two\n"
        self._serve(addon_client, window, "oldest\n  frame one\n")
        page = await self._fetch(addon_client, lines=2, offset=10)
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_boundary_duplicate_line_does_not_end_paging(self, addon_client):
        """One duplicated line at the window boundary must not read as done.

        The entry just behind the window renders identically to the window's
        oldest line ("dup"), which false-matched a one-line probe; the block
        probe sees the differing context behind it.
        """
        window = "dup\n" + "".join(f"w{i}\n" for i in range(1, 4))
        probe = "".join(f"x{i}\n" for i in range(7)) + "dup\n"
        self._serve(addon_client, window, probe)
        page = await self._fetch(addon_client, lines=4, offset=0)
        assert page.has_more is True

    @pytest.mark.asyncio
    async def test_uniform_duplicate_run_reads_as_end_of_history(self, addon_client):
        """Accepted imprecision: _PROBE_ENTRIES identical lines straddling the
        boundary stop paging — and what a false stop skips there is more of
        the same duplicates, so nothing distinguishable is lost."""
        window = "dup\n" * 10
        probe = "dup\n" * 8
        self._serve(addon_client, window, probe)
        page = await self._fetch(addon_client, lines=10, offset=0)
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_small_window_clamp_still_ends_paging(self, addon_client):
        """A probe longer than the window must not read the clamp as "more".

        With lines < _PROBE_ENTRIES and an offset past the journal start,
        both requests clamp to the oldest entry and the probe comes back
        LONGER than the window; comparing full lengths would report more
        history forever. The shared-prefix compare says done.
        """
        window = "j0\nj1\n"
        probe = "".join(f"j{i}\n" for i in range(8))
        self._serve(addon_client, window, probe)
        page = await self._fetch(addon_client, lines=2, offset=500)
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_proxied_route_probes_the_same_way(self, client):
        """The supervised proxy route runs the identical protocol."""
        client._request = AsyncMock(return_value={"components": ["hassio"]})
        client._raw_request = AsyncMock(
            side_effect=[
                SimpleNamespace(text="a\nb\n"),
                SimpleNamespace(text="older\n"),
            ]
        )

        with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
            page = await client.get_error_log(lines=2, offset=4)

        assert page.has_more is True
        probe_headers = client._raw_request.await_args.kwargs["headers"]
        # Probe block: 8 entries ending just behind the window (offset 4 +
        # window 2 -> skip 13, count 8).
        assert probe_headers["Range"] == "entries=:-13:8"


@pytest.mark.asyncio
async def test_get_error_log_probe_401_never_reaches_the_container_branch(client):
    """A dead token surfaces as an auth error, not as a 404 from /error_log.

    This is the whole chain the probe's fail-open used to break: an
    external client against HAOS with an expired LLAT would fail the
    probe, be classified Container, and request the one route that
    install class does not serve.
    """
    client._request = AsyncMock(side_effect=HomeAssistantAuthError("401 Unauthorized"))
    client._raw_request = AsyncMock()
    client._supervisor_logs_get = AsyncMock()

    with (
        patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False),
        pytest.raises(HomeAssistantAuthError),
    ):
        await client.get_error_log(lines=_WINDOW)

    client._raw_request.assert_not_called()
    client._supervisor_logs_get.assert_not_called()


@pytest.mark.asyncio
async def test_get_error_log_uses_cached_supervised_flag(client):
    """Second `get_error_log` call on a supervised install reuses the cache."""
    response = SimpleNamespace(text="cached-call-log")
    client._raw_request = AsyncMock(return_value=response)
    # First call's probe returns hassio loaded; second call MUST NOT re-probe.
    client._request = AsyncMock(return_value={"components": ["hassio"]})

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        await client.get_error_log(lines=_WINDOW)
        await client.get_error_log(lines=_WINDOW)
        await client.get_error_log(lines=_WINDOW)

    # /api/config probe fired exactly once across 3 get_error_log calls.
    assert client._request.await_count == 1
    # /hassio/core/logs fetched on every call (no caching of log content).
    assert client._raw_request.await_count == 3


@pytest.mark.asyncio
async def test_get_error_log_caches_negative_supervised_flag(client):
    """Container HA: probe runs once across N get_error_log calls.

    Definitive non-supervised signal is cached, so subsequent calls go
    straight to /error_log without re-probing /api/config. Avoids the
    extra round-trip Gemini flagged as a MEDIUM efficiency issue.
    """
    container_response = SimpleNamespace(text="log content\n")
    client._request = AsyncMock(return_value={"components": ["sun", "demo"]})
    client._raw_request = AsyncMock(return_value=container_response)

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        await client.get_error_log(lines=_WINDOW)
        await client.get_error_log(lines=_WINDOW)
        await client.get_error_log(lines=_WINDOW)

    # /api/config probe fired exactly once.
    assert client._request.await_count == 1
    # /error_log fetched on every call.
    assert client._raw_request.await_count == 3


@pytest.mark.asyncio
async def test_get_error_log_supervised_probe_failure_falls_to_container(client):
    """Probe transport failure on first call drops to the Container branch.

    Fail-open contract: a transient /api/config failure must not break
    get_error_log entirely. It falls through to the historical
    /error_log path. (On HAOS that path then 404s — same status quo
    as before the fix, but with a clearer error than swallowing the
    probe exception would surface.) The probe failure is NOT cached, so
    the next call re-probes.
    """
    container_response = SimpleNamespace(text="container fallback log\n")
    client._request = AsyncMock(side_effect=HomeAssistantConnectionError("boom"))
    client._raw_request = AsyncMock(return_value=container_response)

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        page = await client.get_error_log(lines=_WINDOW)

    assert page.text == "container fallback log"
    # Probe was attempted, then /error_log fetched via _raw_request.
    client._request.assert_awaited_once_with("GET", "/config")
    client._raw_request.assert_awaited_once_with(
        "GET", "/error_log", headers={"Accept": "text/plain"}
    )
    # Probe failure must NOT have poisoned the cache.
    assert client._supervised_detected is None
