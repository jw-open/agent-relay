# ai-relay

> WebSocket relay that bridges AI coding agent CLIs (Claude Code, Codex, Gemini CLI, Snowflake Cortex, and more) to any web interface — stream reasoning, tool calls, and file changes in real time.

## Install

```bash
pip install ai-relay
```

## Quick start

### One-shot mode (local dev)

```bash
# Start the relay server (default: ws://0.0.0.0:8765)
ai-relay --port 8765
```

### Server mode (container / daemon)

```bash
# Persistent server — each WebSocket connection becomes one independent agent session
ai-relay serve --port 9000
```

Then connect from OhWise Lab (or any WebSocket client) and send a handshake:

```json
{"tool": "claude", "folder": "/path/to/project", "model": "claude-sonnet-4-6"}
```

The relay streams structured JSON events over WebSocket and forwards your messages to the selected backend. Claude Code and Gemini use native JSONL process protocols, Codex uses the app-server JSON-RPC protocol, and Snowflake Cortex uses HTTP/SSE. The PTY bridge is retained only for generic/legacy CLI tools.

## Running in a container

`ai-relay serve` is designed to run inside a Docker container as a persistent daemon:

```dockerfile
FROM python:3.11-slim
RUN pip install ai-relay
# Install your AI CLI here (e.g. npm install -g @anthropic-ai/claude-code)
CMD ["ai-relay", "serve", "--port", "9000"]
```

Each incoming WebSocket connection spawns an independent agent session. Multiple clients can connect simultaneously.

## Event types

| Type | Description |
|------|-------------|
| `session_start` | Process spawned |
| `session_end` | Process exited (includes exit_code) |
| `stdout` / `stderr` | Raw output lines |
| `reasoning` | Agent thinking/planning text |
| `tool_call` | Agent invoking a tool (Read, Edit, Bash…) |
| `tool_result` | Result of a tool call |
| `file_diff` | File created or edited |
| `response` | Final answer text |
| `assistant_message` | Native structured assistant message |
| `user_message` | Native structured user/tool-result message |
| `stream_event` | Native streaming event |
| `status` | Native status/control event |
| `permission_request` | Tool permission prompt from a structured backend |
| `permission_cancelled` | Pending permission prompt was cancelled |
| `control_response` | Native control response acknowledgment |
| `tool_progress` | Native tool progress event |
| `quota_warning` | API quota / rate limit detected |
| `context_warning` | Context window nearing limit (includes context_pct) |
| `context_compacted` | Context was compacted |
| `error` | Relay or process error |
| `input_ack` | Relay confirms your message was sent to the process |

## Sending commands

Send JSON over WebSocket:

```json
{"text": "refactor the authentication module to use JWT"}
```

Claude Code also accepts structured web-client messages:

```json
{"type": "user_message", "content": "refactor the authentication module to use JWT"}
```

Permission responses:

```json
{"type": "permission_response", "request_id": "req", "behavior": "allow", "updatedInput": {"command": "git status"}}
```

Codex permission responses can also use:

```json
{"type": "permission_response", "request_id": "req", "allow": true}
```

Interrupt the active structured turn:

```json
{"type": "interrupt"}
```

Codex uses `codex app-server --listen stdio://` and keeps a persistent thread behind the WebSocket session:

```json
{"tool": "codex", "folder": "/path/to/project", "model": "gpt-5.2"}
```

Gemini CLI uses ACP (Agent Communication Protocol) mode. Each text message starts one Gemini turn:

```json
{"tool": "gemini", "folder": "/path/to/project", "model": "gemini-2.5-flash"}
```

> **Gemini-specific behavior — per-turn process lifecycle**
>
> Unlike Claude Code (persistent process + `--resume`) and Codex (persistent
> app-server), Gemini CLI ACP mode (≥ 0.40.x) exits the subprocess after every
> `session/prompt` turn completes (exit code 0). This is by design in the Gemini
> CLI implementation.
>
> The relay handles this transparently:
> - On the first turn the relay calls `session/new` and saves the returned
>   `sessionId` to `.gemini_acp_session` in the session working folder.
> - On each subsequent reconnect the relay calls `session/load` with that ID so
>   Gemini restores conversation context in the new process.
> - If `session/load` fails (session expired or folder was wiped) the relay
>   falls back to `session/new` automatically.
>
> **Implication for permission approvals:** because the process exits after each
> turn, permission responses must be sent while the current turn's process is
> still alive (i.e. before the `session/prompt` RPC completes). The default
> `session/prompt` timeout is 5 minutes, which covers typical interactive
> permission dialogs.

Snowflake Cortex uses API configuration in the handshake.

Cortex chat mode:

```json
{
  "tool": "cortex",
  "mode": "chat",
  "model": "claude-sonnet-4-5",
  "snowflake": {
    "account_url": "https://<account>.snowflakecomputing.com",
    "token_env": "SNOWFLAKE_PAT"
  }
}
```

Cortex Analyst mode:

```json
{
  "tool": "cortex",
  "mode": "analyst",
  "snowflake": {
    "account_url": "https://<account>.snowflakecomputing.com",
    "token_env": "SNOWFLAKE_PAT",
    "semantic_view": "DB.SCHEMA.VIEW"
  }
}
```

To send CLI commands (e.g. `/compact`, `/clear`):

```json
{"text": "/compact"}
```

## Supported tools

| Tool | Adapter | `tool` value |
|------|---------|-------------|
| Claude Code | `ClaudeCodeAdapter` | `"claude"` / `"claude-code"` |
| OpenAI Codex | `CodexAdapter` | `"codex"` |
| Gemini CLI | `GeminiAdapter` | `"gemini"` |
| Snowflake Cortex | `CortexAdapter` | `"cortex"` |
| Any CLI | `GenericAdapter` | `"generic"` |

## Configuration reference (ohwise-lab-ctrl)

When using ohwise-lab alongside ai-relay, all settings are controlled via environment variables — nothing is hardcoded:

| Variable | Default | Description |
|----------|---------|-------------|
| `LAB_MODE` | `single` | `single` = subprocesses in lab-ctrl; `multi` = per-user Docker containers |
| `LAB_WORKSPACE_ROOT` | `/var/ohwise-lab-workspaces` | Host path for user workspace volumes |
| `LAB_IMAGE` | `ohwise-lab-ctrl:local` | Docker image for user containers (must have ai-relay + CLIs installed) |
| `LAB_NETWORK` | `lab-network` | Docker network user containers join. For Compose: `<project>_default` |
| `LAB_CONTAINER_PORT` | `9000` | Internal port `ai-relay serve` listens on inside user containers |
| `LAB_CONTAINER_USER` | `labuser` | OS user inside user containers |
| `LAB_CONTAINER_HOME` | `/home/<LAB_CONTAINER_USER>` | Home dir inside user containers |
| `LAB_CONTAINER_STARTUP_DELAY` | `1.5` | Seconds to wait for ai-relay to be ready after container start |
| `LAB_CONTAINER_WS_TIMEOUT` | `15` | Seconds to wait for WebSocket connection to user container |
| `LAB_IDLE_TIMEOUT_SECS` | `1800` | Seconds of inactivity before a user container is eligible for cleanup |

## Python API

```python
from ai_relay import RelayServer

server = RelayServer(host="0.0.0.0", port=8765)
server.run()
```

## Documentation

- [Architecture & Developer Guide](docs/architecture.md) — internals, event system, auth, turn lifecycle, reconnect handling, adding providers, containerised deployment

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

Apache-2.0
