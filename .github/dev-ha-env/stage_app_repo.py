"""Stage the checkout's app (add-on) as an app repository a live HA can install.

A dev env started with ``server=app`` runs the app baked into its HAOS image.
To move it to a new commit in place, push the tree this writes to a branch of
your fork, add that branch as an app repository in the running Home
Assistant, and install or update the app from it. Every update rebuilds the
app on the instance from the tree staged here. See docs/dev-ha-env.md, step 5.

usage: stage_app_repo.py <out dir> <version>
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "haos_image_build"))
from dev_addon_tree import stage_dev_addon_tree  # noqa: E402

# Distinct from the image's local app (local_ha_mcp_dev), so both can be
# installed; only one may run at a time, since both bind port 9583.
SLUG = "ha_mcp_branch"
MARKER = "name: ha-mcp branch build\n"


def _clear(out: Path) -> None:
    """Empty a previous staging of ``out``, keeping its ``.git``."""
    leftovers = [child for child in out.iterdir() if child.name != ".git"]
    manifest = out / "repository.yaml"
    if leftovers and not (
        manifest.is_file() and manifest.read_text().startswith(MARKER)
    ):
        sys.exit(
            f"{out} is not an earlier staging of this script; refusing to empty it"
        )
    for child in leftovers:
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _stage(staging: Path, version: str) -> None:
    """Write the app repository for ``version`` into the empty ``staging``."""
    (staging / "repository.yaml").write_text(
        MARKER
        + "url: https://github.com/homeassistant-ai/ha-mcp\nmaintainer: dev env\n"
    )
    app = staging / SLUG
    stage_dev_addon_tree(REPO_ROOT, app)
    config = app / "config.yaml"
    replacements = {
        "slug:": f'slug: "{SLUG}"\n',
        "name:": f'name: "HA MCP (branch build {version})"\n',
        "version:": f'version: "{version}"\n',
    }
    lines = []
    for line in config.read_text().splitlines(keepends=True):
        key = next((key for key in replacements if line.startswith(key)), None)
        lines.append(replacements.pop(key) if key else line)
    if replacements:
        sys.exit(f"{config} has no top-level {', '.join(replacements)} line to set")
    config.write_text("".join(lines))


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    out, version = Path(sys.argv[1]).resolve(), sys.argv[2]
    # Also a Docker image tag: Supervisor builds the app under its version.
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", version):
        sys.exit(f"{version!r} is not a version (use e.g. 9.1.0-test.1)")
    if out.is_relative_to(REPO_ROOT):
        sys.exit(f"{out} is inside the checkout it copies; stage outside it")
    out.mkdir(parents=True, exist_ok=True)
    # Build first, so a failed staging leaves the previous one in place.
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        _stage(Path(tmp), version)
        _clear(out)
        for child in Path(tmp).iterdir():
            shutil.move(child, out / child.name)
    print(f"Staged {out / SLUG} at version {version}")


if __name__ == "__main__":
    main()
