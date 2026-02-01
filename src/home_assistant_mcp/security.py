"""Security utilities for Home Assistant MCP server.

Handles service allowlist parsing and enforcement for read-write mode.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class ServiceAllowlist:
    """Manages allowed service patterns for Home Assistant service calls.

    Supports patterns:
    - Exact: "light.turn_on"
    - Domain wildcard: "light.*"
    - Full wildcard: "*" (allows all services)
    """

    patterns: list[str] = field(default_factory=list)
    allow_all: bool = False

    @classmethod
    def from_env(cls, env_value: str | None) -> ServiceAllowlist:
        """Parse allowlist from HA_ALLOWED_SERVICES env var.

        Args:
            env_value: Comma-separated list of patterns, or None/empty for no services.

        Returns:
            Configured ServiceAllowlist instance.
        """
        if not env_value or not env_value.strip():
            return cls(patterns=[], allow_all=False)

        patterns = [p.strip() for p in env_value.split(",") if p.strip()]

        if "*" in patterns:
            logger.warning(
                "Service allowlist contains wildcard '*' - ALL services are allowed. "
                "This is potentially dangerous in read-write mode."
            )
            return cls(patterns=patterns, allow_all=True)

        # Validate pattern format
        validated_patterns: list[str] = []
        for pattern in patterns:
            if cls._validate_pattern(pattern):
                validated_patterns.append(pattern)
            else:
                logger.warning("Invalid service pattern ignored: %s", pattern)

        logger.info(
            "Service allowlist configured with %d pattern(s): %s",
            len(validated_patterns),
            ", ".join(validated_patterns) if validated_patterns else "(none)"
        )

        return cls(patterns=validated_patterns, allow_all=False)

    @staticmethod
    def _validate_pattern(pattern: str) -> bool:
        """Validate a service pattern format.

        Valid formats:
        - "domain.service" (exact)
        - "domain.*" (domain wildcard)
        - "*" (full wildcard, handled separately)
        """
        if pattern == "*":
            return True

        # Must contain exactly one dot
        if pattern.count(".") != 1:
            return False

        domain, service = pattern.split(".")

        # Domain must be alphanumeric/underscore
        if not re.match(r"^[a-z_][a-z0-9_]*$", domain):
            return False

        # Service must be alphanumeric/underscore or wildcard
        if service != "*" and not re.match(r"^[a-z_][a-z0-9_]*$", service):
            return False

        return True

    def is_allowed(self, domain: str, service: str) -> bool:
        """Check if a domain.service call is allowed.

        Args:
            domain: Service domain (e.g., "light")
            service: Service name (e.g., "turn_on")

        Returns:
            True if the service call is allowed, False otherwise.
        """
        if self.allow_all:
            return True

        if not self.patterns:
            return False

        full_service = f"{domain}.{service}"

        for pattern in self.patterns:
            # Exact match
            if pattern == full_service:
                logger.debug("Service %s matched exact pattern %s", full_service, pattern)
                return True

            # Wildcard match using fnmatch
            if fnmatch.fnmatch(full_service, pattern):
                logger.debug("Service %s matched wildcard pattern %s", full_service, pattern)
                return True

        logger.info(
            "Service call %s denied - not in allowlist: %s",
            full_service,
            ", ".join(self.patterns)
        )
        return False

    def get_denial_message(self, domain: str, service: str) -> str:
        """Generate a user-friendly denial message."""
        full_service = f"{domain}.{service}"

        if not self.patterns:
            return (
                f"Service call '{full_service}' denied: no services are allowlisted. "
                f"Set HA_ALLOWED_SERVICES to enable service calls."
            )

        return (
            f"Service call '{full_service}' denied: not in allowlist. "
            f"Allowed patterns: {', '.join(self.patterns)}"
        )


@dataclass
class MCPConfig:
    """Configuration for the Home Assistant MCP server."""

    # Required
    ha_url: str
    ha_token: str

    # Mode
    mode: Literal["readonly", "readwrite"] = "readonly"

    # Service allowlist (only relevant in readwrite mode)
    allowed_services: ServiceAllowlist = field(default_factory=ServiceAllowlist)

    # TLS verification
    verify_tls: bool = True

    # SSH configuration
    ssh_enable: bool = False
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_port: int = 22
    ssh_key_path: str | None = None
    ssh_password: str | None = None

    # Timeouts
    request_timeout: float = 15.0

    @classmethod
    def from_env(cls) -> MCPConfig:
        """Create configuration from environment variables.

        Required:
            HA_URL: Base URL of Home Assistant instance
            HA_TOKEN: Long-lived access token

        Optional:
            HA_MCP_MODE: "readonly" (default) or "readwrite"
            HA_ALLOWED_SERVICES: Comma-separated service patterns
            HA_VERIFY_TLS: "true" (default) or "false"
            HA_SSH_ENABLE: "true" or "false" (default)
            HA_SSH_HOST: SSH hostname (defaults to HA_URL host)
            HA_SSH_USER: SSH username
            HA_SSH_PORT: SSH port (default 22)
            HA_SSH_KEY_PATH: Path to SSH private key
            HA_SSH_PASSWORD: SSH password (if not using key)
            HA_REQUEST_TIMEOUT_SECONDS: Request timeout (default 15)

        Raises:
            ValueError: If required variables are missing or invalid.
        """
        import os
        from urllib.parse import urlparse

        # Required variables
        ha_url = os.environ.get("HA_URL", "").strip()
        ha_token = os.environ.get("HA_TOKEN", "").strip()

        if not ha_url:
            raise ValueError("HA_URL environment variable is required")
        if not ha_token:
            raise ValueError("HA_TOKEN environment variable is required")

        # Normalize URL (remove trailing slash)
        ha_url = ha_url.rstrip("/")

        # Validate URL format
        parsed = urlparse(ha_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"HA_URL must use http or https scheme, got: {parsed.scheme}")
        if not parsed.netloc:
            raise ValueError(f"HA_URL is missing hostname: {ha_url}")

        # Mode
        mode_str = os.environ.get("HA_MCP_MODE", "readonly").strip().lower()
        if mode_str not in ("readonly", "readwrite"):
            raise ValueError(f"HA_MCP_MODE must be 'readonly' or 'readwrite', got: {mode_str}")
        mode: Literal["readonly", "readwrite"] = "readwrite" if mode_str == "readwrite" else "readonly"

        # Service allowlist
        allowed_services = ServiceAllowlist.from_env(
            os.environ.get("HA_ALLOWED_SERVICES")
        )

        # TLS verification
        verify_tls_str = os.environ.get("HA_VERIFY_TLS", "true").strip().lower()
        verify_tls = verify_tls_str != "false"

        if not verify_tls:
            logger.warning("TLS verification disabled - this is insecure for production use")

        # SSH configuration
        ssh_enable_str = os.environ.get("HA_SSH_ENABLE", "false").strip().lower()
        ssh_enable = ssh_enable_str == "true"

        ssh_host = os.environ.get("HA_SSH_HOST", "").strip() or None
        ssh_user = os.environ.get("HA_SSH_USER", "").strip() or None

        ssh_port_str = os.environ.get("HA_SSH_PORT", "22").strip()
        try:
            ssh_port = int(ssh_port_str)
        except ValueError:
            raise ValueError(f"HA_SSH_PORT must be an integer, got: {ssh_port_str}")

        ssh_key_path = os.environ.get("HA_SSH_KEY_PATH", "").strip() or None
        ssh_password = os.environ.get("HA_SSH_PASSWORD", "").strip() or None

        if ssh_enable:
            if not ssh_host:
                # Default to HA_URL host
                ssh_host = parsed.hostname
            if not ssh_user:
                raise ValueError("HA_SSH_USER is required when HA_SSH_ENABLE=true")
            if not ssh_key_path and not ssh_password:
                logger.warning(
                    "Neither HA_SSH_KEY_PATH nor HA_SSH_PASSWORD set - "
                    "will attempt SSH agent or default key"
                )

        # Request timeout
        timeout_str = os.environ.get("HA_REQUEST_TIMEOUT_SECONDS", "15").strip()
        try:
            request_timeout = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"HA_REQUEST_TIMEOUT_SECONDS must be a number, got: {timeout_str}"
            )

        config = cls(
            ha_url=ha_url,
            ha_token=ha_token,
            mode=mode,
            allowed_services=allowed_services,
            verify_tls=verify_tls,
            ssh_enable=ssh_enable,
            ssh_host=ssh_host,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            ssh_key_path=ssh_key_path,
            ssh_password=ssh_password,
            request_timeout=request_timeout,
        )

        # Log configuration (without sensitive data)
        logger.info("Home Assistant MCP configuration loaded:")
        logger.info("  URL: %s", ha_url)
        logger.info("  Mode: %s", mode)
        logger.info("  TLS verification: %s", verify_tls)
        logger.info("  SSH enabled: %s", ssh_enable)
        logger.info("  Request timeout: %.1fs", request_timeout)

        if mode == "readwrite":
            if allowed_services.allow_all:
                logger.warning("  Service allowlist: ALL (wildcard)")
            elif allowed_services.patterns:
                logger.info("  Service allowlist: %s", ", ".join(allowed_services.patterns))
            else:
                logger.warning(
                    "  Service allowlist: EMPTY - no service calls will be allowed"
                )

        return config

    @property
    def is_readwrite(self) -> bool:
        """Check if server is in read-write mode."""
        return self.mode == "readwrite"
