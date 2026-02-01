"""Home Assistant MCP Server implementation.

Provides MCP tools for interacting with Home Assistant:
- Read-only tools for entities, history, logbook, error logs, and Lovelace config
- Read-write tools for service calls (when enabled)
- SSH tools for full log access (when enabled)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
)

from home_assistant_mcp.ha_rest import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantNotFoundError,
    HomeAssistantRestClient,
)
from home_assistant_mcp.ha_ws import (
    HomeAssistantWebSocketClient,
    HomeAssistantWSError,
)
from home_assistant_mcp.security import MCPConfig
from home_assistant_mcp.ssh_logs import SSHDisabledError, SSHError, SSHLogClient

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,  # MCP uses stdout for protocol, logs go to stderr
)
logger = logging.getLogger("home_assistant_mcp")


def _json_response(data: Any) -> list[TextContent]:
    """Convert data to MCP TextContent response."""
    import json
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _error_response(message: str) -> list[TextContent]:
    """Create error response."""
    import json
    return [TextContent(type="text", text=json.dumps({"error": message}, indent=2))]


def create_server(config: MCPConfig | None = None) -> Server:
    """Create and configure the MCP server.

    Args:
        config: Optional configuration. If None, loads from environment.

    Returns:
        Configured MCP Server instance.
    """
    if config is None:
        config = MCPConfig.from_env()

    server = Server("home-assistant-mcp")

    # Initialize clients
    rest_client = HomeAssistantRestClient(config)
    ws_client = HomeAssistantWebSocketClient(config)
    ssh_client = SSHLogClient(config)

    # --- Tool Definitions ---

    def get_tools() -> list[Tool]:
        """Get list of available tools based on configuration."""
        tools = [
            # Read-only tools (always available)
            Tool(
                name="ha_ping",
                description="Check Home Assistant connectivity and get version info",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            ),
            Tool(
                name="ha_list_entities",
                description="List all entities in Home Assistant, optionally filtered by domain (e.g., 'light', 'sensor', 'switch')",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Optional domain to filter by (e.g., 'light', 'sensor', 'climate')",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="ha_get_entity",
                description="Get the current state and attributes of a specific entity",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity_id": {
                            "type": "string",
                            "description": "The entity ID (e.g., 'light.living_room', 'sensor.temperature')",
                        },
                    },
                    "required": ["entity_id"],
                },
            ),
            Tool(
                name="ha_search_entities",
                description="Search for entities by name, ID, or attributes",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query to match against entity IDs, friendly names, and attributes",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="ha_get_history",
                description="Get state history for an entity or all entities over a time period",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity_id": {
                            "type": "string",
                            "description": "Optional entity ID to get history for. If omitted, returns history for all entities (limited).",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Number of hours of history to retrieve (default: 24, max: 168)",
                            "default": 24,
                            "minimum": 1,
                            "maximum": 168,
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="ha_get_logbook",
                description="Get logbook events showing what happened in Home Assistant",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity_id": {
                            "type": "string",
                            "description": "Optional entity ID to filter events for",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Number of hours of events to retrieve (default: 24, max: 168)",
                            "default": 24,
                            "minimum": 1,
                            "maximum": 168,
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="ha_get_error_log",
                description="Get the Home Assistant error log",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            ),
            Tool(
                name="ha_get_lovelace_config",
                description="Get the Lovelace dashboard configuration via WebSocket API",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "force": {
                            "type": "boolean",
                            "description": "Force reload configuration from storage (default: false)",
                            "default": False,
                        },
                    },
                    "required": [],
                },
            ),
        ]

        # Read-write tools (only in readwrite mode)
        if config.is_readwrite:
            tools.append(
                Tool(
                    name="ha_call_service",
                    description=(
                        "Call a Home Assistant service (e.g., turn on lights, set temperature). "
                        "Only allowed services can be called based on HA_ALLOWED_SERVICES configuration."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "domain": {
                                "type": "string",
                                "description": "Service domain (e.g., 'light', 'climate', 'switch')",
                            },
                            "service": {
                                "type": "string",
                                "description": "Service name (e.g., 'turn_on', 'turn_off', 'set_temperature')",
                            },
                            "data": {
                                "type": "object",
                                "description": "Optional service data (e.g., {'brightness': 255})",
                                "default": {},
                            },
                            "target": {
                                "type": "object",
                                "description": "Optional target specifying entity_id, device_id, or area_id",
                                "properties": {
                                    "entity_id": {
                                        "oneOf": [
                                            {"type": "string"},
                                            {"type": "array", "items": {"type": "string"}},
                                        ],
                                    },
                                    "device_id": {
                                        "oneOf": [
                                            {"type": "string"},
                                            {"type": "array", "items": {"type": "string"}},
                                        ],
                                    },
                                    "area_id": {
                                        "oneOf": [
                                            {"type": "string"},
                                            {"type": "array", "items": {"type": "string"}},
                                        ],
                                    },
                                },
                            },
                        },
                        "required": ["domain", "service"],
                    },
                )
            )

        # SSH tools (only when SSH enabled)
        if config.ssh_enable:
            tools.append(
                Tool(
                    name="ha_get_full_logs",
                    description=(
                        "Get full Home Assistant logs via SSH. "
                        "Requires SSH access to the HA host."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "description": "Log type: 'core' for HA core logs, 'supervisor' for supervisor logs",
                                "enum": ["core", "supervisor"],
                                "default": "core",
                            },
                            "lines": {
                                "type": "integer",
                                "description": "Number of log lines to retrieve (default: 500, max: 2000)",
                                "default": 500,
                                "minimum": 10,
                                "maximum": 2000,
                            },
                        },
                        "required": [],
                    },
                )
            )

        return tools

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available MCP tools."""
        return get_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle tool calls."""
        logger.info("Tool call: %s", name)

        try:
            # --- Read-only tools ---
            if name == "ha_ping":
                result = await rest_client.ping()
                return _json_response(result)

            elif name == "ha_list_entities":
                domain = arguments.get("domain")
                result = await rest_client.list_entities(domain=domain)
                return _json_response(result)

            elif name == "ha_get_entity":
                entity_id = arguments.get("entity_id")
                if not entity_id:
                    return _error_response("entity_id is required")
                result = await rest_client.get_entity(entity_id)
                return _json_response(result)

            elif name == "ha_search_entities":
                query = arguments.get("query")
                if not query:
                    return _error_response("query is required")
                result = await rest_client.search_entities(query)
                return _json_response(result)

            elif name == "ha_get_history":
                entity_id = arguments.get("entity_id")
                hours = min(arguments.get("hours", 24), 168)
                result = await rest_client.get_history(
                    entity_id=entity_id,
                    hours=hours,
                )
                return _json_response(result)

            elif name == "ha_get_logbook":
                entity_id = arguments.get("entity_id")
                hours = min(arguments.get("hours", 24), 168)
                result = await rest_client.get_logbook(
                    entity_id=entity_id,
                    hours=hours,
                )
                return _json_response(result)

            elif name == "ha_get_error_log":
                result = await rest_client.get_error_log()
                return _json_response(result)

            elif name == "ha_get_lovelace_config":
                force = arguments.get("force", False)
                result = await ws_client.get_lovelace_config(force=force)
                return _json_response(result)

            # --- Read-write tools ---
            elif name == "ha_call_service":
                if not config.is_readwrite:
                    return _error_response(
                        "Service calls are disabled in read-only mode. "
                        "Set HA_MCP_MODE=readwrite to enable."
                    )

                domain = arguments.get("domain")
                service = arguments.get("service")

                if not domain or not service:
                    return _error_response("domain and service are required")

                # Check allowlist
                if not config.allowed_services.is_allowed(domain, service):
                    return _error_response(
                        config.allowed_services.get_denial_message(domain, service)
                    )

                data = arguments.get("data", {})
                target = arguments.get("target")

                result = await rest_client.call_service(
                    domain=domain,
                    service=service,
                    data=data,
                    target=target,
                )
                return _json_response(result)

            # --- SSH tools ---
            elif name == "ha_get_full_logs":
                if not config.ssh_enable:
                    return _error_response(
                        "SSH is not enabled. Set HA_SSH_ENABLE=true and configure "
                        "SSH credentials to use this feature."
                    )

                kind = arguments.get("kind", "core")
                lines = min(arguments.get("lines", 500), 2000)

                result = await ssh_client.get_logs(kind=kind, lines=lines)
                return _json_response(result)

            else:
                return _error_response(f"Unknown tool: {name}")

        except HomeAssistantAuthError as e:
            logger.error("Authentication error: %s", e)
            return _error_response(f"Authentication failed: {e}")

        except HomeAssistantNotFoundError as e:
            logger.warning("Resource not found: %s", e)
            return _error_response(str(e))

        except HomeAssistantAPIError as e:
            logger.error("API error: %s", e)
            return _error_response(f"Home Assistant API error: {e}")

        except HomeAssistantWSError as e:
            logger.error("WebSocket error: %s", e)
            return _error_response(f"WebSocket error: {e}")

        except SSHDisabledError as e:
            return _error_response(str(e))

        except SSHError as e:
            logger.error("SSH error: %s", e)
            return _error_response(f"SSH error: {e}")

        except Exception as e:
            logger.exception("Unexpected error in tool %s", name)
            return _error_response(f"Unexpected error: {type(e).__name__}: {e}")

    # Cleanup on shutdown
    async def cleanup() -> None:
        """Clean up resources."""
        await rest_client.close()

    # Store cleanup function for external use
    server._cleanup = cleanup  # type: ignore

    return server


async def run_server() -> None:
    """Run the MCP server with stdio transport."""
    logger.info("Starting Home Assistant MCP server...")

    try:
        config = MCPConfig.from_env()
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    server = create_server(config)

    logger.info("Server ready, waiting for connections...")
    logger.info("Mode: %s", config.mode)

    if config.is_readwrite:
        if config.allowed_services.allow_all:
            logger.warning("ALL service calls are allowed (wildcard)")
        elif config.allowed_services.patterns:
            logger.info(
                "Allowed services: %s",
                ", ".join(config.allowed_services.patterns)
            )
        else:
            logger.warning("No services are allowlisted - service calls will be denied")

    async with stdio_server() as (read_stream, write_stream):
        try:
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
        finally:
            if hasattr(server, "_cleanup"):
                await server._cleanup()  # type: ignore
