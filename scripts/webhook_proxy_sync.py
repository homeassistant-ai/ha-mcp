#!/usr/bin/env python3
"""Transform engine for the webhook-proxy add-on promote / reset workflows.

The stable (``homeassistant-addon-webhook-proxy``) and dev
(``homeassistant-addon-webhook-proxy-dev``) flavors are a hand-maintained
duplicate: the same code with a fixed *identity* rewrite between them. This
module encodes that identity as two :class:`Flavor` profiles that are exact
inverses of each other, and applies it mechanically:

* ``--direction promote``  copies dev -> stable (SRC=dev, DST=stable)
* ``--direction reset``    copies stable -> dev (SRC=stable, DST=dev)

Beyond the component code, the copy+rename set also spans ``start.py``, the
``Dockerfile``, ``config.yaml``, and ``translations/en.yaml`` — each carries
the ``mcp_proxy`` identity token (in ``translations/en.yaml`` it is the
log-filter hint) that must flip with the flavor.

Everything is expressed as small pure functions so the transform can be unit
tested without touching the filesystem. The one side-effecting step is
``ruff format`` on the emitted ``.py`` files, which re-wraps lines whose length
crossed the 88-column boundary purely because a token grew/shrank
(``mcp_proxy`` <-> ``mcp_proxy_dev``).

Round-trip invariant (the acceptance gate): because the two flavors already
differ only by this identity, ``reset`` on a clean tree must change nothing in
the dev dir except the ``version`` lines, and ``promote --bump patch`` must
change nothing in the stable dir except the ``version`` lines.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Flavor identity profiles (exact inverses of each other)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Flavor:
    """The fixed, per-flavor identity of one webhook-proxy add-on variant."""

    addon_dir: str
    component: str
    slug: str
    config_name: str
    config_description: str
    boot: str
    stage: str | None
    sibling_slug_base: str
    mutex_notification_id: str
    sibling_mutex_notification_id: str
    sibling_label: str
    async_register_name: str
    dockerfile_hass_name: str
    dockerfile_hass_description: str
    manifest_name: str
    # The flavor-descriptor comment in start.py flips like the mutex constants:
    # "This is the {flavor_word} flavor: its sibling is the {sibling_word} add-on."
    flavor_word: str
    sibling_word: str


STABLE = Flavor(
    addon_dir="homeassistant-addon-webhook-proxy",
    component="mcp_proxy",
    slug="ha_mcp_webhook_proxy",
    config_name="Nabu Casa - Webhook Proxy for HA MCP",
    config_description=(
        "Remote access proxy via Nabu Casa or any reverse proxy "
        "(Cloudflare, DuckDNS, nginx)"
    ),
    boot="auto",
    stage=None,
    sibling_slug_base="ha_mcp_webhook_proxy_dev",
    mutex_notification_id="mcp_proxy_mutex",
    sibling_mutex_notification_id="mcp_proxy_dev_mutex",
    sibling_label="Webhook Proxy (Dev)",
    async_register_name="MCP Proxy",
    dockerfile_hass_name="Nabu Casa - Webhook Proxy for HA MCP",
    dockerfile_hass_description="Remote access proxy for HA MCP via webhook",
    manifest_name="MCP Webhook Proxy",
    flavor_word="STABLE",
    sibling_word="dev",
)

DEV = Flavor(
    addon_dir="homeassistant-addon-webhook-proxy-dev",
    component="mcp_proxy_dev",
    slug="ha_mcp_webhook_proxy_dev",
    config_name="Nabu Casa - Webhook Proxy for HA MCP (Dev)",
    config_description=(
        "DEV CHANNEL (unstable) — remote access proxy via Nabu Casa or any "
        "reverse proxy. Cannot run alongside the stable Webhook Proxy add-on."
    ),
    boot="manual",
    stage="experimental",
    sibling_slug_base="ha_mcp_webhook_proxy",
    mutex_notification_id="mcp_proxy_dev_mutex",
    sibling_mutex_notification_id="mcp_proxy_mutex",
    sibling_label="Webhook Proxy",
    async_register_name="MCP Proxy (Dev)",
    dockerfile_hass_name="Nabu Casa - Webhook Proxy for HA MCP (Dev)",
    dockerfile_hass_description="DEV channel — remote access proxy for HA MCP via webhook",
    manifest_name="MCP Webhook Proxy (Dev)",
    flavor_word="DEV",
    sibling_word="stable",
)


# ---------------------------------------------------------------------------
# Version helpers (pure)
# ---------------------------------------------------------------------------
_STABLE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_DEV_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.dev(\d+)$")


def parse_stable_version(version: str) -> tuple[int, int, int]:
    """Parse an ``X.Y.Z`` stable version into a ``(X, Y, Z)`` tuple."""
    m = _STABLE_RE.match(version.strip())
    if not m:
        raise ValueError(f"unparseable stable version: {version!r}")
    return int(m[1]), int(m[2]), int(m[3])


def parse_dev_version(version: str) -> tuple[tuple[int, int, int], int]:
    """Parse ``A.B.C.devN`` into ``((A, B, C), N)``."""
    m = _DEV_RE.match(version.strip())
    if not m:
        raise ValueError(f"unparseable dev version: {version!r}")
    return (int(m[1]), int(m[2]), int(m[3])), int(m[4])


def bump_stable(base: tuple[int, int, int], bump: str) -> tuple[int, int, int]:
    """Apply a semver bump to a ``(X, Y, Z)`` base tuple."""
    x, y, z = base
    if bump == "patch":
        return (x, y, z + 1)
    if bump == "minor":
        return (x, y + 1, 0)
    if bump == "major":
        return (x + 1, 0, 0)
    raise ValueError(f"unknown bump level: {bump!r}")


def promote_version(current_stable: str, bump: str) -> str:
    """Next stable version = current stable ``X.Y.Z`` bumped by ``bump``."""
    return "{}.{}.{}".format(*bump_stable(parse_stable_version(current_stable), bump))


def reset_version(current_dev: str, current_stable: str) -> str:
    """Next dev version, guaranteed to strictly increase.

    ``new_base = max(stable_base, dev_base)`` and the ``devN`` counter always
    advances by one. Because the base never decreases and the counter always
    rises, the result strictly increases even when stable's base is *behind*
    the current dev base (the code goes backward; the version still climbs).
    """
    dev_base, dev_n = parse_dev_version(current_dev)
    stable_base = parse_stable_version(current_stable)
    new_base = max(stable_base, dev_base)
    return "{}.{}.{}.dev{}".format(*new_base, dev_n + 1)


# ---------------------------------------------------------------------------
# Text transforms (pure)
# ---------------------------------------------------------------------------
def rename_tokens(text: str, src_component: str, dst_component: str) -> str:
    """Blanket-rename the component token, e.g. ``mcp_proxy`` <-> ``mcp_proxy_dev``.

    When renaming the shorter token into the longer one (reset:
    ``mcp_proxy`` -> ``mcp_proxy_dev``), any literal that was already
    ``mcp_proxy_dev`` in the source (e.g. the sibling mutex id) becomes
    ``mcp_proxy_dev_dev``; a second pass collapses the doubled suffix back.
    """
    out = text.replace(src_component, dst_component)
    if dst_component.startswith(src_component) and dst_component != src_component:
        suffix = dst_component[len(src_component) :]  # e.g. "_dev"
        doubled = dst_component + suffix  # e.g. "mcp_proxy_dev_dev"
        while doubled in out:
            out = out.replace(doubled, dst_component)
    return out


def apply_start_py_identity(text: str, dst: Flavor) -> str:
    """Overwrite the flip-affected identity in start.py to the DST flavor.

    The four mutual-exclusion constants and the flavor-descriptor comment do
    NOT follow the blanket rename (the sibling relationship flips), so each is
    rewritten explicitly and robustly regardless of what the rename produced.
    """
    text = re.sub(
        r'^SIBLING_SLUG_BASE = "[^"]*"',
        f'SIBLING_SLUG_BASE = "{dst.sibling_slug_base}"',
        text,
        flags=re.M,
    )
    text = re.sub(
        r'^MUTEX_NOTIFICATION_ID = "[^"]*"',
        f'MUTEX_NOTIFICATION_ID = "{dst.mutex_notification_id}"',
        text,
        flags=re.M,
    )
    text = re.sub(
        r'^SIBLING_MUTEX_NOTIFICATION_ID = "[^"]*"',
        f'SIBLING_MUTEX_NOTIFICATION_ID = "{dst.sibling_mutex_notification_id}"',
        text,
        flags=re.M,
    )
    text = re.sub(
        r'^SIBLING_LABEL = "[^"]*"',
        f'SIBLING_LABEL = "{dst.sibling_label}"',
        text,
        flags=re.M,
    )
    text = re.sub(
        r"# This is the \w+ flavor: its sibling is the \w+ add-on\.",
        f"# This is the {dst.flavor_word} flavor: its sibling is the "
        f"{dst.sibling_word} add-on.",
        text,
    )
    return text


def apply_async_register_name(text: str, dst: Flavor) -> str:
    """Set the webhook display name (3rd positional arg to ``async_register``)."""
    return re.sub(
        r'(async_register\(\s*\n\s*hass,\s*\n\s*DOMAIN,\s*\n\s*)"[^"]*"',
        lambda m: m.group(1) + f'"{dst.async_register_name}"',
        text,
    )


def apply_dockerfile_labels(text: str, dst: Flavor) -> str:
    """Set the two ``io.hass.*`` labels (the COPY line is handled by rename)."""
    text = re.sub(
        r'io\.hass\.name="[^"]*"',
        f'io.hass.name="{dst.dockerfile_hass_name}"',
        text,
    )
    text = re.sub(
        r'io\.hass\.description="[^"]*"',
        f'io.hass.description="{dst.dockerfile_hass_description}"',
        text,
    )
    return text


# The one flavor-specific DOCS.md section, keyed by component. Everything else
# in DOCS.md is shared user documentation that the transform carries across
# (with the component token renamed); this block is each flavor's own identity
# wording and is swapped wholesale, like the start.py mutex constants.
# test_docs_banners_match_canonical keeps the committed files honest against
# these constants between syncs.
_DOCS_BANNER_RE = re.compile(r"^## Only one .*?(?=^## )", re.M | re.S)

DOCS_BANNERS = {
    "mcp_proxy": (
        "## Only one Webhook Proxy flavor runs at a time\n"
        "\n"
        "This add-on and its development counterpart, **Nabu Casa - Webhook Proxy for HA MCP\n"
        "(Dev)**, cannot run at the same time — they would collide over Home Assistant's root\n"
        "OAuth `/authorize` and `/token` routes. If you start this add-on while the **(Dev)**\n"
        "add-on is running, it refuses to start (a clear error in the add-on log plus a Home\n"
        "Assistant notification). Stop the (Dev) add-on first; the notification clears\n"
        "automatically on the next clean start.\n"
        "\n"
    ),
    "mcp_proxy_dev": (
        "## Only one flavor runs at a time (dev vs stable)\n"
        "\n"
        "This is the **dev** build. It is fully isolated from the stable Webhook Proxy add-on\n"
        "(separate integration, webhook URL, and OAuth credentials), but the two **cannot run at\n"
        "the same time** — they would collide over Home Assistant's root OAuth `/authorize` and\n"
        "`/token` routes. If you start this add-on while the stable **Webhook Proxy for HA MCP**\n"
        "add-on is running, it refuses to start (a clear error in the add-on log plus a Home\n"
        "Assistant notification). Stop the stable add-on first; the notification clears\n"
        "automatically on the next clean start.\n"
        "\n"
    ),
}


def transform_docs(src_text: str, src: Flavor, dst: Flavor) -> str:
    """Transform DOCS.md across flavors: token rename + flavor-banner swap.

    DOCS.md is shared user documentation; only the "Only one flavor runs at a
    time" section is flavor-specific, so after the component-token rename it
    is replaced with the destination's canonical block (DOCS_BANNERS).
    """
    renamed = rename_tokens(src_text, src.component, dst.component)
    # Lambda replacement: the banner is literal text, not a regex template.
    swapped, n = _DOCS_BANNER_RE.subn(lambda _m: DOCS_BANNERS[dst.component], renamed)
    if n != 1:
        raise ValueError(
            f"expected exactly one '## Only one …' banner section in "
            f"{src.addon_dir}/DOCS.md, found {n}"
        )
    return swapped


def apply_manifest(text: str, dst: Flavor, version: str) -> str:
    """Set ``domain``/``name``/``version`` in the component manifest.json."""
    text = re.sub(
        r'("domain":\s*")[^"]*(")',
        lambda m: m.group(1) + dst.component + m.group(2),
        text,
    )
    text = re.sub(
        r'("name":\s*")[^"]*(")',
        lambda m: m.group(1) + dst.manifest_name + m.group(2),
        text,
    )
    text = re.sub(
        r'("version":\s*")[^"]*(")',
        lambda m: m.group(1) + version + m.group(2),
        text,
    )
    return text


def transform_config_yaml(src_text: str, dst: Flavor, version: str) -> str:
    """Rebuild config.yaml from the SRC flavor's file, DST identity + version.

    Every non-identity key (arch/init/startup/hassio_*/host_network/map/
    options/schema/url) is preserved verbatim; only name/description/version/
    slug/boot/stage are set. ``stage: experimental`` is inserted right after
    ``url:`` for dev and removed for stable, matching the two files' layout.
    """
    lines = src_text.split("\n")

    def set_scalar(key: str, value: str, *, quote: bool = True) -> None:
        rendered = f'"{value}"' if quote else value
        for i, line in enumerate(lines):
            if re.match(rf"^{re.escape(key)}:(\s|$)", line):
                lines[i] = f"{key}: {rendered}"
                return
        raise ValueError(f"top-level key {key!r} not found in config.yaml")

    set_scalar("name", dst.config_name)
    set_scalar("description", dst.config_description)
    set_scalar("version", version)
    set_scalar("slug", dst.slug)
    set_scalar("boot", dst.boot, quote=False)

    stage_idx = next(
        (i for i, ln in enumerate(lines) if re.match(r"^stage:(\s|$)", ln)), None
    )
    if dst.stage is None:
        if stage_idx is not None:
            del lines[stage_idx]
    elif stage_idx is not None:
        lines[stage_idx] = f"stage: {dst.stage}"
    else:
        url_idx = next(
            (i for i, ln in enumerate(lines) if re.match(r"^url:(\s|$)", ln)), None
        )
        insert_at = url_idx + 1 if url_idx is not None else len(lines)
        lines.insert(insert_at, f"stage: {dst.stage}")

    return "\n".join(lines)


def profiles_are_inverse(a: Flavor, b: Flavor) -> bool:
    """True if two flavor profiles are exact identity inverses of each other."""
    return (
        a.slug == b.sibling_slug_base
        and b.slug == a.sibling_slug_base
        and a.mutex_notification_id == b.sibling_mutex_notification_id
        and b.mutex_notification_id == a.sibling_mutex_notification_id
        and a.flavor_word.lower() == b.sibling_word
        and b.flavor_word.lower() == a.sibling_word
        and a.sibling_label != b.sibling_label
        and a.component != b.component
    )


# ---------------------------------------------------------------------------
# Orchestration (filesystem I/O)
# ---------------------------------------------------------------------------
def _read_config_version(path: Path) -> str:
    m = re.search(r'^version:\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    if not m:
        raise ValueError(f"no version line in {path}")
    return m.group(1)


def _ruff_format(paths: list[Path], root: Path) -> None:
    """Run ``ruff format`` so token-length-driven line wrapping matches."""
    ruff = shutil.which("ruff")
    if ruff is None:
        raise RuntimeError(
            "ruff not found on PATH; it is required to normalize formatting "
            "after the token rename"
        )
    subprocess.run(
        [ruff, "format", *[str(p) for p in paths]],
        check=True,
        cwd=str(root),
        stdout=subprocess.DEVNULL,
    )


def sync(direction: str, bump: str | None = None, root: Path = REPO_ROOT) -> str:
    """Apply the promote/reset transform in place. Returns the new version."""
    if direction == "promote":
        src, dst = DEV, STABLE
        if bump is None:
            raise ValueError("--bump is required for --direction promote")
        version = promote_version(
            _read_config_version(root / STABLE.addon_dir / "config.yaml"), bump
        )
    elif direction == "reset":
        src, dst = STABLE, DEV
        version = reset_version(
            _read_config_version(root / DEV.addon_dir / "config.yaml"),
            _read_config_version(root / STABLE.addon_dir / "config.yaml"),
        )
    else:
        raise ValueError(f"unknown direction: {direction!r}")

    src_dir = root / src.addon_dir
    dst_dir = root / dst.addon_dir
    dst_comp = dst_dir / dst.component

    # 1. Replace the component dir + copy start.py / Dockerfile / translations.
    if dst_comp.exists():
        shutil.rmtree(dst_comp)
    shutil.copytree(
        src_dir / src.component,
        dst_comp,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(src_dir / "start.py", dst_dir / "start.py")
    shutil.copy2(src_dir / "Dockerfile", dst_dir / "Dockerfile")
    shutil.copy2(
        src_dir / "translations" / "en.yaml", dst_dir / "translations" / "en.yaml"
    )

    # DOCS.md joins the transform: shared content is carried across (token-
    # renamed) and the flavor-specific banner section is swapped to the
    # destination's canonical wording — no manual carry-over pass.
    docs_src = (src_dir / "DOCS.md").read_text(encoding="utf-8")
    (dst_dir / "DOCS.md").write_text(
        transform_docs(docs_src, src, dst), encoding="utf-8"
    )

    # 2. Blanket token rename on the copied code + Dockerfile + translations.
    rename_targets = [
        dst_dir / "start.py",
        dst_dir / "Dockerfile",
        dst_dir / "translations" / "en.yaml",
    ]
    rename_targets += [
        p
        for p in sorted(dst_comp.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]
    for path in rename_targets:
        original = path.read_text(encoding="utf-8")
        path.write_text(
            rename_tokens(original, src.component, dst.component), encoding="utf-8"
        )

    # 3. Overwrite the flip-affected identity in start.py.
    start_py = dst_dir / "start.py"
    start_py.write_text(
        apply_start_py_identity(start_py.read_text(encoding="utf-8"), dst),
        encoding="utf-8",
    )

    # 4. Overwrite the async_register display name.
    init_py = dst_comp / "__init__.py"
    init_py.write_text(
        apply_async_register_name(init_py.read_text(encoding="utf-8"), dst),
        encoding="utf-8",
    )

    # 5. Rebuild config.yaml from SRC + DST identity + version.
    config_yaml = dst_dir / "config.yaml"
    config_yaml.write_text(
        transform_config_yaml(
            (src_dir / "config.yaml").read_text(encoding="utf-8"), dst, version
        ),
        encoding="utf-8",
    )

    # 6. Set the Dockerfile labels.
    dockerfile = dst_dir / "Dockerfile"
    dockerfile.write_text(
        apply_dockerfile_labels(dockerfile.read_text(encoding="utf-8"), dst),
        encoding="utf-8",
    )

    # 7. Set manifest domain/name/version.
    manifest = dst_comp / "manifest.json"
    manifest.write_text(
        apply_manifest(manifest.read_text(encoding="utf-8"), dst, version),
        encoding="utf-8",
    )

    # Re-wrap .py lines whose length crossed 88 cols due to the token swap.
    _ruff_format([start_py, *sorted(dst_comp.rglob("*.py"))], root)
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Promote (dev -> stable) or reset (stable -> dev) the "
        "webhook-proxy add-on in place."
    )
    parser.add_argument("--direction", required=True, choices=["promote", "reset"])
    parser.add_argument(
        "--bump",
        choices=["patch", "minor", "major"],
        help="required for promote; ignored for reset",
    )
    args = parser.parse_args(argv)

    if args.direction == "promote" and args.bump is None:
        parser.error("--bump is required for --direction promote")

    new_version = sync(args.direction, args.bump)
    print(new_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
