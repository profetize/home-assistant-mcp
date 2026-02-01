#!/usr/bin/env python3
"""Entry point for Home Assistant MCP server.

Run with: python -m home_assistant_mcp
Or: home-assistant-mcp (if installed via pip)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Home Assistant MCP Server for Claude integration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  HA_URL                     Home Assistant URL (required)
  HA_TOKEN                   Long-lived access token (required)
  HA_MCP_MODE                'readonly' (default) or 'readwrite'
  HA_ALLOWED_SERVICES        Comma-separated service patterns (e.g., 'light.*,switch.turn_on')
  HA_VERIFY_TLS              'true' (default) or 'false'
  HA_SSH_ENABLE              'true' or 'false' (default)
  HA_SSH_HOST                SSH hostname (defaults to HA_URL host)
  HA_SSH_USER                SSH username
  HA_SSH_PORT                SSH port (default: 22)
  HA_SSH_KEY_PATH            Path to SSH private key
  HA_SSH_PASSWORD            SSH password
  HA_REQUEST_TIMEOUT_SECONDS Request timeout (default: 15)

Examples:
  # Read-only mode (default)
  HA_URL=http://homeassistant.local:8123 HA_TOKEN=xxx python -m home_assistant_mcp

  # Read-write mode with limited services
  HA_MCP_MODE=readwrite HA_ALLOWED_SERVICES='light.*,switch.*' \\
    HA_URL=http://homeassistant.local:8123 HA_TOKEN=xxx python -m home_assistant_mcp

  # With SSH log access
  HA_SSH_ENABLE=true HA_SSH_USER=root HA_SSH_KEY_PATH=~/.ssh/id_rsa \\
    HA_URL=http://homeassistant.local:8123 HA_TOKEN=xxx python -m home_assistant_mcp
""",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )

    parser.add_argument(
        "--mode",
        choices=["readonly", "readwrite"],
        help="Override HA_MCP_MODE environment variable",
    )

    parser.add_argument(
        "--url",
        help="Override HA_URL environment variable",
    )

    parser.add_argument(
        "--allowed-services",
        help="Override HA_ALLOWED_SERVICES environment variable",
    )

    parser.add_argument(
        "--no-verify-tls",
        action="store_true",
        help="Disable TLS verification (for self-signed certificates)",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Apply CLI overrides to environment
    if args.mode:
        os.environ["HA_MCP_MODE"] = args.mode

    if args.url:
        os.environ["HA_URL"] = args.url

    if args.allowed_services:
        os.environ["HA_ALLOWED_SERVICES"] = args.allowed_services

    if args.no_verify_tls:
        os.environ["HA_VERIFY_TLS"] = "false"

    # Configure logging level
    import logging

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("home_assistant_mcp").setLevel(logging.DEBUG)

    # Check required environment variables early
    if not os.environ.get("HA_URL"):
        print(
            "Error: HA_URL environment variable is required.\n"
            "Set it to your Home Assistant URL, e.g.:\n"
            "  export HA_URL=http://homeassistant.local:8123\n",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.environ.get("HA_TOKEN"):
        print(
            "Error: HA_TOKEN environment variable is required.\n"
            "Create a long-lived access token in Home Assistant:\n"
            "  Profile -> Security -> Long-lived access tokens -> Create token\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Run the server
    from home_assistant_mcp.server import run_server

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
