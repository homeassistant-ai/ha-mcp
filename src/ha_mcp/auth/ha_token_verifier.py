"""Home Assistant token verification for add-on authentication.

This module provides token validation against Home Assistant's API
using the Supervisor proxy. This only works when running as a Home Assistant add-on.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import httpx
from fastmcp.server.auth import AccessToken, TokenVerifier

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)


class HATokenVerifier(TokenVerifier):
    """Validates Home Assistant long-lived access tokens.

    This verifier works only in Home Assistant add-on context where
    the Supervisor proxy is available at http://supervisor/core/api/.

    Users provide their HA long-lived access token (created in Profile → Security),
    and this verifier validates it against the Home Assistant API.
    """

    def __init__(
        self,
        base_url: str | None = None,
        required_scopes: list[str] | None = None,
    ):
        """Initialize the HA token verifier.

        Args:
            base_url: Ignored - add-on always uses http://supervisor/core/api/
            required_scopes: Scopes required for all requests (currently unused)
        """
        super().__init__(base_url=base_url, required_scopes=required_scopes or [])

        # Verify we're running in add-on context
        if not os.getenv("SUPERVISOR_TOKEN"):
            _LOGGER.warning(
                "SUPERVISOR_TOKEN not found - HATokenVerifier only works in add-on context"
            )

        self.ha_api_url = "http://supervisor/core/api/"

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a Home Assistant long-lived access token.

        Args:
            token: The HA long-lived access token to validate

        Returns:
            AccessToken if valid, None if invalid or expired

        Note:
            This makes a request to the Home Assistant API using the token.
            If the API returns 200, the token is valid. If 401, it's invalid.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    self.ha_api_url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    _LOGGER.debug("Token validation successful")
                    return AccessToken(
                        token=token,
                        client_id="ha-user",
                        scopes=self.required_scopes,
                    )
                elif resp.status_code == 401:
                    _LOGGER.warning("Token validation failed: invalid token")
                    return None
                else:
                    _LOGGER.error(
                        f"Token validation failed: unexpected status {resp.status_code}"
                    )
                    return None

        except httpx.HTTPError as e:
            _LOGGER.error(f"Token validation failed: {e}")
            return None
        except Exception as e:
            _LOGGER.error(f"Unexpected error during token validation: {e}")
            return None
