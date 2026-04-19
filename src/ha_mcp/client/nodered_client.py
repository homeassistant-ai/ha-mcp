"""
Node-RED Admin API client with HTTP Basic authentication and structured error handling.

Mirrors the design of `rest_client.py` so the Node-RED tool layer can rely on the
same exception types that ``helpers.exception_to_structured_error`` already
classifies. The Node-RED Admin API is enabled by default on Home Assistant
Node-RED add-on installations and is reachable through the same NGINX proxy as
Home Assistant itself (typically at `/nodered-api/`).
"""

import logging
from typing import Any

import httpx

from ..config import get_global_settings

logger = logging.getLogger(__name__)


class NodeRedError(Exception):
    """Base exception for Node-RED API errors."""


class NodeRedConnectionError(NodeRedError):
    """Connection error to the Node-RED Admin API."""


class NodeRedAuthError(NodeRedError):
    """Authentication error with the Node-RED Admin API."""


class NodeRedAPIError(NodeRedError):
    """API error returned by Node-RED."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class NodeRedClient:
    """Authenticated HTTP client for the Node-RED Admin API."""

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: int | None = None,
    ) -> None:
        if base_url is None or username is None or password is None:
            settings = get_global_settings()
            self.base_url = (base_url or settings.nodered_url).rstrip("/")
            self.username = username or settings.nodered_username
            self.password = password or settings.nodered_password
            self.timeout = timeout if timeout is not None else settings.timeout
        else:
            self.base_url = base_url.rstrip("/")
            self.username = username
            self.password = password
            self.timeout = timeout if timeout is not None else 30

        self.httpx_client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=(self.username, self.password),
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(self.timeout),
        )

        logger.info("Initialised Node-RED client for %s", self.base_url)

    async def __aenter__(self) -> "NodeRedClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self.httpx_client.aclose()
        logger.debug("Closed Node-RED client")

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_body: Any = None,
        deployment_type: str = "full",
    ) -> Any:
        """Issue an authenticated request and return the parsed body.

        For POST/PUT calls the Node-RED `Node-RED-Deployment-Type` header is set
        so partial deploys behave consistently with the upstream Node-RED
        editor's default. Returns parsed JSON when the response advertises a
        JSON content-type, otherwise returns response text.
        """
        path = "/" + endpoint.lstrip("/")
        method_upper = method.upper()

        headers: dict[str, str] = {}
        if method_upper in {"POST", "PUT", "DELETE"}:
            headers["Node-RED-Deployment-Type"] = deployment_type

        try:
            response = await self.httpx_client.request(
                method_upper, path, json=json_body, headers=headers
            )
        except httpx.TimeoutException as e:
            raise NodeRedConnectionError(
                f"Node-RED request timeout after {self.timeout}s"
            ) from e
        except httpx.RequestError as e:
            raise NodeRedConnectionError(f"Failed to connect to Node-RED: {e}") from e

        if response.status_code in (401, 403):
            raise NodeRedAuthError(
                f"Node-RED rejected the credentials (HTTP {response.status_code})"
            )

        if response.status_code >= 400:
            raise NodeRedAPIError(
                f"Node-RED API error: {response.status_code} - {response.text}",
                status_code=response.status_code,
                response_text=response.text,
            )

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    async def get_flows(self) -> list[dict[str, Any]]:
        """Return the full `/flows` array (tabs, nodes and config nodes)."""
        result = await self._request("GET", "/flows")
        if not isinstance(result, list):
            raise NodeRedAPIError(
                "Unexpected /flows response shape (expected a JSON array)"
            )
        return result

    async def post_flows(self, flows: list[dict[str, Any]]) -> Any:
        """Replace the entire `/flows` array. Returns the deployment revision."""
        return await self._request("POST", "/flows", json_body=flows)

    async def get_settings(self) -> dict[str, Any]:
        """Return the Node-RED runtime `/settings` payload."""
        result = await self._request("GET", "/settings")
        if not isinstance(result, dict):
            raise NodeRedAPIError(
                "Unexpected /settings response shape (expected a JSON object)"
            )
        return result

    async def inject(self, node_id: str) -> Any:
        """Trigger an inject node by ID via `/inject/<id>`."""
        return await self._request("POST", f"/inject/{node_id}")
