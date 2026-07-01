"""Guards for the duplicated webhook-proxy-dev add-on.

The dev flavor is a hand-maintained copy of the stable add-on with the
`mcp_proxy` identity rewritten to `mcp_proxy_dev`. A single un-renamed token
would make the dev component collide with stable's domain/state. These tests
fail CI if that ever happens.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEV_ADDON = REPO_ROOT / "homeassistant-addon-webhook-proxy-dev"

# A bare `mcp_proxy` NOT immediately followed by `_dev`.
BARE_MCP_PROXY = re.compile(r"mcp_proxy(?!_dev)")

# Files that legitimately discuss the stable name (none expected by default).
SKIP_NAMES = {"CHANGELOG.md"}


def _dev_text_files() -> list[Path]:
    return [
        p
        for p in DEV_ADDON.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.name not in SKIP_NAMES
        and p.suffix in {".py", ".json", ".yaml", ".yml", ".md", ""}
    ]


def test_dev_addon_dir_exists():
    assert DEV_ADDON.is_dir(), f"missing dev add-on dir: {DEV_ADDON}"
    assert (DEV_ADDON / "mcp_proxy_dev").is_dir()


def test_no_bare_mcp_proxy_token_in_dev_tree():
    offenders: list[str] = []
    for path in _dev_text_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if BARE_MCP_PROXY.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}: {line.strip()}")
    assert not offenders, "bare `mcp_proxy` token(s) leaked into the dev tree:\n" + "\n".join(
        offenders
    )


def test_dev_slug_and_domain():
    cfg = (DEV_ADDON / "config.yaml").read_text(encoding="utf-8")
    assert 'slug: "ha_mcp_webhook_proxy_dev"' in cfg
    manifest = (DEV_ADDON / "mcp_proxy_dev" / "manifest.json").read_text(encoding="utf-8")
    assert '"domain": "mcp_proxy_dev"' in manifest
