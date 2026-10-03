"""Collection hooks of the E2E suite: backend marker skips and the doomed-run hook."""

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))  # tests/src/ for haos_runtime
import doomed_run
from haos_runtime import (
    is_haos_backend_selected,
    is_haos_embedded_mode,
    is_haos_inaddon_mode,
    is_haos_stdio_mode,
)

from ._conftest_embedded import (
    _is_embedded_backend_selected,
    _is_no_tools_entry_selected,
)


def _move_haos_tls_items_last(items: list[Any]) -> None:
    """Make the one destructive Core-restart scope the final xdist work unit.

    ``loadscope`` groups tests into scopes by module (or module::class). With
    xdist's default ``--loadscope-reorder`` the scopes are sorted by
    descending size with a stable sort, so the one-test TLS module moved to
    the end stays last among the one-test scopes; with ``--no-loadscope-reorder``
    plain collection order keeps it last outright. The FIFO workqueue then
    dispatches it as the final unit. The worker that receives it may still
    hold up to two queued ordinary tests (xdist tops nodes up at <=2 pending)
    but executes units in dispatch order, so Core is restarted in that
    worker's already-running VM only after its ordinary work has run.
    """
    ordinary = [item for item in items if "haos_tls" not in item.keywords]
    tls = [item for item in items if "haos_tls" in item.keywords]
    items[:] = [*ordinary, *tls]


def _apply_haos_tls_skip(item: Any, enabled: bool, skip_marker: Any) -> None:
    """Skip a ``haos_tls`` item on every lane where the scenario is disabled.

    Kept out-of-line so ``pytest_collection_modifyitems`` stays under the
    repo-wide C901 ceiling — inlining this branch trips it.
    """
    if "haos_tls" in item.keywords and not enabled:
        item.add_marker(skip_marker)


def _apply_haos_embedded_only_skip(
    item: Any, haos_embedded: bool, skip_marker: Any
) -> None:
    """Skip a ``haos_embedded_only`` item everywhere but the HAOS embedded lane.

    Out-of-line for the same reason as ``_apply_haos_tls_skip``.
    """
    if "haos_embedded_only" in item.keywords and not haos_embedded:
        item.add_marker(skip_marker)


def _apply_embedded_only_skip(
    item: Any, embedded_selected: bool, skip_marker: Any
) -> None:
    """Skip an ``embedded_only`` item everywhere but the embedded lane.

    Split out of ``pytest_collection_modifyitems`` for the same reason as
    ``_apply_haos_tls_skip``: the dispatcher sits at ruff's C901 ceiling.
    """
    if "embedded_only" in item.keywords and not embedded_selected:
        item.add_marker(skip_marker)


def _apply_beta_haos_only_skip(
    item: Any, beta_selected: bool, skip_marker: Any
) -> None:
    """Skip a ``beta_haos_only`` item outside the beta image lanes."""
    if "beta_haos_only" in item.keywords and not beta_selected:
        item.add_marker(skip_marker)


def _apply_no_tools_entry_skips(
    item: Any,
    no_tools_entry: bool,
    skip_requires_tools_entry: Any,
    skip_no_tools_only: Any,
) -> None:
    """Apply both directions of the no-tools-lane gate (#2292).

    The two markers are mirror images: ``requires_tools_entry`` needs the
    File & YAML Tools entry (so it skips ON the no-tools lanes),
    ``no_tools_only`` pins what happens WITHOUT it (so it skips everywhere
    else). Out-of-line for the same reason as ``_apply_haos_tls_skip``: the
    dispatcher sits at ruff's C901 ceiling.
    """
    if no_tools_entry and "requires_tools_entry" in item.keywords:
        item.add_marker(skip_requires_tools_entry)
    if not no_tools_entry and "no_tools_only" in item.keywords:
        item.add_marker(skip_no_tools_only)


def pytest_collection_modifyitems(config, items):
    """Enforce backend markers and auto-apply ``haos_only`` to its dir.

    Backend markers (#1349 item 7 introduced the inaddon split; #1527 added
    the embedded lanes, where ``external_only`` also skips because the
    server-under-test lives out-of-process inside the HA container):

    - ``haos_only``: only runs when the HAOS backend is selected
      (``HAOS_TEST_IMAGE_PATH`` set). Auto-applied to anything under
      ``tests/src/e2e/haos_only/``.
    - ``container_only``: only runs on the testcontainer backend.
    - ``embedded_only``: only runs on the embedded testcontainer backend
      (``E2E_BACKEND=embedded``) — the one lane whose session container has
      ha-mcp installed inside the HA image.
    - ``external_only``: any tier where the server-under-test lives IN the
      pytest process — plain testcontainer AND HAOS external (``mcp_client``
      is an in-process FastMCP server talking HTTP to HAOS). Skipped on stdio,
      inaddon, container-embedded, and HAOS-embedded. The name is historical and
      does NOT mean "HAOS external only"; see the skip expression below.
    - ``beta_haos_only``: only runs when at least one beta image expectation
      variable is set. Partial expectation sets deliberately run so the test
      can report the missing contract value.
    - ``inaddon_only``: HAOS inaddon mode only (``mcp_client`` is HTTP
      to the addon's MCP endpoint, ``is_running_in_addon()=True`` paths
      exercised). Skipped on external mode and on testcontainer.
    - ``haos_stdio_only``: HAOS stdio mode only (``mcp_client`` launches the
      installed ``ha-mcp`` command and uses real stdio JSON-RPC framing).
    - ``haos_tls``: final HAOS-embedded scenario. It restarts Core with HTTPS,
      exercises the same VM, and restores HTTP before session teardown.
    - ``haos_embedded_only``: HAOS embedded mode only — the in-process server
      shares Home Assistant's process and event loop, which is the property
      under test (#2357).
    - ``requires_tools_entry`` / ``no_tools_only``: the two directions of the
      no-tools lane gate (``E2E_NO_TOOLS_ENTRY=1``, #2292). The first needs the
      component's "File & YAML Tools" config entry and skips where it is
      absent; the second pins the absent-entry behaviour and skips everywhere
      the entry IS installed.
    """
    del config
    haos = is_haos_backend_selected()
    inaddon = haos and is_haos_inaddon_mode()
    haos_stdio = haos and is_haos_stdio_mode()
    beta_haos = any(
        os.environ.get(name)
        for name in (
            "HAOS_EXPECTED_OS_VERSION",
            "HAOS_EXPECTED_SUPERVISOR_CHANNEL",
            "HAOS_EXPECTED_SUPERVISOR_MIN_VERSION",
            "HAOS_EXPECTED_CORE_VERSION",
        )
    )
    # The embedded backend (#1527) is a testcontainer variant, so ``haos`` is
    # False here — ``haos_only`` still skips and ``container_only`` still runs on
    # it. What differs is that the in-process server lives INSIDE the container:
    # test-process env / monkeypatch reconfiguration and in-process mocks can't
    # reach it (exactly the inaddon limitation ``external_only`` already guards),
    # and a couple of tests are provably redundant with the lane's own backend.
    embedded = _is_embedded_backend_selected()
    # The HAOS embedded lane (#1527) IS a HAOS backend (``haos`` True — qcow2
    # staged), so ``haos_only`` runs and ``container_only`` skips exactly like the
    # other HAOS lanes. Its server-under-test is the in-process MCP server
    # inside the HAOS core container, driven over its ingress webhook — the same
    # out-of-process constraint as stdio/inaddon/container-embedded, so
    # ``external_only`` skips here too; the haos_only embedded smoke module is
    # redundant with the lane's session backend and skips via ``not_on_haos_embedded``.
    haos_embedded = haos and is_haos_embedded_mode()
    skip_haos = pytest.mark.skip(
        reason="HAOS backend not selected (set HAOS_TEST_IMAGE_PATH)"
    )
    skip_container = pytest.mark.skip(
        reason="HAOS backend is active; test is container-only"
    )
    skip_inaddon_only = pytest.mark.skip(
        reason="inaddon mode required (set HAOS_TEST_MODE=inaddon)"
    )
    skip_haos_stdio_only = pytest.mark.skip(
        reason="HAOS stdio mode required (set HAOS_TEST_MODE=stdio)"
    )
    skip_haos_tls = pytest.mark.skip(
        reason="final Core TLS scenario runs only in the existing HAOS embedded worker"
    )
    skip_haos_embedded_only = pytest.mark.skip(
        reason="HAOS embedded mode required (set HAOS_TEST_MODE=embedded)"
    )
    skip_external_only = pytest.mark.skip(
        reason="out-of-process server (stdio/inaddon/embedded); test needs an "
        "in-process server it can reconfigure via env/monkeypatch or reach an "
        "in-process mock"
    )
    skip_embedded_only = pytest.mark.skip(
        reason="embedded testcontainer backend required (E2E_BACKEND=embedded); "
        "only that lane installs ha-mcp inside the HA image"
    )
    skip_beta_haos_only = pytest.mark.skip(
        reason="beta HAOS image expectations are not configured"
    )
    no_tools_entry = _is_no_tools_entry_selected()
    skip_requires_tools_entry = pytest.mark.skip(
        reason="File & YAML Tools entry not installed in this lane "
        "(E2E_NO_TOOLS_ENTRY=1; #2292)"
    )
    skip_no_tools_only = pytest.mark.skip(
        reason="no-tools lane required (set E2E_NO_TOOLS_ENTRY=1); only that "
        "lane runs without the File & YAML Tools config entry"
    )
    skip_not_on_embedded = pytest.mark.skip(
        reason="redundant on the embedded backend (the lane's own session "
        "backend already exercises this path)"
    )
    skip_not_on_haos_embedded = pytest.mark.skip(
        reason="redundant on the haos_embedded backend (the lane's own session "
        "backend already enables the entry and drives the in-process server)"
    )
    for item in items:
        if "haos_only" in str(item.fspath):
            item.add_marker(pytest.mark.haos_only)
        keywords = item.keywords
        if "haos_only" in keywords and not haos:
            item.add_marker(skip_haos)
        elif "container_only" in keywords and haos:
            item.add_marker(skip_container)
        _apply_embedded_only_skip(item, embedded, skip_embedded_only)
        if "inaddon_only" in keywords and not inaddon:
            item.add_marker(skip_inaddon_only)
        if "haos_stdio_only" in keywords and not haos_stdio:
            item.add_marker(skip_haos_stdio_only)
        _apply_haos_tls_skip(item, haos_embedded, skip_haos_tls)
        _apply_haos_embedded_only_skip(item, haos_embedded, skip_haos_embedded_only)
        _apply_beta_haos_only_skip(item, beta_haos, skip_beta_haos_only)
        _apply_no_tools_entry_skips(
            item, no_tools_entry, skip_requires_tools_entry, skip_no_tools_only
        )
        # Deliberate: inserting the haos_tls dispatch above broke the old
        # ``elif`` chain into this ``if``. An item carrying several gate
        # markers now collects one skip marker per gate instead of the first
        # only — still one skipped item, same counts; only the reported
        # reason (first marker added) could differ.
        # ``external_only`` skips on any tier where the server is NOT in the
        # pytest process: the inaddon HAOS addon AND the embedded backend's
        # in-process MCP server (both #1527). The name is historical (from
        # #1361 where the only motivating consumer was ``test_supervisor_mock.py``,
        # whose monkeypatch-based fixture works fine on testcontainer + external
        # HAOS but can't reach a server in another process). Skipping on plain
        # testcontainer was a dispatcher bug — the mock fixture is in-process and
        # runs cleanly there (PR #1375 final-skip audit; 14 supervisor_mock tests
        # were silently skipping on every testcontainer e2e-tests.yml run). The
        # embedded backend has the same out-of-process constraint as inaddon, so
        # it joins the skip; those tests keep full coverage on the container lane.
        # ``haos_embedded`` joins for the same reason: its server-under-test is the
        # in-process MCP server inside the HAOS core container, reachable only
        # over the webhook — the test process can't reconfigure it via env /
        # monkeypatch or reach an in-process mock. These tests keep full coverage
        # on the external HAOS lane (where the in-process FastMCP server IS in the
        # test process) and the container lane.
        # haos_stdio is also out-of-process: its installed ha-mcp subprocess
        # cannot observe pytest-process env changes, monkeypatches, or in-process
        # mocks. The same tests retain coverage on container/external HAOS lanes.
        #
        if "external_only" in keywords and (
            inaddon or embedded or haos_embedded or haos_stdio
        ):
            item.add_marker(skip_external_only)
        # ``not_on_embedded`` is applied only where a test is provably redundant
        # with the embedded lane's own session backend (e.g. the workflows/embedded
        # smoke test boots its OWN in-process MCP server container to prove the install
        # method — which the embedded lane already does for every test). It keeps
        # running unchanged on the container lane.
        if "not_on_embedded" in keywords and embedded:
            item.add_marker(skip_not_on_embedded)
        # ``not_on_haos_embedded`` marks the haos_only embedded smoke module
        # (haos_only/test_embedded_server_haos.py): it enables the baked entry and
        # drives the webhook per-test, which the haos_embedded lane's session
        # backend already does ONCE at setup for the whole suite. Running them here
        # would double-enable the entry and race the session backend, so they skip
        # (they keep running on the external + inaddon HAOS lanes, where they are
        # the sole thing exercising the in-process server).
        if "not_on_haos_embedded" in keywords and haos_embedded:
            item.add_marker(skip_not_on_haos_embedded)
    _move_haos_tls_items_last(items)


def pytest_runtest_logreport(report):
    """Fail fast on a doomed run, on every e2e lane.

    This conftest is shared by the testcontainer / external-HAOS / inaddon
    suites. The logic lives in ``doomed_run`` beside its detector, so tests can
    load it without this module's HA and container imports.
    """
    doomed_run.pytest_runtest_logreport(report)
