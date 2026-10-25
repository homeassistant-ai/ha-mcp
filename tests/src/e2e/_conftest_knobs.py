"""Opt-in environment knobs that trade E2E container boot time for reliability.

Both knobs are inert when unset: the defaults are the values every lane has
always used.

``HA_E2E_PIP_CACHE``
    Host directory bound over the container image's pip cache. When set, the
    container lanes' preinstalls drop ``--no-cache-dir`` so their downloads
    land in that directory and a later run reuses them instead of paying for
    the whole wheel tree again. When unset, each container keeps its own
    ephemeral pip cache, exactly as before.

``HA_E2E_READY_TIMEOUT``
    Seconds to wait for the Home Assistant API before failing the lane. The
    embedded lane preinstalls the checkout wheel plus its dependency tree
    before HA's ``/init``, so ``/api/`` liveness trails a full pip window; on a
    slow PyPI run that window alone can exceed the historical budget. It
    overrides BOTH testcontainer-lane budgets (embedded and plain container)
    rather than only the embedded one, because the knob names the readiness
    gate, not a lane, and a slow host pays the same cost on both. The HAOS
    (QEMU) lanes keep their own budgets: a VM boot is bounded by the image, not
    by a pip window, so this knob would be the wrong lever there.

Resolution lives here rather than in the fixtures so the timeout budget and the
pip-cache assembly can be asserted without Docker.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PIP_CACHE_ENV_VAR = "HA_E2E_PIP_CACHE"
READY_TIMEOUT_ENV_VAR = "HA_E2E_READY_TIMEOUT"

# pip's cache directory inside the Home Assistant container image. The upstream
# image sets no ``USER`` (it runs as root), and neither
# ``home-assistant/core``'s Dockerfile nor its ``docker-base`` parent sets
# ``PIP_CACHE_DIR``, ``XDG_CACHE_HOME`` or ``PIP_NO_CACHE_DIR``, so pip
# resolves the cache to ``$HOME/.cache/pip`` = ``/root/.cache/pip``. A mount
# anywhere else is a directory pip never reads.
PIP_CACHE_MOUNT_PATH = "/root/.cache/pip"


def pip_cache_mount() -> tuple[Path, str] | None:
    """``(host_dir, container_path)`` to bind as the pip cache, or None when unset.

    ``$HOME`` in the value is expanded and a relative path is resolved against
    the working directory: Docker requires an absolute host path, so a relative
    ``pip-cache`` would otherwise bind the wrong directory (or fail the mount).
    """
    raw = os.environ.get(PIP_CACHE_ENV_VAR, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve(), PIP_CACHE_MOUNT_PATH


def pip_cache_flags() -> str:
    """The cache flag interpolated into every container-lane ``pip install``.

    ``"--no-cache-dir "`` unless a cache directory is mounted, in which case
    pip's own default — cache into ``$HOME/.cache/pip``, which IS the mount —
    is what we want and the flag is dropped. The trailing space lets callers
    interpolate it directly in front of the next flag; an empty return value
    therefore leaves the command's spacing intact.
    """
    return "" if pip_cache_mount() is not None else "--no-cache-dir "


def resolve_ready_timeout(default: int) -> int:
    """API-ready budget in seconds: ``HA_E2E_READY_TIMEOUT`` when set, else ``default``.

    Raises ValueError for anything that is not a positive whole number of
    seconds instead of falling back to ``default``. A silently ignored typo
    would reinstate the exact budget a slow run was trying to escape, and the
    lane would then die of the timeout it was configured to survive — with
    nothing in the log pointing at the knob.
    """
    raw = os.environ.get(READY_TIMEOUT_ENV_VAR)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        # Set but empty is a broken export, not an absent knob: defaulting here
        # would reinstate the budget the caller meant to raise.
        raise ValueError(
            f"{READY_TIMEOUT_ENV_VAR} must be a whole number of seconds, got {raw!r}"
        )
    try:
        timeout = int(value)
    except ValueError:
        raise ValueError(
            f"{READY_TIMEOUT_ENV_VAR} must be a whole number of seconds, got {raw!r}"
        ) from None
    if timeout <= 0:
        raise ValueError(f"{READY_TIMEOUT_ENV_VAR} must be positive, got {timeout}")
    logger.info(
        "⏳ API-ready budget overridden to %ss by %s (default %ss)",
        timeout,
        READY_TIMEOUT_ENV_VAR,
        default,
    )
    return timeout
