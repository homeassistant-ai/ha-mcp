"""Session guard: unit and e2e tests must not run in the same pytest session.

They share ``asyncio_default_test_loop_scope = session`` (one event loop per
xdist worker). E2e tests leave Docker/HTTP state in that loop; unit tests
using AsyncMock then behave non-deterministically.

Run them separately:
    cd tests && uv run pytest src/unit/ -n2
    cd tests && uv run pytest src/e2e/ -n2 --dist loadscope

The check runs in ``pytest_configure`` against the invocation path args
(not collected items) so it fires on the xdist controller before any worker
is spawned. Raising from a collection hook inside a worker crashes xdist with
an opaque INTERNALERROR; ``pytest_configure`` on the controller exits cleanly.
Only explicit path args are inspected: a bare ``pytest`` relying on
``testpaths`` is left alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_UNIT = Path(__file__).parent / "unit"
_E2E = Path(__file__).parent / "e2e"

_MESSAGE = (
    "Unit and e2e tests cannot run in the same pytest session. They share an "
    "asyncio event loop per xdist worker, and e2e's Docker/HTTP state then "
    "contaminates AsyncMock-based unit tests. Run them separately: "
    "'uv run pytest src/unit/ -n2' and "
    "'uv run pytest src/e2e/ -n2 --dist loadscope'."
)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def pytest_configure(config: pytest.Config) -> None:
    """Reject an invocation whose path args span both unit and e2e trees."""
    params = config.invocation_params
    if params is None:
        return
    base = Path(params.dir)
    touches_unit = touches_e2e = False
    # ``config.args`` contains only the already-parsed positional path args,
    # not option values (e.g. -k unit). Using invocation_params.args would
    # misclassify option arguments as paths.
    for arg in config.args:
        # Strip ``::node::ids`` and resolve relative to the invocation dir.
        target = (base / arg.split("::", 1)[0]).resolve()
        if _is_under(target, _UNIT):
            touches_unit = True
        elif _is_under(target, _E2E):
            touches_e2e = True
    if touches_unit and touches_e2e:
        raise pytest.UsageError(_MESSAGE)
