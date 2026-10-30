"""Lay out the ha-mcp dev app (add-on) as a Supervisor local-build context.

Shared by the HAOS image bake (``build_image.stage_dev_addon_source``) and the
dev env's in-place app update (``.github/dev-ha-env/stage_app_repo.py``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

if __package__:
    from .python_base import copy_addon_tree
else:
    from python_base import copy_addon_tree

# Files the dev addon's Dockerfile copies from the stable addon dir, staged at
# the build-context root with their COPY lines rewritten to match.
STABLE_ADDON_FILES = ("start.py", "app_options.json")
# Other repo files it copies, besides src/. Supervisor builds with the addon
# dir as the context, so they are staged into it. Both tuples must match
# haos_runtime's (kept in sync by hand like HA_MCP_TEST_SECRET_PATH;
# test_haos_dev_addon_context checks both against the Dockerfile).
DEV_ADDON_REPO_FILES = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "tests/test-env/pyproject.toml",
)


def stage_dev_addon_tree(repo_root: Path, staging: Path) -> None:
    """Lay out the dev addon at ``staging`` as Supervisor builds a local addon.

    The addon dir is the build context, so the files its Dockerfile copies
    from elsewhere in the repo are staged into it, the Dockerfile's COPY lines
    are rewritten to match, and ``image:`` is removed so Supervisor builds
    instead of pulling.
    """
    dev_addon_src = repo_root / "homeassistant-addon-dev"
    copy_addon_tree(dev_addon_src, staging)

    # Files outside the addon dir that the Dockerfile COPYs from.
    for name in STABLE_ADDON_FILES:
        shutil.copy(repo_root / "homeassistant-addon" / name, staging / name)
    for name in DEV_ADDON_REPO_FILES:
        (staging / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(repo_root / name, staging / name)
    # src/ha_mcp: nuke + copy fresh so a stale tree (e.g. left over from
    # a prior local run) doesn't shadow the current version.
    addon_src_dir = staging / "src"
    if addon_src_dir.exists():
        shutil.rmtree(addon_src_dir)
    addon_src_dir.mkdir()
    shutil.copytree(repo_root / "src" / "ha_mcp", addon_src_dir / "ha_mcp")

    # Dockerfile in homeassistant-addon-dev/ uses
    # ``COPY homeassistant-addon/start.py /`` because it's authored to
    # be built from the repo root context. Supervisor builds with the addon
    # dir itself as the context, so the path needs to be ``COPY start.py /``.
    dockerfile = staging / "Dockerfile"
    patched = dockerfile.read_text()
    for name in STABLE_ADDON_FILES:
        line = f"COPY homeassistant-addon/{name} /"
        if line not in patched:
            # Fail fast: an unpatched Dockerfile fails the addon build
            # opaquely 5+ min later, during ``addons/{slug}/install``.
            raise RuntimeError(
                f"Dockerfile patch failed: expected line {line!r} not "
                f"found in {dockerfile}. The dev addon's Dockerfile may "
                f"have been restructured; update the patch in "
                f"stage_dev_addon_tree to match the new shape."
            )
        patched = patched.replace(line, f"COPY {name} /")
    dockerfile.write_text(patched)

    # Strip the ``image:`` field from config.yaml. Production dev-addon
    # ships built images at ghcr.io/homeassistant-ai/ha-mcp-addon-dev-{arch};
    # when Supervisor sees ``image:``, it tries to PULL from GHCR rather
    # than build from the staged Dockerfile, and a locally bumped version has
    # no GHCR tag (404 → addon update fails). Without the field Supervisor
    # builds from the staged Dockerfile, from the local store or an app
    # repository alike.
    config_yaml = staging / "config.yaml"
    config_lines = [
        ln
        for ln in config_yaml.read_text().splitlines(keepends=True)
        if not ln.startswith("image:")
    ]
    config_yaml.write_text("".join(config_lines))
    # Verify the strip: a future restructure that indents the field
    # under a parent key would make the line-prefix filter a no-op,
    # silently re-introducing GHCR-pull behavior.
    post_strip = config_yaml.read_text()
    if "\nimage:" in post_strip or post_strip.startswith("image:"):
        raise RuntimeError(
            f"config.yaml ``image:`` strip did not remove the field "
            f"from {config_yaml}; Supervisor will pull from GHCR and "
            f"the per-PR version bump will 404. The field may now be "
            f"indented under a parent — update the filter accordingly."
        )
