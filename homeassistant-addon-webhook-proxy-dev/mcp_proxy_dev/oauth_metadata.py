"""Provider contract shared by the OAuth discovery views."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aiohttp import web


class MetadataProvider(Protocol):
    """Interface the mode-aware discovery-document views need from a provider.

    Satisfied structurally by both `OAuthProvider` (legacy) and
    `auth_native.ResourceServer` (ha_auth). The views additionally read the
    implementation's `_hass` via ``getattr`` (see `_active_oauth_mode` /
    `_active_provider`), which a Protocol cannot express for a private
    attribute — both implementations carry it.
    """

    @property
    def webhook_id(self) -> str:
        """This install's private webhook id."""

    def resource_url(self, base_url: str) -> str:
        """Absolute URL of the protected webhook resource under ``base_url``."""

    def authorization_server_url(self, base_url: str) -> str:
        """Issuer / authorization-server URL under ``base_url``."""

    def base_url_for(self, request: web.Request) -> str:
        """Public base URL for ``request`` per the provider's policy
        (legacy: pinned to the configured URL; ha_auth: request-host-derived)."""
