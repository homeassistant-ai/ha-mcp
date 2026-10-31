"""Mount check for writes under a HAOS sibling volume (issue #2875).

Kept out of ``__init__.py``, which is at its module-size baseline.
"""

import os
from pathlib import Path

# write_file refusals that name a directory, keyed by _write_file_sync's _error.
PARENT_ERRORS = {
    "no_parent": "Parent directory does not exist: {}",
    "not_mounted": "{} is not mounted into Home Assistant Core on this install",
}


def _in_container() -> bool:
    """True when Core runs in a container (HAOS, Supervised, Container)."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def _mount_points() -> set[str]:
    """Mount points from /proc/self/mountinfo, which also lists a bind mount
    from the same filesystem (``os.path.ismount`` only sees a device change).
    Empty where it can't be read."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as info:
            return {line.split()[4].replace("\\040", " ") for line in info}
    except (OSError, IndexError):
        return set()


def on_mounted_volume(target: Path, root: str) -> bool:
    """True if a write to ``target`` lands on storage outside the container
    layer: a filesystem mounted at or below the volume ``root``.

    Inside a container, Core sees a volume only where one is mounted: on HAOS
    and Supervised, Supervisor mounts /share, /media and /ssl; on a Container
    install only what the user bind-mounts is there. An unmounted root can
    still exist as a plain directory (the image's own /media, or one an
    earlier build created with mkdir), and a write there reports success for
    a file that stays in the container layer. So resolve symlinks as the write
    will, then walk up from the deepest existing ancestor of ``target`` to
    ``root`` and require a mount point on the way; a bind mount of
    /media/music alone still counts. Outside a container (a Core install) the
    root is the host's own directory, so it only has to exist.
    """
    if not _in_container():
        return os.path.isdir(root)
    top = Path(os.path.realpath(root))
    probe = Path(os.path.realpath(target.parent))
    if probe != top and top not in probe.parents:
        probe = top
    while probe != top and not probe.exists():
        probe = probe.parent
    mounts = _mount_points()
    while probe != top and not (str(probe) in mounts or os.path.ismount(probe)):
        probe = probe.parent
    return str(probe) in mounts or os.path.ismount(probe)
