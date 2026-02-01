"""SSH log retrieval for Home Assistant.

Provides async SSH client for fetching full Home Assistant logs
via command execution on the HA host (typically a Raspberry Pi).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from home_assistant_mcp.security import MCPConfig

logger = logging.getLogger(__name__)

# Maximum log output size
MAX_LOG_SIZE = 200_000  # 200KB

# Common log file paths on HA OS / supervised installs
HA_CORE_LOG_PATHS = [
    "/config/home-assistant.log",
    "/var/log/home-assistant.log",
    "/home/homeassistant/.homeassistant/home-assistant.log",
]


class SSHError(Exception):
    """SSH operation error."""
    pass


class SSHDisabledError(SSHError):
    """SSH is not enabled in configuration."""
    pass


class SSHLogClient:
    """Async SSH client for fetching Home Assistant logs."""

    def __init__(self, config: MCPConfig):
        """Initialize the SSH client.

        Args:
            config: MCP configuration with SSH settings.
        """
        self.config = config

    async def _connect(self) -> "asyncssh.SSHClientConnection":
        """Create SSH connection to HA host.

        Returns:
            SSH connection object

        Raises:
            SSHDisabledError: If SSH is not enabled
            SSHError: If connection fails
        """
        if not self.config.ssh_enable:
            raise SSHDisabledError(
                "SSH is not enabled. Set HA_SSH_ENABLE=true and configure "
                "HA_SSH_HOST, HA_SSH_USER, and auth credentials."
            )

        if not self.config.ssh_host:
            raise SSHError("SSH host not configured (HA_SSH_HOST)")
        if not self.config.ssh_user:
            raise SSHError("SSH user not configured (HA_SSH_USER)")

        try:
            import asyncssh
        except ImportError:
            raise SSHError(
                "asyncssh not installed. Install with: pip install asyncssh"
            )

        # Build connection options
        connect_kwargs: dict = {
            "host": self.config.ssh_host,
            "port": self.config.ssh_port,
            "username": self.config.ssh_user,
            "known_hosts": None,  # Don't verify host keys for local network
        }

        # Authentication
        if self.config.ssh_key_path:
            connect_kwargs["client_keys"] = [self.config.ssh_key_path]
        elif self.config.ssh_password:
            connect_kwargs["password"] = self.config.ssh_password
        # else: will try SSH agent or default keys

        try:
            logger.debug(
                "Connecting to %s@%s:%d",
                self.config.ssh_user,
                self.config.ssh_host,
                self.config.ssh_port,
            )
            conn = await asyncio.wait_for(
                asyncssh.connect(**connect_kwargs),
                timeout=self.config.request_timeout,
            )
            return conn
        except asyncio.TimeoutError:
            raise SSHError(
                f"SSH connection timeout after {self.config.request_timeout}s"
            )
        except asyncssh.Error as e:
            raise SSHError(f"SSH connection failed: {e}")
        except OSError as e:
            raise SSHError(f"SSH connection failed: {e}")

    async def _run_command(
        self,
        command: str,
        timeout: float | None = None,
    ) -> tuple[str, str, int]:
        """Run a command over SSH.

        Args:
            command: Command to execute
            timeout: Optional command timeout (uses config default if None)

        Returns:
            Tuple of (stdout, stderr, exit_code)

        Raises:
            SSHError: If command execution fails
        """
        if timeout is None:
            timeout = self.config.request_timeout

        try:
            import asyncssh
        except ImportError:
            raise SSHError("asyncssh not installed")

        conn = await self._connect()

        try:
            logger.debug("Running SSH command: %s", command)

            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=timeout,
            )

            return (
                result.stdout or "",
                result.stderr or "",
                result.exit_status or 0,
            )
        except asyncio.TimeoutError:
            raise SSHError(f"Command timeout after {timeout}s")
        except asyncssh.Error as e:
            raise SSHError(f"Command execution failed: {e}")
        finally:
            conn.close()
            await conn.wait_closed()

    async def get_logs(
        self,
        kind: Literal["core", "supervisor"] = "core",
        lines: int = 500,
    ) -> dict:
        """Get Home Assistant logs via SSH.

        Args:
            kind: Log type - "core" for HA core logs, "supervisor" for supervisor logs
            lines: Number of log lines to retrieve (max 2000)

        Returns:
            Dict with log content and metadata

        Raises:
            SSHDisabledError: If SSH is not enabled
            SSHError: If log retrieval fails
        """
        if not self.config.ssh_enable:
            raise SSHDisabledError(
                "SSH is not enabled. Set HA_SSH_ENABLE=true to use this feature."
            )

        # Limit lines to prevent huge output
        lines = min(lines, 2000)

        if kind == "core":
            return await self._get_core_logs(lines)
        elif kind == "supervisor":
            return await self._get_supervisor_logs(lines)
        else:
            raise SSHError(f"Unknown log kind: {kind}. Use 'core' or 'supervisor'.")

    async def _get_core_logs(self, lines: int) -> dict:
        """Get Home Assistant core logs.

        Tries `ha core logs` command first (HA OS/Supervised),
        then falls back to reading log files directly.
        """
        # Try ha CLI first (works on HA OS and Supervised)
        try:
            stdout, stderr, exit_code = await self._run_command(
                f"ha core logs --lines {lines}",
                timeout=30,  # Log commands can be slow
            )

            if exit_code == 0 and stdout:
                return self._format_log_response(stdout, "ha core logs", lines)

            logger.debug("ha core logs failed (exit %d), trying file fallback", exit_code)
        except SSHError as e:
            logger.debug("ha core logs command failed: %s", e)

        # Fallback: try common log file paths
        for log_path in HA_CORE_LOG_PATHS:
            try:
                stdout, stderr, exit_code = await self._run_command(
                    f"tail -n {lines} {log_path}",
                    timeout=30,
                )

                if exit_code == 0 and stdout:
                    return self._format_log_response(
                        stdout,
                        f"tail {log_path}",
                        lines,
                    )
            except SSHError:
                continue

        # Try journalctl as last resort
        try:
            stdout, stderr, exit_code = await self._run_command(
                f"journalctl -u home-assistant -n {lines} --no-pager",
                timeout=30,
            )

            if exit_code == 0 and stdout:
                return self._format_log_response(stdout, "journalctl", lines)
        except SSHError:
            pass

        raise SSHError(
            "Could not retrieve core logs. Tried: ha core logs, common log files, journalctl"
        )

    async def _get_supervisor_logs(self, lines: int) -> dict:
        """Get Home Assistant supervisor logs."""
        try:
            stdout, stderr, exit_code = await self._run_command(
                f"ha supervisor logs --lines {lines}",
                timeout=30,
            )

            if exit_code == 0 and stdout:
                return self._format_log_response(stdout, "ha supervisor logs", lines)

            if stderr:
                raise SSHError(f"ha supervisor logs failed: {stderr[:200]}")

            raise SSHError("ha supervisor logs returned empty output")

        except SSHError as e:
            if "not found" in str(e).lower() or "command not found" in str(e).lower():
                raise SSHError(
                    "Supervisor logs not available. This requires HA OS or Supervised installation."
                )
            raise

    def _format_log_response(
        self,
        log_content: str,
        source: str,
        requested_lines: int,
    ) -> dict:
        """Format log response with truncation handling."""
        # Count actual lines
        actual_lines = log_content.count('\n') + (1 if log_content and not log_content.endswith('\n') else 0)

        # Truncate if too large
        if len(log_content) > MAX_LOG_SIZE:
            truncated_content = log_content[:MAX_LOG_SIZE]
            # Try to end at a newline
            last_newline = truncated_content.rfind('\n')
            if last_newline > MAX_LOG_SIZE // 2:
                truncated_content = truncated_content[:last_newline]

            return {
                "truncated": True,
                "source": source,
                "requested_lines": requested_lines,
                "total_bytes": len(log_content),
                "returned_bytes": len(truncated_content),
                "max_bytes": MAX_LOG_SIZE,
                "log": truncated_content,
            }

        return {
            "truncated": False,
            "source": source,
            "requested_lines": requested_lines,
            "actual_lines": actual_lines,
            "total_bytes": len(log_content),
            "log": log_content,
        }

    async def test_connection(self) -> dict:
        """Test SSH connection.

        Returns:
            Dict with connection status and info
        """
        if not self.config.ssh_enable:
            return {
                "success": False,
                "error": "SSH is not enabled (HA_SSH_ENABLE=false)",
            }

        try:
            stdout, stderr, exit_code = await self._run_command("whoami")
            return {
                "success": True,
                "host": self.config.ssh_host,
                "user": stdout.strip(),
                "message": "SSH connection successful",
            }
        except SSHError as e:
            return {
                "success": False,
                "host": self.config.ssh_host,
                "error": str(e),
            }
