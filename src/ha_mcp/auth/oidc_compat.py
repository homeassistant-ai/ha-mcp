"""FastMCP OIDC compatibility behavior scoped to ha-mcp's OIDC entrypoint."""

from __future__ import annotations

import logging
from typing import override

from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from mcp.server.auth.provider import AuthorizationParams, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

logger = logging.getLogger(__name__)


class HaMcpOIDCProxy(OIDCProxy):
    """OIDC proxy with ha-mcp's FastMCP 3.4.6 compatibility behavior."""

    def _add_required_scopes(self, scopes: list[str]) -> list[str]:
        """Prepend any configured required scopes missing from ``scopes``."""
        missing_scopes = [
            scope for scope in self.required_scopes if scope not in scopes
        ]
        return [*missing_scopes, *scopes]

    def setup_scope_compatibility(self) -> None:
        """Allow optional DCR scopes without advertising them as requirements.

        FastMCP 3.4.6 copies ``required_scopes`` into both the DCR default and
        valid-scope allow-list.  The MCP SDK then rejects clients that add
        ordinary optional OIDC scopes.  ``None`` restores upstream IdP scope
        validation while FastMCP continues to default, advertise, and enforce
        ha-mcp's required ``openid`` scope.
        """
        if self.required_scopes != ["openid"]:
            raise RuntimeError(
                "HaMcpOIDCProxy requires exactly the openid scope before setup"
            )
        options = self.client_registration_options
        if options is None:
            raise RuntimeError("FastMCP OIDCProxy did not enable client registration")
        if options.default_scopes != ["openid"] or self._default_scope_str != "openid":
            raise RuntimeError("FastMCP OIDCProxy did not preserve the openid default")
        options.valid_scopes = None

    @override
    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Persist DCR clients with every required scope included."""
        current_scopes = client_info.scope.split() if client_info.scope else []
        normalized_scopes = self._add_required_scopes(current_scopes)
        if normalized_scopes != current_scopes:
            client_info.scope = " ".join(normalized_scopes)
        await super().register_client(client_info)

    @override
    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Forward every authorization with required scopes included."""
        current_scopes = params.scopes or []
        normalized_scopes = self._add_required_scopes(current_scopes)
        if normalized_scopes != current_scopes:
            params = params.model_copy(update={"scopes": normalized_scopes})
        return await super().authorize(client, params)

    @override
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Load a client, adding required scopes to persisted legacy records once."""
        was_persisted = await self._client_store.get(key=client_id) is not None
        client = await super().get_client(client_id)
        if client is None or not was_persisted:
            return client
        if not isinstance(client, ProxyDCRClient):
            raise RuntimeError("FastMCP returned an unexpected persisted client type")

        current_scopes = client.scope.split() if client.scope else []
        normalized_scopes = self._add_required_scopes(current_scopes)
        if normalized_scopes == current_scopes:
            return client

        migrated = client.model_copy(update={"scope": " ".join(normalized_scopes)})
        await self._client_store.put(key=client_id, value=migrated)
        return migrated

    @override
    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """Reject legacy refresh tokens that predate required OIDC scopes."""
        loaded = await super().load_refresh_token(client, refresh_token)
        if loaded is None:
            return None
        missing_scopes = set(self.required_scopes) - set(loaded.scopes)
        if missing_scopes:
            logger.debug(
                "Rejecting legacy refresh token for client_id=%s: "
                "missing required scopes %s",
                client.client_id,
                sorted(missing_scopes),
            )
            return None
        return loaded
