"""Stage the checkout's app (add-on) as an app repository a live HA can install.

A dev env started with ``server=app`` runs the app baked into its HAOS image.
To move it to a new commit in place, push the tree this writes to a branch of
your fork, add that branch as an app repository in the running Home
Assistant, and install or update the app from it. Every update rebuilds the
app on the instance from this checkout. See docs/dev-ha-env.md, step 5.

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


def main() -> None:
    out, version = Path(sys.argv[1]), sys.argv[2]
    out.mkdir(parents=True, exist_ok=True)
    for child in out.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
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
    lines = [
        next((new for key, new in replacements.items() if ln.startswith(key)), ln)
        for ln in config.read_text().splitlines(keepends=True)
    ]
    config.write_text("".join(lines))
    print(f"Staged {app} at version {version}")


if __name__ == "__main__":
    main()
