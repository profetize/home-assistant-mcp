# Home Assistant MCP Server

A Model Context Protocol (MCP) server that enables Claude to interact with your Home Assistant instance. Run it locally on your laptop to control smart home devices, query sensor data, view dashboards, and more.

## Features

- **Read-only by default**: Safe exploration of your Home Assistant data
- **Read-write mode**: Control devices with configurable service allowlists
- **Comprehensive API coverage**:
  - Entity states and attributes
  - History and logbook
  - Error logs
  - Lovelace dashboard configuration
  - Service calls (in read-write mode)
- **SSH log access**: Optional full log retrieval via SSH
- **Security-focused**: Token-based auth, service allowlists, no hardcoded secrets
- **Robust**: Retry logic, timeouts, structured logging

## Quick Start

### 1. Install

```bash
# Clone and install
cd ~/src/home_assistant_mcp
pip install -e .

# Or install dependencies directly
pip install mcp httpx websockets asyncssh pydantic tenacity
```

### 2. Create a Home Assistant Token

1. Open Home Assistant web UI
2. Go to your **Profile** (click your name in the sidebar)
3. Scroll to **Long-lived access tokens**
4. Click **Create Token**
5. Give it a name (e.g., "Claude MCP")
6. Copy the token (you won't see it again!)

### 3. Configure Environment

```bash
# Required
export HA_URL="http://homeassistant.local:8123"  # or IP: http://192.168.1.100:8123
export HA_TOKEN="your_long_lived_access_token_here"

# Optional: Enable read-write mode for device control
export HA_MCP_MODE="readwrite"
export HA_ALLOWED_SERVICES="light.*,switch.*,climate.set_temperature"
```

### 4. Run the Server

```bash
# Run directly
python -m home_assistant_mcp

# Or use the console script
home-assistant-mcp

# With debug logging
python -m home_assistant_mcp --debug
```

### 5. Configure Claude Code

Add to your Claude Code MCP configuration (`~/.claude/claude_desktop_config.json` or similar):

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "python",
      "args": ["-m", "home_assistant_mcp"],
      "env": {
        "HA_URL": "http://homeassistant.local:8123",
        "HA_TOKEN": "your_token_here",
        "HA_MCP_MODE": "readonly"
      }
    }
  }
}
```

For read-write mode with controlled services:

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "python",
      "args": ["-m", "home_assistant_mcp"],
      "env": {
        "HA_URL": "http://homeassistant.local:8123",
        "HA_TOKEN": "your_token_here",
        "HA_MCP_MODE": "readwrite",
        "HA_ALLOWED_SERVICES": "light.*,switch.*,climate.set_temperature,scene.turn_on"
      }
    }
  }
}
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HA_URL` | Yes | - | Home Assistant base URL |
| `HA_TOKEN` | Yes | - | Long-lived access token |
| `HA_MCP_MODE` | No | `readonly` | `readonly` or `readwrite` |
| `HA_ALLOWED_SERVICES` | No | (empty) | Comma-separated service patterns |
| `HA_VERIFY_TLS` | No | `true` | Set `false` for self-signed certs |
| `HA_REQUEST_TIMEOUT_SECONDS` | No | `15` | Request timeout in seconds |
| `HA_SSH_ENABLE` | No | `false` | Enable SSH log access |
| `HA_SSH_HOST` | No | (from URL) | SSH hostname |
| `HA_SSH_USER` | No | - | SSH username |
| `HA_SSH_PORT` | No | `22` | SSH port |
| `HA_SSH_KEY_PATH` | No | - | Path to SSH private key |
| `HA_SSH_PASSWORD` | No | - | SSH password (if not using key) |

## Available Tools

### Read-Only Tools (Always Available)

#### `ha_ping`
Check connectivity and get Home Assistant version.

```json
// Response
{
  "status": "ok",
  "message": "API running.",
  "version": "2024.1.0"
}
```

#### `ha_list_entities`
List all entities, optionally filtered by domain.

```json
// Arguments
{ "domain": "light" }  // optional

// Response
{
  "total": 42,
  "returned": 42,
  "domain_filter": "light",
  "entities": [
    {
      "entity_id": "light.living_room",
      "state": "on",
      "friendly_name": "Living Room Light",
      "device_class": null
    }
  ]
}
```

#### `ha_get_entity`
Get detailed state for a specific entity.

```json
// Arguments
{ "entity_id": "light.living_room" }

// Response
{
  "entity_id": "light.living_room",
  "state": "on",
  "attributes": {
    "friendly_name": "Living Room Light",
    "brightness": 255,
    "color_mode": "brightness"
  },
  "last_changed": "2024-01-15T10:30:00+00:00",
  "last_updated": "2024-01-15T10:30:00+00:00"
}
```

#### `ha_search_entities`
Search entities by ID, name, or attributes.

```json
// Arguments
{ "query": "temperature" }

// Response
{
  "query": "temperature",
  "total_matches": 5,
  "entities": [...]
}
```

#### `ha_get_history`
Get state history for an entity.

```json
// Arguments
{ "entity_id": "sensor.temperature", "hours": 24 }

// Response
{
  "entity_id": "sensor.temperature",
  "hours": 24,
  "history": [[...state changes...]]
}
```

#### `ha_get_logbook`
Get logbook events.

```json
// Arguments
{ "entity_id": "light.living_room", "hours": 12 }

// Response
{
  "entries": [
    {
      "when": "2024-01-15T10:30:00+00:00",
      "name": "Living Room Light",
      "message": "turned on"
    }
  ]
}
```

#### `ha_get_error_log`
Get the Home Assistant error log.

```json
// Response
{
  "truncated": false,
  "total_bytes": 12345,
  "log": "2024-01-15 10:30:00 ERROR (MainThread) ..."
}
```

#### `ha_get_lovelace_config`
Get Lovelace dashboard configuration.

```json
// Arguments
{ "force": false }

// Response
{
  "truncated": false,
  "config": {
    "title": "Home",
    "views": [...]
  }
}
```

### Read-Write Tools (Requires `HA_MCP_MODE=readwrite`)

#### `ha_call_service`
Call a Home Assistant service.

```json
// Arguments
{
  "domain": "light",
  "service": "turn_on",
  "data": { "brightness": 200 },
  "target": { "entity_id": "light.living_room" }
}

// Response
{
  "success": true,
  "domain": "light",
  "service": "turn_on",
  "result": [...]
}
```

### SSH Tools (Requires `HA_SSH_ENABLE=true`)

#### `ha_get_full_logs`
Get full logs via SSH.

```json
// Arguments
{ "kind": "core", "lines": 500 }

// Response
{
  "source": "ha core logs",
  "truncated": false,
  "log": "..."
}
```

## Service Allowlist Patterns

The `HA_ALLOWED_SERVICES` variable accepts comma-separated patterns:

| Pattern | Matches |
|---------|---------|
| `light.turn_on` | Exact service |
| `light.*` | All light services |
| `*` | All services (dangerous!) |

Examples:
```bash
# Allow all light and switch services
export HA_ALLOWED_SERVICES="light.*,switch.*"

# Allow specific services only
export HA_ALLOWED_SERVICES="light.turn_on,light.turn_off,climate.set_temperature"

# Allow everything (use with caution!)
export HA_ALLOWED_SERVICES="*"
```

## Security Best Practices

1. **Start with read-only mode** until you're comfortable
2. **Use specific allowlist patterns** instead of wildcards
3. **Never commit tokens** to version control
4. **Use environment variables** or a secrets manager
5. **Restrict token scope** in Home Assistant if possible
6. **Disable TLS verification only for local networks** with self-signed certs

## Troubleshooting

### "Authentication failed"
- Verify your token is correct and not expired
- Check that the token has the required permissions
- Ensure `HA_URL` doesn't have a trailing slash

### "Connection refused"
- Verify Home Assistant is running and accessible
- Check the URL is correct (try opening it in a browser)
- Ensure you're on the same network

### "Service call denied"
- Check `HA_MCP_MODE` is set to `readwrite`
- Verify the service is in `HA_ALLOWED_SERVICES`
- Check pattern matching (use `domain.*` for all services in a domain)

### "TLS certificate verify failed"
- For self-signed certificates, set `HA_VERIFY_TLS=false`
- This is safe for local network access

### SSH logs not working
- Ensure `HA_SSH_ENABLE=true`
- Verify SSH credentials are correct
- Check that `ha` CLI is available on the HA host (HA OS/Supervised only)

## Architecture

```
┌─────────────────┐     ┌──────────────────────────────────────┐
│  Claude Code    │────▶│  home-assistant-mcp                  │
│  (MCP Client)   │◀────│  (MCP Server)                        │
└─────────────────┘     │                                      │
                        │  ┌──────────────┐  ┌──────────────┐  │
                        │  │ REST Client  │  │  WS Client   │  │
                        │  └──────┬───────┘  └──────┬───────┘  │
                        │         │                 │          │
                        │  ┌──────▼─────────────────▼───────┐  │
                        │  │        Home Assistant          │  │
                        │  │     (Raspberry Pi / LAN)       │  │
                        │  └────────────────────────────────┘  │
                        │                                      │
                        │  ┌──────────────┐ (optional)         │
                        │  │  SSH Client  │─────▶ HA Host      │
                        │  └──────────────┘                    │
                        └──────────────────────────────────────┘
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Type checking
mypy src/

# Linting
ruff check src/
```

## License

MIT License - see LICENSE file for details.
