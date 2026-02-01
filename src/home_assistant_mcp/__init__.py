"""Home Assistant MCP Server.

A Model Context Protocol server for integrating Claude with Home Assistant.
Supports read-only and read-write modes with configurable service allowlists.
"""

__version__ = "1.0.0"
__all__ = ["create_server", "run_server"]


def __getattr__(name: str):
    """Lazy import for server functions."""
    if name in ("create_server", "run_server"):
        from home_assistant_mcp.server import create_server, run_server
        return create_server if name == "create_server" else run_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
