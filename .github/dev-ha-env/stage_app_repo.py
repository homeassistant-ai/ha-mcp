"""Stage the checkout's app (add-on) as an app repository a live HA can install.

A dev env started with ``server=app`` runs the app baked into its HAOS image.
To move it to a new commit in place, push the tree this writes to a branch of
your fork, add that branch as an app repository in the running Home
Assistant, and install or update the app from it. Every update rebuilds the
app on the instance from the tree staged here. See docs/dev-ha-env.md, step 5.

usage: stage_app_repo.py <out dir> <version>
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "haos_image_build"))
from dev_addon_tree import stage_dev_addon_tree  # noqa: E402

# Distinct from the image's local app (local_ha_mcp_dev), so both can be
# installed; only one may run at a time, since both bind port 9583.
SLUG = "ha_mcp_branch"


def _clear(out: Path) -> None:
    """Empty a previous staging of ``out``, keeping its ``.git``."""
    leftovers = [child for child in out.iterdir() if child.name != ".git"]
    if leftovers and not (out / "repository.yaml").is_file():
        sys.exit(
            f"{out} is not an earlier staging (no repository.yaml); refusing to empty it"
        )
    for child in leftovers:
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    out, version = Path(sys.argv[1]).resolve(), sys.argv[2]
    out.mkdir(parents=True, exist_ok=True)
    _clear(out)
    (out / "repository.yaml").write_text(
        "name: ha-mcp branch build\nurl: https://github.com/homeassistant-ai/ha-mcp\n"
        "maintainer: dev env\n"
    )
    app = out / SLUG
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
    print(f"Staged {app} at version {version}")


if __name__ == "__main__":
    main()
