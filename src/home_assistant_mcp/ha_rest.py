"""Home Assistant REST API client.

Provides async HTTP client for Home Assistant REST API endpoints.
Includes retry logic, proper error handling, and response truncation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from home_assistant_mcp.security import MCPConfig

logger = logging.getLogger(__name__)

# Maximum response size before truncation (100KB)
MAX_RESPONSE_SIZE = 100_000

# Maximum number of entities to return in bulk queries
MAX_ENTITIES = 500

# Maximum history/logbook entries
MAX_HISTORY_ENTRIES = 200


class HomeAssistantAPIError(Exception):
    """Base exception for Home Assistant API errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HomeAssistantAuthError(HomeAssistantAPIError):
    """Authentication error with Home Assistant."""
    pass


class HomeAssistantNotFoundError(HomeAssistantAPIError):
    """Resource not found in Home Assistant."""
    pass


def _truncate_response(data: Any, max_bytes: int = MAX_RESPONSE_SIZE) -> dict[str, Any]:
    """Truncate response data if it exceeds max size.

    Args:
        data: The data to potentially truncate
        max_bytes: Maximum size in bytes before truncation

    Returns:
        Either the original data or a truncated wrapper with metadata.
    """
    import json

    serialized = json.dumps(data)
    if len(serialized) <= max_bytes:
        return {"truncated": False, "data": data}

    # For lists, truncate by removing items
    if isinstance(data, list):
        # Binary search for acceptable size
        items = len(data)
        while items > 0:
            items = items // 2
            truncated_data = data[:items]
            if len(json.dumps(truncated_data)) <= max_bytes:
                return {
                    "truncated": True,
                    "total_items": len(data),
                    "returned_items": items,
                    "max_bytes": max_bytes,
                    "data": truncated_data,
                }

    # For other types, just indicate truncation
    return {
        "truncated": True,
        "max_bytes": max_bytes,
        "message": f"Response exceeds {max_bytes} bytes and could not be truncated safely",
        "data": None,
    }


class HomeAssistantRestClient:
    """Async client for Home Assistant REST API."""

    def __init__(self, config: MCPConfig):
        """Initialize the REST client.

        Args:
            config: MCP configuration containing URL, token, and settings.
        """
        self.config = config
        self.base_url = config.ha_url
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.config.ha_token}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(self.config.request_timeout),
                verify=self.config.verify_tls,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _log_request(self, method: str, path: str) -> None:
        """Log an API request without exposing sensitive data."""
        logger.debug("HA REST API: %s %s", method, path)

    def _handle_response(self, response: httpx.Response, path: str) -> Any:
        """Handle API response and raise appropriate errors.

        Args:
            response: The HTTP response
            path: The request path (for error messages)

        Returns:
            The parsed JSON response

        Raises:
            HomeAssistantAuthError: On 401/403 responses
            HomeAssistantNotFoundError: On 404 responses
            HomeAssistantAPIError: On other error responses
        """
        if response.status_code == 401:
            raise HomeAssistantAuthError(
                "Authentication failed - check HA_TOKEN",
                status_code=401
            )
        if response.status_code == 403:
            raise HomeAssistantAuthError(
                "Access forbidden - token may lack required permissions",
                status_code=403
            )
        if response.status_code == 404:
            raise HomeAssistantNotFoundError(
                f"Resource not found: {path}",
                status_code=404
            )
        if response.status_code >= 400:
            raise HomeAssistantAPIError(
                f"API error {response.status_code}: {response.text[:200]}",
                status_code=response.status_code
            )

        # Handle text responses (like error_log)
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
    ) -> Any:
        """Make an API request with retry logic.

        Args:
            method: HTTP method
            path: API path (relative to base URL)
            json_data: Optional JSON body

        Returns:
            Parsed response data
        """
        self._log_request(method, path)
        client = await self._get_client()

        try:
            response = await client.request(method, path, json=json_data)
            return self._handle_response(response, path)
        except httpx.TimeoutException:
            logger.warning("Request timeout for %s %s", method, path)
            raise HomeAssistantAPIError(
                f"Request timeout after {self.config.request_timeout}s"
            )
        except httpx.TransportError as e:
            logger.warning("Transport error for %s %s: %s", method, path, e)
            raise

    # --- API Methods ---

    async def ping(self) -> dict[str, Any]:
        """Check connectivity and get HA info.

        Returns:
            Dict with HA version and message.
        """
        result = await self._request("GET", "/api/")
        return {
            "status": "ok",
            "message": result.get("message", "API running"),
            "version": result.get("version"),
        }

    async def list_entities(
        self,
        domain: str | None = None,
    ) -> dict[str, Any]:
        """List all entities, optionally filtered by domain.

        Args:
            domain: Optional domain prefix to filter (e.g., "light", "sensor")

        Returns:
            Dict with entity list (potentially truncated).
        """
        states = await self._request("GET", "/api/states")

        if not isinstance(states, list):
            raise HomeAssistantAPIError("Unexpected response format from /api/states")

        # Filter by domain if specified
        if domain:
            domain_prefix = f"{domain}."
            states = [s for s in states if s.get("entity_id", "").startswith(domain_prefix)]

        # Build summary list
        entities = []
        for state in states[:MAX_ENTITIES]:
            entity_id = state.get("entity_id", "unknown")
            attrs = state.get("attributes", {})
            entities.append({
                "entity_id": entity_id,
                "state": state.get("state"),
                "friendly_name": attrs.get("friendly_name"),
                "device_class": attrs.get("device_class"),
            })

        result: dict[str, Any] = {
            "total": len(states),
            "returned": len(entities),
            "domain_filter": domain,
            "entities": entities,
        }

        if len(states) > MAX_ENTITIES:
            result["truncated"] = True
            result["message"] = f"Limited to {MAX_ENTITIES} entities. Use domain filter for more specific results."

        return result

    async def get_entity(self, entity_id: str) -> dict[str, Any]:
        """Get state for a specific entity.

        Args:
            entity_id: The entity ID (e.g., "light.living_room")

        Returns:
            Complete entity state dict.
        """
        path = f"/api/states/{entity_id}"
        return await self._request("GET", path)

    async def search_entities(self, query: str) -> dict[str, Any]:
        """Search entities by ID, name, or attributes.

        Args:
            query: Search string (case-insensitive)

        Returns:
            Dict with matching entities (potentially truncated).
        """
        states = await self._request("GET", "/api/states")

        if not isinstance(states, list):
            raise HomeAssistantAPIError("Unexpected response format from /api/states")

        query_lower = query.lower()
        matches = []

        for state in states:
            entity_id = state.get("entity_id", "")
            attrs = state.get("attributes", {})
            friendly_name = attrs.get("friendly_name", "")

            # Check entity_id
            if query_lower in entity_id.lower():
                matches.append(state)
                continue

            # Check friendly_name
            if query_lower in friendly_name.lower():
                matches.append(state)
                continue

            # Check attributes (convert to string for search)
            attr_str = str(attrs).lower()
            if query_lower in attr_str:
                matches.append(state)
                continue

        # Build summary
        entities = []
        for state in matches[:MAX_ENTITIES]:
            entity_id = state.get("entity_id", "unknown")
            attrs = state.get("attributes", {})
            entities.append({
                "entity_id": entity_id,
                "state": state.get("state"),
                "friendly_name": attrs.get("friendly_name"),
                "device_class": attrs.get("device_class"),
                "last_changed": state.get("last_changed"),
            })

        return _truncate_response({
            "query": query,
            "total_matches": len(matches),
            "returned": len(entities),
            "entities": entities,
        })

    async def get_history(
        self,
        entity_id: str | None = None,
        hours: int = 24,
    ) -> dict[str, Any]:
        """Get state history for entity or all entities.

        Args:
            entity_id: Optional entity ID to filter
            hours: Number of hours of history (default 24)

        Returns:
            Dict with history data (potentially truncated).
        """
        # Calculate time range
        end_time = datetime.now(timezone.utc).replace(microsecond=0)
        start_time = end_time - timedelta(hours=hours)

        # Format timestamps for HA API (ISO 8601 without microseconds)
        start_str = start_time.isoformat()
        end_str = end_time.isoformat()

        # URL encode the timestamps (+ needs to be %2B)
        path = f"/api/history/period/{quote(start_str, safe='')}"
        params = f"?end_time={quote(end_str, safe='')}"

        if entity_id:
            params += f"&filter_entity_id={entity_id}"
        else:
            # Limit to significant entities when fetching all
            params += "&significant_changes_only=1"

        history = await self._request("GET", path + params)

        if not isinstance(history, list):
            raise HomeAssistantAPIError("Unexpected response format from history API")

        # Process and limit history
        result_history = []
        total_entries = 0

        for entity_history in history:
            if not isinstance(entity_history, list) or not entity_history:
                continue

            total_entries += len(entity_history)

            # Limit entries per entity
            limited_entries = entity_history[:MAX_HISTORY_ENTRIES]
            result_history.append(limited_entries)

        return _truncate_response({
            "entity_id": entity_id,
            "hours": hours,
            "start_time": start_str,
            "end_time": end_str,
            "total_entries": total_entries,
            "history": result_history,
        })

    async def get_logbook(
        self,
        entity_id: str | None = None,
        hours: int = 24,
    ) -> dict[str, Any]:
        """Get logbook events for entity or all entities.

        Args:
            entity_id: Optional entity ID to filter
            hours: Number of hours of events (default 24)

        Returns:
            Dict with logbook data (potentially truncated).
        """
        # Calculate time range
        end_time = datetime.now(timezone.utc).replace(microsecond=0)
        start_time = end_time - timedelta(hours=hours)

        # Format timestamps for HA API (ISO 8601 without microseconds)
        start_str = start_time.isoformat()
        end_str = end_time.isoformat()

        # URL encode the timestamps (+ needs to be %2B)
        path = f"/api/logbook/{quote(start_str, safe='')}"
        params = f"?end_time={quote(end_str, safe='')}"

        if entity_id:
            params += f"&entity={entity_id}"

        logbook = await self._request("GET", path + params)

        if not isinstance(logbook, list):
            raise HomeAssistantAPIError("Unexpected response format from logbook API")

        # Limit entries
        limited = logbook[:MAX_HISTORY_ENTRIES]

        return _truncate_response({
            "entity_id": entity_id,
            "hours": hours,
            "start_time": start_str,
            "end_time": end_str,
            "total_entries": len(logbook),
            "returned_entries": len(limited),
            "entries": limited,
        })

    async def get_error_log(self) -> dict[str, Any]:
        """Get Home Assistant error log.

        Returns:
            Dict with error log text (potentially truncated).
        """
        try:
            log_text = await self._request("GET", "/api/error_log")
        except HomeAssistantNotFoundError:
            # Try alternative endpoint or return helpful message
            return {
                "error": False,
                "message": (
                    "Error log endpoint not available. This may be due to: "
                    "1) Home Assistant version differences, "
                    "2) Logging not configured, or "
                    "3) Insufficient API token permissions. "
                    "Try checking logs via SSH with ha_get_full_logs if SSH is enabled."
                ),
                "log": None,
            }

        if not isinstance(log_text, str):
            log_text = str(log_text)

        # Truncate if needed
        if len(log_text) > MAX_RESPONSE_SIZE:
            return {
                "truncated": True,
                "total_bytes": len(log_text),
                "max_bytes": MAX_RESPONSE_SIZE,
                "log": log_text[:MAX_RESPONSE_SIZE],
            }

        return {
            "truncated": False,
            "total_bytes": len(log_text),
            "log": log_text,
        }

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a Home Assistant service.

        Args:
            domain: Service domain (e.g., "light")
            service: Service name (e.g., "turn_on")
            data: Optional service data
            target: Optional target (entity_id, device_id, or area_id)

        Returns:
            Dict with service call result.
        """
        path = f"/api/services/{domain}/{service}"

        # Build request body
        body: dict[str, Any] = {}
        if data:
            body.update(data)
        if target:
            # Merge target into body (HA REST API expects flat structure)
            body.update(target)

        logger.info(
            "Calling service %s.%s with %d data keys",
            domain, service, len(body)
        )

        result = await self._request("POST", path, json_data=body if body else None)

        return {
            "success": True,
            "domain": domain,
            "service": service,
            "result": result if result else "Service called successfully",
        }
