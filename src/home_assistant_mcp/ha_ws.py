"""Home Assistant WebSocket API client.

Provides async WebSocket client for Home Assistant WebSocket API.
Used primarily for Lovelace dashboard configuration retrieval.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    WebSocketException,
)

from home_assistant_mcp.security import MCPConfig

logger = logging.getLogger(__name__)

# Maximum response size for Lovelace config
MAX_LOVELACE_SIZE = 500_000  # 500KB - Lovelace configs can be large


class HomeAssistantWSError(Exception):
    """WebSocket error communicating with Home Assistant."""
    pass


class HomeAssistantWSAuthError(HomeAssistantWSError):
    """WebSocket authentication error."""
    pass


class HomeAssistantWebSocketClient:
    """Async WebSocket client for Home Assistant."""

    def __init__(self, config: MCPConfig):
        """Initialize the WebSocket client.

        Args:
            config: MCP configuration containing URL, token, and settings.
        """
        self.config = config
        self._ws_url = self._build_ws_url()
        self._message_id = 0

    def _build_ws_url(self) -> str:
        """Build WebSocket URL from HTTP URL."""
        parsed = urlparse(self.config.ha_url)

        # Convert http/https to ws/wss
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"

        # Build WebSocket URL
        ws_url = f"{ws_scheme}://{parsed.netloc}/api/websocket"
        return ws_url

    def _next_message_id(self) -> int:
        """Get next message ID for WebSocket commands."""
        self._message_id += 1
        return self._message_id

    async def _authenticate(
        self,
        ws: websockets.WebSocketClientProtocol,
    ) -> None:
        """Authenticate with Home Assistant WebSocket API.

        Args:
            ws: WebSocket connection

        Raises:
            HomeAssistantWSAuthError: If authentication fails
        """
        # Receive auth_required message
        auth_required = await asyncio.wait_for(
            ws.recv(),
            timeout=self.config.request_timeout
        )

        auth_required_data = json.loads(auth_required)
        if auth_required_data.get("type") != "auth_required":
            raise HomeAssistantWSAuthError(
                f"Expected auth_required, got: {auth_required_data.get('type')}"
            )

        logger.debug("WebSocket auth required, sending token")

        # Send auth token
        auth_msg = {
            "type": "auth",
            "access_token": self.config.ha_token,
        }
        await ws.send(json.dumps(auth_msg))

        # Receive auth response
        auth_response = await asyncio.wait_for(
            ws.recv(),
            timeout=self.config.request_timeout
        )

        auth_response_data = json.loads(auth_response)
        if auth_response_data.get("type") == "auth_ok":
            logger.debug("WebSocket authentication successful")
            return

        if auth_response_data.get("type") == "auth_invalid":
            raise HomeAssistantWSAuthError(
                f"Authentication failed: {auth_response_data.get('message', 'Invalid token')}"
            )

        raise HomeAssistantWSAuthError(
            f"Unexpected auth response: {auth_response_data.get('type')}"
        )

    async def _send_command(
        self,
        ws: websockets.WebSocketClientProtocol,
        command_type: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a command and wait for response.

        Args:
            ws: WebSocket connection
            command_type: The command type to send
            **kwargs: Additional command parameters

        Returns:
            The response data

        Raises:
            HomeAssistantWSError: If command fails
        """
        msg_id = self._next_message_id()

        command = {
            "id": msg_id,
            "type": command_type,
            **kwargs,
        }

        logger.debug("Sending WebSocket command: %s (id=%d)", command_type, msg_id)
        await ws.send(json.dumps(command))

        # Wait for response with matching ID
        while True:
            response = await asyncio.wait_for(
                ws.recv(),
                timeout=self.config.request_timeout
            )

            response_data = json.loads(response)

            # Check if this is our response
            if response_data.get("id") == msg_id:
                if response_data.get("success"):
                    return response_data.get("result", {})
                else:
                    error = response_data.get("error", {})
                    raise HomeAssistantWSError(
                        f"Command failed: {error.get('message', 'Unknown error')}"
                    )

            # Log unexpected messages
            logger.debug(
                "Ignoring WebSocket message with different ID: %s",
                response_data.get("id")
            )

    async def get_lovelace_config(
        self,
        force: bool = False,
        url_path: str | None = None,
    ) -> dict[str, Any]:
        """Get Lovelace dashboard configuration.

        Args:
            force: If True, force reload from storage
            url_path: Optional dashboard URL path (None for default)

        Returns:
            Dict with Lovelace configuration or error

        Note:
            This method creates a new WebSocket connection for each call.
            For the MCP use case (infrequent calls), this is acceptable.
            For high-frequency use, connection pooling would be better.
        """
        ssl_context = None
        if not self.config.verify_tls:
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        try:
            async with websockets.connect(
                self._ws_url,
                ssl=ssl_context,
                close_timeout=5,
                open_timeout=self.config.request_timeout,
            ) as ws:
                # Authenticate
                await self._authenticate(ws)

                # Build Lovelace command
                command_kwargs: dict[str, Any] = {
                    "force": force,
                }
                if url_path:
                    command_kwargs["url_path"] = url_path

                # Get Lovelace config
                try:
                    config = await self._send_command(
                        ws,
                        "lovelace/config",
                        **command_kwargs,
                    )
                except HomeAssistantWSError as e:
                    error_msg = str(e).lower()
                    if "no config found" in error_msg or "config not found" in error_msg:
                        return {
                            "config": None,
                            "message": (
                                "No UI-managed Lovelace configuration found. "
                                "This typically means: "
                                "1) You're using YAML mode (dashboards defined in configuration.yaml), "
                                "2) Using auto-generated dashboards, or "
                                "3) No custom dashboards have been created via the UI. "
                                "YAML mode dashboards are stored in files, not the database."
                            ),
                        }
                    raise

                # Truncate if needed
                config_str = json.dumps(config)
                if len(config_str) > MAX_LOVELACE_SIZE:
                    # Try to return just the views summary
                    views = config.get("views", [])
                    view_summary = []
                    for view in views:
                        view_summary.append({
                            "title": view.get("title"),
                            "path": view.get("path"),
                            "icon": view.get("icon"),
                            "cards_count": len(view.get("cards", [])),
                        })

                    return {
                        "truncated": True,
                        "message": "Full config too large, returning summary",
                        "total_bytes": len(config_str),
                        "max_bytes": MAX_LOVELACE_SIZE,
                        "title": config.get("title"),
                        "views_count": len(views),
                        "views": view_summary,
                    }

                return {
                    "truncated": False,
                    "config": config,
                }

        except asyncio.TimeoutError:
            raise HomeAssistantWSError(
                f"WebSocket timeout after {self.config.request_timeout}s"
            )
        except ConnectionClosed as e:
            raise HomeAssistantWSError(f"WebSocket connection closed: {e}")
        except WebSocketException as e:
            raise HomeAssistantWSError(f"WebSocket error: {e}")
        except OSError as e:
            raise HomeAssistantWSError(f"Connection failed: {e}")

    async def list_dashboards(self) -> dict[str, Any]:
        """List available Lovelace dashboards.

        Returns:
            Dict with list of dashboards

        Note:
            This uses the lovelace/dashboards command which may not be
            available in all HA versions. Falls back gracefully.
        """
        ssl_context = None
        if not self.config.verify_tls:
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        try:
            async with websockets.connect(
                self._ws_url,
                ssl=ssl_context,
                close_timeout=5,
                open_timeout=self.config.request_timeout,
            ) as ws:
                await self._authenticate(ws)

                dashboards = await self._send_command(ws, "lovelace/dashboards")

                return {
                    "dashboards": dashboards,
                    "count": len(dashboards) if isinstance(dashboards, list) else 0,
                }

        except HomeAssistantWSError as e:
            # Command may not be available in older HA versions
            logger.warning("Failed to list dashboards: %s", e)
            return {
                "error": str(e),
                "message": "Dashboard listing may not be available in your HA version",
            }
        except asyncio.TimeoutError:
            raise HomeAssistantWSError(
                f"WebSocket timeout after {self.config.request_timeout}s"
            )
        except (ConnectionClosed, WebSocketException, OSError) as e:
            raise HomeAssistantWSError(f"WebSocket error: {e}")
