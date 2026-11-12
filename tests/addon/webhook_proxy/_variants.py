"""The two webhook proxy flavors, and what each one's source ships.

A variant-specific VALUE (a path, slug, domain or route base) lives in the
``Variant`` table. A variant-specific CAPABILITY is feature-detected from the
flavor's own source instead: the promote workflow copies dev code onto stable
but never edits these tests, so a capability recorded as a table value would
stay off for stable after the code arrives.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Variant:
    key: str
    addon_dir: Path
    component: str
    slug: str
    sibling_base: str
    mutex_id: str

    @property
    def domain(self) -> str:
        return self.component

    @property
    def component_dir(self) -> Path:
        return self.addon_dir / self.component

    @property
    def oauth_base(self) -> str:
        return f"/api/{self.component}/oauth"

    @property
    def config_file(self) -> str:
        return f"/config/.{self.component}_config.json"

    @property
    def inbound_log(self) -> str:
        return f"/config/.{self.component}_inbound.log"

    @property
    def oauth_marker(self) -> str:
        return f"/config/.{self.component}_oauth_restart_required"

    def component_src(self, name: str) -> str:
        """Source of a component module, "" when this flavor lacks it."""
        path = self.component_dir / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def start_src(self) -> str:
        return (self.addon_dir / "start.py").read_text(encoding="utf-8")


VARIANTS = (
    Variant(
        key="stable",
        addon_dir=REPO_ROOT / "homeassistant-addon-webhook-proxy",
        component="mcp_proxy",
        slug="ha_mcp_webhook_proxy",
        sibling_base="ha_mcp_webhook_proxy_dev",
        mutex_id="mcp_proxy_mutex",
    ),
    Variant(
        key="dev",
        addon_dir=REPO_ROOT / "homeassistant-addon-webhook-proxy-dev",
        component="mcp_proxy_dev",
        slug="ha_mcp_webhook_proxy_dev",
        sibling_base="ha_mcp_webhook_proxy",
        mutex_id="mcp_proxy_dev_mutex",
    ),
)


def load_start(variant: Variant) -> ModuleType:
    """Load a fresh copy of the flavor's ``start.py``, the app's process."""
    spec = importlib.util.spec_from_file_location(
        f"webhook_proxy_start_{variant.key}", variant.addon_dir / "start.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def relay_total_unbounded(variant: Variant) -> bool:
    """The relay session drops the wall-clock ``total`` bound so long-lived
    MCP response streams survive."""
    return "Deliberately NO wall-clock" in variant.component_src("__init__.py")


def ha_auth_supported(variant: Variant) -> bool:
    return (variant.component_dir / "auth_native.py").exists()


def none_autoapprove_supported(variant: Variant) -> bool:
    return (variant.component_dir / "oauth_autoapprove.py").exists()


def unified_oauth_routes(variant: Variant) -> bool:
    """The unified scoped authorize/token dispatchers. The scoped URL alone is
    not distinguishing (the none-mode view that predates unification binds the
    same path), so the marker is the legacy handler they dispatch into."""
    return "handle_legacy_authorize_get" in variant.component_src(
        "oauth_autoapprove.py"
    )


def dcr_registration_supported(variant: Variant) -> bool:
    return "DcrRegisterView" in variant.component_src("oauth_dcr.py")


def scoped_revoke_supported(variant: Variant) -> bool:
    """The scoped RFC 7009 revocation dispatcher (#2248)."""
    return "AutoApproveRevokeView" in variant.component_src("oauth_autoapprove.py")


def dedicated_cimd_session(variant: Variant) -> bool:
    return "cimd_session" in variant.component_src("__init__.py")


def proxy_owned_oauth_endpoints(variant: Variant) -> bool:
    """The ha_auth AS document advertises endpoints under the proxy's OAuth
    base rather than Core's own /auth/* routes."""
    return '"authorization_endpoint": f"{base}{OAUTH_BASE}/authorize"' in (
        variant.component_src("auth_native.py")
    )


def autoapprove_any_redirect(variant: Variant) -> bool:
    """None mode accepts any valid redirect (the client allowlist retired)."""
    return "without a client allowlist" in variant.component_src("oauth_autoapprove.py")


def rfc9207_iss_supported(variant: Variant) -> bool:
    """Authorization responses carry the RFC 9207 ``iss`` parameter."""
    return "def _issuer(" in variant.component_src("oauth_autoapprove.py")


def webhook_logger_supported(variant: Variant) -> bool:
    """The component raises Core's webhook logger alongside its own."""
    return "_WEBHOOK_LOGGER" in variant.component_src("__init__.py")


def cloudhook_relay_supported(variant: Variant) -> bool:
    return (variant.component_dir / "cloudhook.py").exists()
