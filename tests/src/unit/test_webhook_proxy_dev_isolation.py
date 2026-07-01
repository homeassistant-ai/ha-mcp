"""Guards for the duplicated webhook-proxy-dev add-on.

The dev flavor is a hand-maintained copy of the stable add-on with the
`mcp_proxy` identity rewritten to `mcp_proxy_dev`. A single un-renamed token
would make the dev component collide with stable's domain/state. These tests
fail CI if that ever happens.
"""

import importlib.util
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
    assert not offenders, (
        "bare `mcp_proxy` token(s) leaked into the dev tree:\n" + "\n".join(offenders)
    )


def test_dev_slug_and_domain():
    cfg = (DEV_ADDON / "config.yaml").read_text(encoding="utf-8")
    assert 'slug: "ha_mcp_webhook_proxy_dev"' in cfg
    manifest = (DEV_ADDON / "mcp_proxy_dev" / "manifest.json").read_text(
        encoding="utf-8"
    )
    assert '"domain": "mcp_proxy_dev"' in manifest


STABLE_START = REPO_ROOT / "homeassistant-addon-webhook-proxy" / "start.py"


def _load_stable_start():
    spec = importlib.util.spec_from_file_location("wp_stable_start", STABLE_START)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # safe: module-level has no side effects
    return mod


def test_sibling_matcher_handles_hash_prefix_and_disambiguates_dev():
    start = _load_stable_start()
    # stable looks for the dev sibling:
    dev_base = "ha_mcp_webhook_proxy_dev"
    started_dev = [{"slug": "abc12345_ha_mcp_webhook_proxy_dev", "state": "started"}]
    stopped_dev = [{"slug": "abc12345_ha_mcp_webhook_proxy_dev", "state": "stopped"}]
    only_stable = [{"slug": "abc12345_ha_mcp_webhook_proxy", "state": "started"}]
    assert start._sibling_is_running(started_dev, dev_base) is True
    assert start._sibling_is_running(stopped_dev, dev_base) is False
    # a running STABLE slug must NOT be mistaken for the dev sibling:
    assert start._sibling_is_running(only_stable, dev_base) is False
    # exact (non-hashed official) slug also matches:
    assert (
        start._sibling_is_running(
            [{"slug": "ha_mcp_webhook_proxy_dev", "state": "started"}], dev_base
        )
        is True
    )


DEV_START = DEV_ADDON / "start.py"


def _load_dev_start():
    spec = importlib.util.spec_from_file_location("wp_dev_start", DEV_START)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # safe: module-level has no side effects
    return mod


def test_dev_sibling_matcher_detects_stable_not_itself():
    start = _load_dev_start()
    # The dev flavor's sibling is STABLE (not its own dev slug).
    assert start.SIBLING_SLUG_BASE == "ha_mcp_webhook_proxy"
    assert start.MUTEX_NOTIFICATION_ID == "mcp_proxy_dev_mutex"
    stable_base = "ha_mcp_webhook_proxy"
    running_stable = [{"slug": "abc12345_ha_mcp_webhook_proxy", "state": "started"}]
    running_dev_self = [
        {"slug": "abc12345_ha_mcp_webhook_proxy_dev", "state": "started"}
    ]
    assert start._sibling_is_running(running_stable, stable_base) is True
    # dev must NOT treat its own (dev) slug as the stable sibling:
    assert start._sibling_is_running(running_dev_self, stable_base) is False
