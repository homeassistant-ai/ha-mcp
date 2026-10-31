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


def on_mounted_volume(target: Path, root: str) -> bool:
    """True if a write to ``target`` lands on a filesystem mounted at or below
    the volume ``root``.

    The component runs inside HA Core, which sees a volume only where one is
    mounted: on HAOS and Supervised, Supervisor mounts /share, /media and /ssl;
    on a Container install only what the user bind-mounts exists. An unmounted
    root can still exist as a plain directory (the image's own /media, or one an
    earlier build created with mkdir), and a write there reports success for a
    file that stays in the container layer. So walk up from the deepest
    existing ancestor of ``target`` to ``root`` and require a mount point on
    the way; a bind mount of /media/music alone still counts.
    """
    top = Path(root)
    probe = target.parent if top in target.parents else top
    while probe != top and not probe.exists():
        probe = probe.parent
    while probe != top and not os.path.ismount(probe):
        probe = probe.parent
    return os.path.ismount(probe)
