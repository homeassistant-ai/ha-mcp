"""
Home Assistant HTTP client with authentication and error handling.
"""

import asyncio
import json
import logging
import ssl
from typing import Any

import httpx

from ..config import get_global_settings


def _is_ssl_error(exc: BaseException) -> bool:
    """True if ``exc`` (or anything in its cause chain) is an SSL error.

    httpx wraps ``ssl.SSLError`` inside ``httpx.ConnectError``; the only
    reliable check is to walk ``__cause__`` / ``__context__``.
    """
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ssl.SSLError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False

logger = logging.getLogger(__name__)


class HomeAssistantError(Exception):
    """Base exception for Home Assistant API errors."""



class HomeAssistantConnectionError(HomeAssistantError):
    """Connection error to Home Assistant."""



class HomeAssistantAuthError(HomeAssistantError):
    """Authentication error with Home Assistant."""



class HomeAssistantAPIError(HomeAssistantError):
    """API error from Home Assistant."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_data: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class HomeAssistantCommandError(HomeAssistantError):
    """WebSocket command returned success=False.

    Raised by ``WebSocketClient.send_command`` when Home Assistant responds
    with ``{type: "result", success: False}``. Used as a type marker in
    ``_classify_exception``'s match dispatch; classification then falls
    through to ``_classify_by_message`` for pattern matching on the
    error message.
    """


class HomeAssistantClient:
    """Authenticated HTTP client for Home Assistant API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: int | None = None,
        verify_ssl: bool | None = None,
    ):
        """
        Initialize Home Assistant client.

        Args:
            base_url: Home Assistant URL (defaults to config)
            token: Long-lived access token (defaults to config)
            timeout: Request timeout in seconds (defaults to config)
            verify_ssl: Whether to verify the HA server's TLS certificate
                (defaults to ``settings.verify_ssl``). Pass False to allow
                self-signed certs or hostname mismatches.
        """
        if base_url is None or token is None or verify_ssl is None:
            settings = get_global_settings()
            self.base_url = (base_url or settings.homeassistant_url).rstrip("/")
            self.token = token or settings.homeassistant_token
            self.timeout = timeout if timeout is not None else settings.timeout
            self.verify_ssl = (
                verify_ssl if verify_ssl is not None else settings.verify_ssl
            )
        else:
            self.base_url = base_url.rstrip("/")
            self.token = token
            self.timeout = timeout if timeout is not None else 30  # Default timeout
            self.verify_ssl = verify_ssl

        if not self.verify_ssl:
            logger.warning(
                "TLS verification disabled for Home Assistant REST client "
                "(HA_VERIFY_SSL=false). Connections to %s will accept "
                "self-signed and mismatched certificates.",
                self.base_url,
            )

        # Create HTTP client with authentication headers
        self.httpx_client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(self.timeout),
            verify=self.verify_ssl,
        )

        logger.info(f"Initialized Home Assistant client for {self.base_url}")

    async def __aenter__(self) -> 'HomeAssistantClient':
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def close(self) -> None:
        """Close HTTP client."""
        await self.httpx_client.aclose()
        logger.debug("Closed Home Assistant client")

    async def _raw_request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> httpx.Response:
        """Authenticated request that returns the raw httpx.Response.

        Handles auth, HTTP 4xx/5xx, and transport errors in one place.
        Callers parse the body themselves (JSON via `_request`, text via
        `get_addon_logs`, etc.).

        Raises:
            HomeAssistantAuthError: 401 response.
            HomeAssistantAPIError: Non-2xx response (with status_code and
                response_data set from JSON body when possible).
            HomeAssistantConnectionError: Network, timeout, or transport error.
        """
        try:
            response = await self.httpx_client.request(method, endpoint, **kwargs)

            if response.status_code == 401:
                raise HomeAssistantAuthError("Invalid authentication token")

            if response.status_code >= 400:
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {"message": response.text}

                message = error_data.get("message")
                if not message or not message.strip():
                    message = response.reason_phrase or "<empty body>"

                raise HomeAssistantAPIError(
                    f"API error: {response.status_code} - {message}",
                    status_code=response.status_code,
                    response_data=error_data,
                )

            return response

        except httpx.ConnectError as e:
            if _is_ssl_error(e) and self.verify_ssl:
                raise HomeAssistantConnectionError(
                    f"TLS verification failed for {self.base_url}: {e}. "
                    "If this is a self-signed certificate or hostname "
                    "mismatch, set HA_VERIFY_SSL=false to skip verification."
                ) from e
            raise HomeAssistantConnectionError(
                f"Failed to connect to Home Assistant: {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise HomeAssistantConnectionError(f"Request timeout: {e}") from e
        except httpx.HTTPError as e:
            raise HomeAssistantConnectionError(f"HTTP error: {e}") from e

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """
        Make authenticated request to Home Assistant API and parse JSON body.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (without /api prefix)
            **kwargs: Additional arguments for httpx request

        Returns:
            Response data as dictionary

        Raises:
            HomeAssistantConnectionError: Connection failed
            HomeAssistantAuthError: Authentication failed
            HomeAssistantAPIError: API error
        """
        response = await self._raw_request(method, endpoint, **kwargs)
        try:
            result: dict[str, Any] = response.json()
            return result
        except json.JSONDecodeError:
            # Some endpoints return empty responses
            return {}

    async def get_config(self) -> dict[str, Any]:
        """Get Home Assistant configuration."""
        logger.debug("Fetching Home Assistant configuration")
        return await self._request("GET", "/config")

    async def get_states(self) -> list[dict[str, Any]]:
        """Get all entity states."""
        logger.debug("Fetching all entity states")
        result = await self._request("GET", "/states")
        if isinstance(result, list):
            return result
        else:
            return []

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        """
        Get specific entity state.

        Args:
            entity_id: Entity ID (e.g., 'light.living_room')

        Returns:
            Entity state data
        """
        logger.debug(f"Fetching state for entity: {entity_id}")
        return await self._request("GET", f"/states/{entity_id}")

    async def set_entity_state(
        self, entity_id: str, state: str, attributes: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Set entity state.

        Args:
            entity_id: Entity ID
            state: New state value
            attributes: Optional attributes dictionary

        Returns:
            Updated entity state
        """
        logger.debug(f"Setting state for entity {entity_id} to {state}")

        payload: dict[str, Any] = {"state": state}
        if attributes:
            payload["attributes"] = attributes

        return await self._request("POST", f"/states/{entity_id}", json=payload)

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any] | None = None,
        return_response: bool = False
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """
        Call Home Assistant service.

        Args:
            domain: Service domain (e.g., 'light', 'climate')
            service: Service name (e.g., 'turn_on', 'set_temperature')
            data: Optional service data
            return_response: If True, returns the service response data (for services
                           that support SupportsResponse.ONLY or SupportsResponse.OPTIONAL)

        Returns:
            Service response data - list of affected states normally, or dict with
            service response if return_response=True
        """
        logger.debug(f"Calling service {domain}.{service} (return_response={return_response})")

        payload = data or {}

        # Build query params for return_response
        params = {}
        if return_response:
            params["return_response"] = "true"

        result = await self._request(
            "POST", f"/services/{domain}/{service}", json=payload, params=params if params else None
        )

        # When return_response is True, HA returns a dict with service_response key
        if return_response:
            if isinstance(result, dict):
                return result
            return {"service_response": result}

        # Normal behavior: return list of affected states
        if isinstance(result, list):
            return result
        else:
            return []

    async def get_services(self) -> dict[str, Any]:
        """Get all available services."""
        logger.debug("Fetching available services")
        return await self._request("GET", "/services")

    async def get_history(
        self,
        entity_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[list[dict[str, Any]]]:
        """
        Get historical data.

        Args:
            entity_id: Optional entity ID to filter
            start_time: Optional start time (ISO format)
            end_time: Optional end time (ISO format)

        Returns:
            Historical data
        """
        logger.debug(f"Fetching history for entity: {entity_id}")

        params = {}
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time

        endpoint = "/history/period"
        if entity_id:
            endpoint += f"/{entity_id}"

        result = await self._request("GET", endpoint, params=params)
        if isinstance(result, list):
            return result
        else:
            return []

    async def get_logbook(
        self,
        entity_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get logbook entries.

        Args:
            entity_id: Optional entity ID to filter
            start_time: Optional start time (ISO format) - used as URL path component
            end_time: Optional end time (ISO format) - used as query parameter

        Returns:
            Logbook entries
        """
        logger.debug(f"Fetching logbook entries for entity: {entity_id}, start: {start_time}, end: {end_time}")

        # Build endpoint - start_time goes in URL path if provided
        if start_time:
            endpoint = f"/logbook/{start_time}"
        else:
            endpoint = "/logbook"

        # Build query parameters
        params = {}
        if entity_id:
            params["entity"] = entity_id
        if end_time:
            params["end_time"] = end_time

        result = await self._request("GET", endpoint, params=params)
        if isinstance(result, list):
            return result
        else:
            return []

    async def fire_event(
        self, event_type: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Fire Home Assistant event.

        Args:
            event_type: Event type name
            data: Optional event data

        Returns:
            Event response
        """
        logger.debug(f"Firing event: {event_type}")

        payload = data or {}
        return await self._request("POST", f"/events/{event_type}", json=payload)

    async def render_template(self, template: str) -> str:
        """
        Render Home Assistant template.

        Args:
            template: Template string

        Returns:
            Rendered template
        """
        logger.debug("Rendering template")

        payload = {"template": template}
        response = await self._request("POST", "/template", json=payload)
        result = response.get("result")
        return str(result) if result is not None else ""

    async def check_config(self) -> dict[str, Any]:
        """Check Home Assistant configuration."""
        logger.debug("Checking configuration")
        return await self._request("POST", "/config/core/check_config")

    async def get_error_log(self) -> str:
        """Get Home Assistant error log."""
        logger.debug("Fetching error log")
        response = await self._request("GET", "/error_log")
        return response if isinstance(response, str) else str(response)

    async def get_addon_logs(self, slug: str) -> str:
        """Fetch an add-on's container logs via HA Core's Supervisor REST proxy.

        Uses `/api/hassio/addons/{slug}/logs`, which HA Core proxies to
        Supervisor and returns as `text/plain`. This avoids the
        `supervisor/api` websocket path that tries to JSON-decode the text
        body and always fails (see #950).

        Raises:
            HomeAssistantAuthError: 401 from HA Core.
            HomeAssistantAPIError: Non-2xx response (e.g. 404 unknown slug,
                400 addon not installed). `status_code` is set so callers
                can map to specific suggestions.
            HomeAssistantConnectionError: Network, timeout, or transport error.
        """
        logger.debug(f"Fetching addon logs for slug={slug}")
        response = await self._raw_request(
            "GET",
            f"/hassio/addons/{slug}/logs",
            headers={"Accept": "text/plain"},
        )
        return response.text

    async def test_connection(self) -> tuple[bool, str | None]:
        """
        Test connection to Home Assistant.

        Returns:
            tuple: (success, error_message)
        """
        try:
            config = await self.get_config()
            if config.get("location_name"):
                logger.info(
                    f"Successfully connected to Home Assistant: {config['location_name']}"
                )
                return True, None
            else:
                return False, "Invalid response from Home Assistant"
        except Exception as e:
            logger.error(f"Failed to connect to Home Assistant: {e}")
            return False, str(e)

    async def get_system_health(self) -> dict[str, Any]:
        """Get system health information."""
        logger.debug("Fetching system health")
        try:
            return await self._request("GET", "/system_health/info")
        except HomeAssistantAPIError:
            # System health might not be available in all HA instances
            return {"status": "unknown", "message": "System health not available"}

    # Automation Configuration Management

    async def _resolve_automation_id(self, identifier: str) -> str:
        """
        Convert entity_id to unique_id if needed, or return unique_id as-is.

        Args:
            identifier: Either entity_id (automation.xxx) or unique_id

        Returns:
            The unique_id for configuration API

        Raises:
            HomeAssistantAPIError: If automation not found
        """
        # If it looks like an entity_id, convert to unique_id
        if identifier.startswith("automation."):
            try:
                state = await self.get_entity_state(identifier)
                unique_id = state.get("attributes", {}).get("id")
                if not unique_id:
                    raise HomeAssistantAPIError(
                        f"Automation {identifier} has no unique_id attribute",
                        status_code=404,
                    )
                logger.debug(
                    f"Converted entity_id {identifier} to unique_id {unique_id}"
                )
                return str(unique_id)
            except Exception as e:
                raise HomeAssistantAPIError(
                    f"Failed to resolve automation {identifier}: {str(e)}",
                    status_code=404,
                ) from e
        else:
            # Assume it's already a unique_id
            return identifier

    async def get_automation_config(self, identifier: str) -> dict[str, Any]:
        """
        Get automation configuration by unique_id or entity_id.

        Args:
            identifier: Either automation entity_id (automation.xxx) or unique_id

        Returns:
            Automation configuration dictionary

        Raises:
            HomeAssistantAPIError: If automation not found or API error
        """
        unique_id = await self._resolve_automation_id(identifier)
        logger.debug(f"Fetching automation config for unique_id: {unique_id}")

        try:
            response = await self._request(
                "GET", f"/config/automation/config/{unique_id}"
            )
            return response
        except Exception as e:
            if "404" in str(e):
                raise HomeAssistantAPIError(
                    f"Automation not found: {identifier} (unique_id: {unique_id})",
                    status_code=404,
                ) from e
            raise

    async def upsert_automation_config(
        self, config: dict[str, Any], identifier: str | None = None
    ) -> dict[str, Any]:
        """
        Create new automation or update existing one.

        Args:
            config: Automation configuration dictionary
            identifier: Optional automation entity_id or unique_id (None = create new)

        Returns:
            Result with automation unique_id and status

        Raises:
            HomeAssistantAPIError: If configuration invalid or API error
        """
        import time

        # Generate unique_id for new automation if not provided
        if identifier is None:
            unique_id = str(int(time.time() * 1000))
            operation = "created"
            logger.debug(f"Creating new automation with unique_id: {unique_id}")
        else:
            unique_id = await self._resolve_automation_id(identifier)
            operation = "updated"
            logger.debug(f"Updating automation with unique_id: {unique_id}")

        # Add unique_id to config for updates
        if unique_id and "id" not in config:
            config = {**config, "id": unique_id}

        try:
            response = await self._request(
                "POST", f"/config/automation/config/{unique_id}", json=config
            )

            # For new automations, query Home Assistant to get the actual entity_id that was assigned
            actual_entity_id = None
            entity_not_verified = False
            if operation == "created":
                actual_entity_id = await self._poll_for_automation_entity(unique_id)
                if not actual_entity_id:
                    entity_not_verified = True

            result: dict[str, Any] = {
                "unique_id": unique_id,
                "entity_id": actual_entity_id,
                "result": response.get("result", "ok"),
                "operation": operation,
            }
            if entity_not_verified:
                result["entity_not_verified"] = True
            return result
        except Exception as e:
            if "400" in str(e):
                raise HomeAssistantAPIError(
                    f"Invalid automation configuration: {str(e)}", status_code=400
                ) from e
            raise

    async def _poll_for_automation_entity(self, unique_id: str) -> str | None:
        """Poll HA state to find the entity_id assigned to a newly created automation."""
        try:
            for attempt in range(3):
                await asyncio.sleep(1 * (attempt + 1))
                states = await self.get_states()
                for state in states:
                    if not state.get("entity_id", "").startswith("automation."):
                        continue
                    if state.get("attributes", {}).get("id") == unique_id:
                        entity_id = state.get("entity_id")
                        logger.debug(
                            f"Found actual entity_id for unique_id {unique_id}: {entity_id}"
                        )
                        return entity_id
        except Exception as e:
            logger.warning(
                f"Failed to query actual entity_id for unique_id {unique_id}: {e}"
            )
            return None

        logger.warning(
            f"Automation with unique_id {unique_id} was not found in HA state after creation"
        )
        return None

    async def delete_automation_config(self, identifier: str) -> dict[str, Any]:
        """
        Delete automation configuration by entity_id or unique_id.

        Args:
            identifier: Either automation entity_id (automation.xxx) or unique_id

        Returns:
            Deletion result

        Raises:
            HomeAssistantAPIError: If automation not found or API error
        """
        unique_id = await self._resolve_automation_id(identifier)
        logger.debug(f"Deleting automation config for unique_id: {unique_id}")

        try:
            response = await self._request(
                "DELETE", f"/config/automation/config/{unique_id}"
            )
            return {
                "identifier": identifier,
                "unique_id": unique_id,
                "result": response.get("result", "ok"),
                "operation": "deleted",
            }
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                raise HomeAssistantAPIError(
                    f"Automation not found: {identifier} (unique_id: {unique_id})",
                    status_code=404,
                ) from e
            elif e.status_code == 405:
                raise HomeAssistantAPIError(
                    f"Cannot delete automation '{identifier}': The HTTP DELETE method is blocked. "
                    f"This typically occurs when running ha-mcp as a Home Assistant add-on, because "
                    f"the Supervisor ingress proxy only allows GET and POST requests. "
                    f"WORKAROUNDS: "
                    f"(1) Use ha-mcp via pip, Docker, or as an external MCP server instead of the add-on. "
                    f"(2) Use a long-lived access token to connect directly to Home Assistant's API. "
                    f"(3) As a fallback, disable the automation and rename it with a 'DELETE_' prefix "
                    f"(e.g., 'DELETE_{identifier}') so you can identify and manually delete it later "
                    f"via the Home Assistant UI (Settings > Automations & Scenes).",
                    status_code=405,
                ) from e
            raise
        except Exception as e:
            if "404" in str(e):
                raise HomeAssistantAPIError(
                    f"Automation not found: {identifier} (unique_id: {unique_id})",
                    status_code=404,
                ) from e
            raise

    async def start_config_flow(
        self, handler: str, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Start a config entry flow.

        Args:
            handler: Integration domain (e.g., "template", "group")
            context: Optional context (e.g., {"source": "user"})

        Returns:
            Flow data with flow_id, step_id, data_schema

        Raises:
            HomeAssistantAPIError: If flow start fails
        """
        payload: dict[str, Any] = {"handler": handler}
        if context:
            payload["context"] = context

        logger.debug(f"Starting config flow for handler: {handler}")
        return await self._request("POST", "/config/config_entries/flow", json=payload)

    async def submit_config_flow_step(
        self, flow_id: str, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Submit data for a config flow step.

        Args:
            flow_id: Flow ID from start_config_flow or previous step
            user_input: Form data for current step

        Returns:
            Flow result: type = "create_entry" | "form" | "menu" | "abort"

        Raises:
            HomeAssistantAPIError: If flow submission fails
        """
        logger.debug(f"Submitting flow step for flow_id: {flow_id}")
        return await self._request(
            "POST", f"/config/config_entries/flow/{flow_id}", json=user_input
        )

    async def abort_config_flow(self, flow_id: str) -> dict[str, Any]:
        """
        Abort an in-progress config entry flow.

        Args:
            flow_id: Flow ID to abort

        Returns:
            Abort confirmation

        Raises:
            HomeAssistantAPIError: If flow not found or API error
        """
        logger.debug(f"Aborting config flow: {flow_id}")
        return await self._request("DELETE", f"/config/config_entries/flow/{flow_id}")

    async def start_options_flow(self, entry_id: str) -> dict[str, Any]:
        """
        Start an options flow for a config entry.

        The options flow allows configuring an existing integration
        (equivalent to clicking "Configure" in the HA UI).

        Args:
            entry_id: Config entry ID to configure

        Returns:
            Flow data with flow_id, step_id, type (form|menu),
            and data_schema or menu_options

        Raises:
            HomeAssistantAPIError: If flow start fails
        """
        logger.debug(f"Starting options flow for entry: {entry_id}")
        return await self._request(
            "POST",
            "/config/config_entries/options/flow",
            json={"handler": entry_id},
        )

    async def submit_options_flow_step(
        self, flow_id: str, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Submit data for an options flow step.

        Args:
            flow_id: Flow ID from start_options_flow or previous step
            user_input: Form data or menu selection

        Returns:
            Flow result: type = "create_entry" | "form" | "menu" | "abort"

        Raises:
            HomeAssistantAPIError: If flow submission fails
        """
        logger.debug(f"Submitting options flow step for flow_id: {flow_id}")
        return await self._request(
            "POST",
            f"/config/config_entries/options/flow/{flow_id}",
            json=user_input,
        )

    async def abort_options_flow(self, flow_id: str) -> dict[str, Any]:
        """
        Abort an in-progress options flow without saving changes.

        Args:
            flow_id: Flow ID to abort

        Returns:
            Abort confirmation

        Raises:
            HomeAssistantAPIError: If flow not found or API error
        """
        logger.debug(f"Aborting options flow: {flow_id}")
        return await self._request(
            "DELETE", f"/config/config_entries/options/flow/{flow_id}"
        )

    async def get_config_entry(self, entry_id: str) -> dict[str, Any]:
        """
        Get config entry details.

        Note: Home Assistant doesn't have a direct REST API endpoint for individual
        config entries. This method lists all entries and filters by entry_id.

        Args:
            entry_id: Config entry ID

        Returns:
            Full config entry data

        Raises:
            HomeAssistantAPIError: If entry not found or API error
        """
        logger.debug(f"Getting config entry: {entry_id}")
        # List all entries and filter by entry_id.
        # Typed as Any because _request returns dict[str, Any] generically,
        # but this endpoint actually returns a list.
        entries: Any = await self._request("GET", "/config/config_entries/entry")

        if not isinstance(entries, list):
            raise HomeAssistantAPIError(
                "Unexpected response format from config entries API",
                status_code=500,
            )

        found: dict[str, Any] | None = next(
            (dict(e) for e in entries if e.get("entry_id") == entry_id), None
        )
        if found is None:
            raise HomeAssistantAPIError(
                f"Config entry not found: {entry_id}",
                status_code=404,
            )
        return found

    async def delete_config_entry(self, entry_id: str) -> dict[str, Any]:
        """Delete a config entry via REST API.

        The WebSocket command ``config_entries/delete`` is not supported by
        Home Assistant.  The REST endpoint ``DELETE /api/config/config_entries/
        entry/{entry_id}`` is the correct way to remove a config entry.

        Args:
            entry_id: Config entry ID to delete.

        Returns:
            Result dict with ``require_restart`` flag.

        Raises:
            HomeAssistantAPIError: If the entry is not found or the API
                returns an error status.
        """
        logger.debug(f"Deleting config entry: {entry_id}")
        return await self._request("DELETE", f"/config/config_entries/entry/{entry_id}")

    async def send_websocket_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send message via WebSocket and wait for response.

        Uses a per-client WebSocket connection keyed to the client's own
        credentials (base_url + token). This ensures OAuth mode uses the
        real HA credentials from the token claims, not the global sentinel
        settings.
        """
        from .websocket_client import get_websocket_client

        max_retries = 2
        retry_delay = 0.5  # seconds

        for attempt in range(max_retries):
            try:
                # Use per-client WebSocket keyed to this client's credentials
                ws_client = await get_websocket_client(
                    url=self.base_url, token=self.token
                )

                # Special handling for render_template which returns an event with the actual result
                if message.get("type") == "render_template":
                    return await self._handle_render_template(ws_client, message)

                # Extract command type and parameters for other commands
                message_copy = message.copy()
                command_type = message_copy.pop("type")
                result = await ws_client.send_command(command_type, **message_copy)

                return result

            except Exception as e:
                error_str = str(e)

                # Detect transient 403 errors (rate limiting / reverse proxy throttling)
                if "403" in error_str and "Forbidden" in error_str:
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"WebSocket 403 error (attempt {attempt + 1}/{max_retries}), "
                            f"retrying after {retry_delay}s: {error_str}"
                        )
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        logger.error(f"WebSocket 403 error after {max_retries} attempts: {error_str}")
                        return {
                            "success": False,
                            "error": f"WebSocket request blocked (403 Forbidden): {error_str}",
                            "suggestions": [
                                "This may be caused by a reverse proxy or security filter",
                                "Try simplifying the request (e.g., shorter templates, fewer parameters)",
                                "If using complex templates, try breaking them into smaller parts",
                                "Check if your Home Assistant is behind a reverse proxy with security rules",
                            ],
                        }

                logger.error(f"WebSocket message failed: {e}")
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "WebSocket request failed"}

    async def _handle_render_template(
        self, ws_client: Any, message: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle render_template WebSocket command with event-based response."""
        template_timeout = message.get("timeout", 3)

        try:
            _, event_response = await ws_client.send_command_with_event(
                "render_template",
                wait_timeout=template_timeout + 2,
                template=message.get("template"),
                timeout=template_timeout,
                report_errors=message.get("report_errors", True),
            )
            logger.debug(f"WebSocket render_template event: {event_response}")

            # Extract template result from event
            if "event" in event_response and "result" in event_response["event"]:
                template_result = event_response["event"]["result"]
                listeners_info = event_response["event"].get("listeners", {})

                return {
                    "success": True,
                    "result": template_result,
                    "template": message.get("template"),
                    "listeners": listeners_info,
                }
            else:
                return {
                    "success": False,
                    "error": "Invalid event response format",
                    "template": message.get("template"),
                }

        except TimeoutError:
            return {
                "success": False,
                "error": "Event timeout - template result not received",
                "template": message.get("template"),
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "template": message.get("template"),
            }

    async def _resolve_script_id(self, identifier: str) -> str:
        """
        Resolve a script identifier to its storage key via the entity registry.

        Scripts may be renamed in the HA UI, changing the entity_id but keeping
        the original storage key. This method looks up the entity registry via
        WebSocket to find the actual storage key (unique_id).

        Unlike automations (which expose their storage key in state attributes),
        scripts require a WebSocket entity registry lookup.

        Args:
            identifier: Script ID (with or without 'script.' prefix)

        Returns:
            The storage key for the configuration API
        """
        bare_id = identifier.removeprefix("script.")
        entity_id = f"script.{bare_id}"
        try:
            result = await self.send_websocket_message(
                {"type": "config/entity_registry/get", "entity_id": entity_id}
            )
            if result.get("success") is not False:
                unique_id = result.get("result", {}).get("unique_id")
                if unique_id:
                    if unique_id != bare_id:
                        logger.debug(
                            f"Resolved script entity_id {entity_id} to storage key {unique_id}"
                        )
                    return str(unique_id)
        except Exception:
            logger.debug(
                f"Entity registry lookup failed for {entity_id}, using bare id: {bare_id}",
                exc_info=True # Log full traceback for better debugging
            )
        return bare_id

    async def get_script_config(self, script_id: str) -> dict[str, Any]:
        """Get Home Assistant script configuration by script_id."""
        resolved_id = await self._resolve_script_id(script_id)
        try:
            endpoint = f"config/script/config/{resolved_id}"
            response = await self._request("GET", endpoint)

            return {"success": True, "script_id": resolved_id, "config": response}
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                msg = f"Script not found: {script_id}"
                if resolved_id != script_id:
                    msg += f" (resolved storage key: {resolved_id})"
                raise HomeAssistantAPIError(msg, status_code=404) from e
            raise
        except Exception as e:
            logger.error(f"Failed to get script config for {script_id}: {e}")
            raise

    async def upsert_script_config(
        self, config: dict[str, Any], script_id: str
    ) -> dict[str, Any]:
        """Create or update Home Assistant script configuration."""
        resolved_id = await self._resolve_script_id(script_id)
        try:
            endpoint = f"config/script/config/{resolved_id}"

            # Validate required fields
            if "alias" not in config:
                config["alias"] = script_id

            # Validate that either sequence or use_blueprint is present
            if "sequence" not in config and "use_blueprint" not in config:
                raise ValueError(
                    "Script configuration must include either 'sequence' (regular scripts) "
                    "or 'use_blueprint' (blueprint-based scripts)"
                )

            response = await self._request("POST", endpoint, json=config)

            return {
                "success": True,
                "script_id": resolved_id,
                "result": response.get("result", "ok"),
                "operation": "created" if response.get("result") == "ok" else "updated",
            }
        except Exception as e:
            logger.error(f"Failed to upsert script config for {script_id}: {e}")
            raise

    async def delete_script_config(self, script_id: str) -> dict[str, Any]:
        """Delete Home Assistant script configuration."""
        resolved_id = await self._resolve_script_id(script_id)
        try:
            endpoint = f"config/script/config/{resolved_id}"
            response = await self._request("DELETE", endpoint)

            return {
                "success": True,
                "script_id": resolved_id,
                "result": response.get("result", "ok"),
                "operation": "deleted",
            }
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                msg = f"Script not found: {script_id}"
                if resolved_id != script_id:
                    msg += f" (resolved storage key: {resolved_id})"
                raise HomeAssistantAPIError(msg, status_code=404) from e
            elif e.status_code == 405:
                raise HomeAssistantAPIError(
                    f"Cannot delete script '{script_id}': The HTTP DELETE method is blocked. "
                    f"This typically occurs when running ha-mcp as a Home Assistant add-on, because "
                    f"the Supervisor ingress proxy only allows GET and POST requests. "
                    f"It may also occur if the script is defined in YAML configuration files. "
                    f"WORKAROUNDS: "
                    f"(1) Use ha-mcp via pip, Docker, or as an external MCP server instead of the add-on. "
                    f"(2) Use a long-lived access token to connect directly to Home Assistant's API. "
                    f"(3) If the script is YAML-defined, edit the configuration file directly. "
                    f"(4) As a fallback, disable the script and rename it with a 'DELETE_' prefix "
                    f"(e.g., 'DELETE_{script_id}') so you can identify and manually delete it later "
                    f"via the Home Assistant UI (Settings > Automations & Scenes > Scripts).",
                    status_code=405,
                ) from e
            raise


async def create_client() -> HomeAssistantClient:
    """Create and return a new Home Assistant client."""
    return HomeAssistantClient()


async def test_connection_with_config() -> tuple[bool, str | None]:
    """Test connection using configuration settings."""
    async with HomeAssistantClient() as client:
        return await client.test_connection()
