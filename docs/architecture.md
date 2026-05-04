# ai-relay — Architecture & Developer Guide

This document explains how ai-relay works internally: the data flow, runtime models, event system, authentication, and per-provider behaviour. Read this before contributing or integrating.

---

## Table of contents

1. [Overview](#overview)
2. [Component map](#component-map)
3. [Data flow](#data-flow)
4. [Runtime models: per-turn vs persistent](#runtime-models-per-turn-vs-persistent)
5. [Event system](#event-system)
6. [Client → relay message protocol](#client--relay-message-protocol)
7. [Handshake](#handshake)
8. [Authentication — per provider](#authentication--per-provider)
9. [Status events and turn lifecycle](#status-events-and-turn-lifecycle)
10. [Permission approval flow](#permission-approval-flow)
11. [Timeouts](#timeouts)
12. [Adding a new provider](#adding-a-new-provider)
13. [Reconnect and session resume](#reconnect-and-session-resume)
14. [Containerised multi-user deployment](#containerised-multi-user-deployment)

---

## Overview

ai-relay is a thin WebSocket server that sits between a web frontend and an AI coding-agent CLI process. It:

- **Spawns** the CLI as a subprocess (or long-lived daemon, depending on provider)
- **Parses** the CLI's stdout/stderr into structured JSON events
- **Streams** those events over WebSocket to the frontend
- **Forwards** user messages from the frontend to the CLI's stdin or via JSON-RPC

```
Browser / frontend
        │ WebSocket (JSON events)
        ▼
   ai-relay serve
        │ subprocess stdin/stdout (JSON-RPC or JSONL or PTY)
        ▼
  AI CLI (Claude Code / Codex / Gemini / Cortex)
        │ HTTP/gRPC/SSE
        ▼
   LLM API (Anthropic / OpenAI / Google / Snowflake)
```

---

## Component map

```
ai_relay/
├── cli.py              Entry points: `ai-relay` (one-shot) and `ai-relay serve`
├── relay.py            RelaySession + RelayServer — top-level orchestration
├── per_turn.py         PerTurnRuntime — wraps any runtime, restarts subprocess per user turn
├── events.py           EventType enum + RelayEvent dataclass
├── transports.py       StructuredProcessTransport (async stdin/stdout/stderr pipes)
├── pty_session.py      PTY transport for generic/legacy CLI tools
├── adapters/
│   ├── base.py         AgentRuntime + BaseAdapter abstract interfaces
│   ├── claude_code.py  Claude Code (stream-json JSONL protocol)
│   ├── codex.py        OpenAI Codex (app-server JSON-RPC 2.0)
│   ├── gemini.py       Gemini CLI (ACP JSON-RPC 2.0)
│   ├── cortex.py       Snowflake Cortex (HTTP/SSE)
│   └── generic.py      Generic PTY fallback
├── claude_auth.py      Claude OAuth credential detection + env injection
├── codex_auth.py       Codex ChatGPT credential detection + env injection
└── gemini_auth.py      Gemini OAuth credential detection, token refresh, settings
```

---

## Data flow

### Server mode (`ai-relay serve`)

```
1. Client connects via WebSocket
2. Client sends handshake JSON:
      {"tool": "claude", "folder": "/repo", "model": "claude-sonnet-4-6"}
3. relay.py creates a RelaySession and calls session.start(ws)
4. session.start():
   a. Selects the adapter for the tool
   b. Builds the subprocess command
   c. Resolves auth credentials (auth modules)
   d. Wraps in PerTurnRuntime (Claude) or uses runtime directly (Codex/Gemini)
   e. Emits SESSION_START event to client
5. Two concurrent tasks run:
   a. _relay_to_client: reads RelayEvents from runtime queue → sends to WS
   b. _client_to_relay: reads WS messages → calls runtime.handle_client_message()
6. When WS closes or process exits → SESSION_END emitted, cleanup
```

### One-shot mode (`ai-relay` without `serve`)

Same flow but the WebSocket server starts, accepts one connection, runs one session, then exits.

---

## Runtime models: per-turn vs persistent

Different CLI tools have fundamentally different lifecycles, which requires two runtime models:

### PerTurnRuntime (Claude Code)

Claude Code's `--print` / stream-json mode reads stdin until EOF, processes one turn, then exits. It cannot stay alive between turns.

**Solution:** `PerTurnRuntime` (in `per_turn.py`) wraps a `ClaudeStructuredRuntime` and restarts the subprocess for every user message:

```
Turn 1: spawn claude-code --print
        → send user message
        → read all events until EOF
        → capture session_id from system/init event
        → subprocess exits

Turn 2: spawn claude-code --print --resume <session_id>
        → send user message
        → read all events until EOF
        → subprocess exits
```

The `--resume` flag tells Claude Code to continue the previous conversation using its on-disk session store. The relay captures the `session_id` from the `system/init` raw event on the first turn.

**Key behaviour:**
- Control messages (`interrupt`, `permission_response`, `set_model`) are forwarded to the **current** subprocess immediately without restarting
- The `_turn_lock` prevents concurrent turns from racing
- If the user sends a message before a turn finishes, the lock queues it

### Persistent runtime (Codex, Gemini)

These CLIs start as long-lived daemon processes that support interactive request/response.

**Codex** (`CodexAppServerRuntime`): uses `codex app-server --listen stdio://` — a JSON-RPC 2.0 server over stdin/stdout. One subprocess per WebSocket session. Manages `thread/start` → `turn/start` → notifications → `turn/completed` lifecycle.

**Gemini** (`GeminiStructuredRuntime`): uses `gemini --acp --output-format stream-json` — ACP (Agent Control Protocol) JSON-RPC 2.0 over stdin/stdout. One subprocess per WebSocket session. Manages `initialize` → `session/new` → `session/prompt` lifecycle.

---

## Event system

All events are `RelayEvent` dataclass instances serialised to JSON and sent over WebSocket. Every event has at minimum:

```json
{
  "type": "<event_type>",
  "ts": 1746359123.456,
  "session_id": "abc123"
}
```

### Full event type reference

| Type | When emitted | Key fields |
|------|-------------|------------|
| `session_start` | WebSocket connected + process started | `metadata.tool`, `metadata.model` |
| `session_end` | Process exited or WS closed | `exit_code` |
| `input_ack` | User message forwarded to process | `text` (echoed prompt) |
| `reasoning` | Agent thinking/planning text (streaming) | `text` |
| `assistant_message` | Structured assistant response block (Claude) | `content` (block array), `text` |
| `tool_call` | Agent invoked a tool | `tool`, `args`, `tool_use_id`, `text` |
| `tool_result` | Tool execution result | `tool_use_id`, `content`, `text`, `exit_code` |
| `tool_progress` | Streaming tool output (command execution delta) | `tool`, `tool_use_id`, `text` |
| `file_diff` | File created or modified | `diff`, `content` |
| `response` | Turn-complete signal (Gemini, Codex) | `text` (may be empty) |
| `permission_request` | CLI asking user to approve a tool action | `request_id`, `tool`, `args`, `text` |
| `permission_cancelled` | A pending permission was cancelled by the CLI | `request_id` |
| `control_response` | CLI acknowledging a control message | |
| `status` | Internal state change notification | `status` (string), `content` |
| `stream_event` | Raw unrecognised native event | `raw` |
| `quota_warning` | API quota or rate limit detected | `text` |
| `context_warning` | Context window nearing limit | `text`, `context_pct` (0–100) |
| `context_compacted` | Context was compacted | `content` |
| `error` | Relay or process error | `text` (human-readable message) |
| `stdout` / `stderr` | Raw process output (generic/PTY mode) | `text` |

### Which events signal turn completion?

The frontend uses `response`, `error`, and `session_end` as **turn-end signals** — they indicate the agent has finished processing and the user can send the next message. All other events are mid-turn streaming.

`input_ack` is the **turn-start signal** — emitted immediately after forwarding the user's message, before any agent output arrives.

---

## Client → relay message protocol

After the handshake, the client sends JSON objects over WebSocket:

### Send a user prompt

```json
{"text": "refactor the auth module"}
```

Or with structured content (images):

```json
{
  "type": "user_message",
  "content": [
    {"type": "text", "text": "what's in this image?"},
    {"type": "image", "source": {"media_type": "image/png", "data": "<base64>"}}
  ]
}
```

### Respond to a permission request

When you receive a `permission_request` event, respond with the `request_id` from that event:

```json
{
  "type": "permission_response",
  "request_id": "<from permission_request.request_id>",
  "behavior": "allow"
}
```

`behavior` values: `"allow"` (approve once), `"deny"` (reject).

**Gemini** additionally requires an `optionId` from the `args.options` array:

```json
{
  "type": "permission_response",
  "request_id": "<id>",
  "behavior": "allow",
  "optionId": "proceed_once"
}
```

Common Gemini option IDs: `proceed_always` (allow for session), `proceed_once` (allow once), `cancel` (reject).

**Codex** accepts either the structured form above or a simpler form:

```json
{"type": "permission_response", "request_id": "<id>", "allow": true}
```

### Interrupt the active turn

```json
{"type": "interrupt"}
```

### Switch model mid-session

```json
{"type": "set_model", "model": "claude-opus-4-6"}
```

### CLI slash commands

```json
{"text": "/compact"}
{"text": "/clear"}
```

---

## Handshake

The first message after a WebSocket connection must be the handshake JSON. The relay does not accept user prompts until it receives the handshake.

### Minimum required

```json
{"tool": "claude", "folder": "/path/to/project"}
```

### Full handshake fields

| Field | Type | Description |
|-------|------|-------------|
| `tool` | string | CLI to use: `"claude"`, `"codex"`, `"gemini"`, `"cortex"`, `"generic"` |
| `folder` | string | Working directory passed to the CLI |
| `session_id` | string | Optional — use an existing session ID for history replay |
| `model` | string | Optional — override the default model |
| `extra_args` | array | Optional — additional CLI arguments |
| `config` | object | Optional — provider-specific config (see below) |
| `env` | object | Optional — extra environment variables injected into the subprocess |

### Provider config examples

**Codex:**
```json
{
  "tool": "codex",
  "folder": "/repo",
  "config": {
    "approvalPolicy": "auto",
    "ephemeral": true,
    "baseInstructions": "You are a Python expert."
  }
}
```

**Gemini:**
```json
{
  "tool": "gemini",
  "folder": "/repo",
  "model": "gemini-2.5-pro",
  "config": {
    "gemini_oauth_client_id": "...",
    "gemini_oauth_client_secret": "..."
  }
}
```

**Cortex chat:**
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

---

## Authentication — per provider

Each provider has a dedicated auth module that runs **before the subprocess starts**. Auth modules:
1. Detect which credentials are available (OAuth file, API key env var, etc.)
2. Prefer the appropriate auth type (configurable)
3. Optionally refresh expiring tokens
4. Inject env vars or write config files so the CLI starts authenticated

### Claude Code (`claude_auth.py`)

**Auth sources (in priority order when `AI_RELAY_CLAUDE_PREFER_OAUTH=1`):**
1. OAuth credentials in `$HOME/.claude/.credentials.json` (Claude Code desktop login)
2. `ANTHROPIC_API_KEY` environment variable

**How it works:**
- Reads `$HOME/.claude/.credentials.json`
- If `claudeAiOauth.accessToken` is present AND `AI_RELAY_CLAUDE_PREFER_OAUTH=1`, removes `ANTHROPIC_API_KEY` from subprocess env so Claude Code uses its own OAuth flow
- If no OAuth, keeps `ANTHROPIC_API_KEY` in env

**Credential file location:** `$HOME/.claude/.credentials.json`

**Key env vars:**
| Variable | Effect |
|----------|--------|
| `AI_RELAY_CLAUDE_PREFER_OAUTH` | `1` = use OAuth creds over API key when both are present |
| `ANTHROPIC_API_KEY` | Direct API key auth (bypassed when OAuth preferred) |

---

### OpenAI Codex (`codex_auth.py`)

**Auth sources (in priority order when `AI_RELAY_CODEX_PREFER_CHATGPT=1`):**
1. ChatGPT OAuth tokens in `$HOME/.codex/auth.json`
2. `OPENAI_API_KEY` environment variable

**How it works:**
- Reads `$HOME/.codex/auth.json`
- If `tokens.access_token` and `tokens.refresh_token` are present AND `AI_RELAY_CODEX_PREFER_CHATGPT=1`, removes `OPENAI_API_KEY` from subprocess env

**Key env vars:**
| Variable | Effect |
|----------|--------|
| `AI_RELAY_CODEX_PREFER_CHATGPT` | `1` = prefer ChatGPT OAuth over API key |
| `OPENAI_API_KEY` | Direct API key auth |

---

### Gemini CLI (`gemini_auth.py`)

Gemini auth is the most complex because:
- Gemini CLI uses a settings file (`settings.json`) to determine which auth method to use
- OAuth access tokens expire (typically in 1 hour) and must be refreshed before each session
- The credential file location may differ between the relay process and the subprocess (containerised deployments)

**Auth sources (in priority order when `AI_RELAY_GEMINI_PREFER_OAUTH=1`):**
1. OAuth credentials in `$GEMINI_CLI_HOME/oauth_creds.json` or `$HOME/.gemini/oauth_creds.json`
2. `GEMINI_API_KEY` / `GOOGLE_API_KEY` environment variable
3. Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=1`)

**`ensure_gemini_auth()` flow (runs before each session start):**

```
1. Locate creds file:
   - Use $GEMINI_CLI_HOME/oauth_creds.json if GEMINI_CLI_HOME is set
   - Otherwise $HOME/.gemini/oauth_creds.json

2. Check if OAuth creds are valid (has access_token + refresh_token)

3. If AI_RELAY_GEMINI_PREFER_OAUTH=1 and OAuth creds exist:
   - Remove GEMINI_API_KEY, GOOGLE_API_KEY from subprocess env
   - Write settings.json: security.auth.selectedType = "oauth-personal"

4. If only API key in env:
   - Write settings.json: security.auth.selectedType = "gemini-api-key"

5. If token is expiring within 5 minutes:
   - Use GEMINI_OAUTH_CLIENT_ID + GEMINI_OAUTH_CLIENT_SECRET to call Google's
     token refresh endpoint (oauth2.googleapis.com/token)
   - Write updated access_token + expiry_date back to oauth_creds.json
   - If client credentials are not set, skip refresh and let Gemini CLI handle it
```

**Why GEMINI_CLI_HOME exists:**
In containerised deployments (e.g. OhWise Lab multi-user mode), the relay process and the CLI subprocess run in different containers. The relay process needs to read/write creds from its own filesystem path, while the CLI subprocess reads creds from its own `$HOME`. `GEMINI_CLI_HOME` tells `ensure_gemini_auth` where to find creds on the relay's filesystem, independently of `HOME`.

**Key env vars:**
| Variable | Effect |
|----------|--------|
| `AI_RELAY_GEMINI_PREFER_OAUTH` | `1` = prefer OAuth over API key |
| `GEMINI_CLI_HOME` | Directory containing `oauth_creds.json` and `settings.json` (relay-side path) |
| `GEMINI_OAUTH_CLIENT_ID` | OAuth client ID for proactive token refresh |
| `GEMINI_OAUTH_CLIENT_SECRET` | OAuth client secret for proactive token refresh |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Direct API key auth |
| `GOOGLE_GENAI_USE_VERTEXAI` | `1` = Vertex AI auth |

**Credential files:**
- `$HOME/.gemini/oauth_creds.json` — contains `access_token`, `refresh_token`, `expiry_date`
- `$HOME/.gemini/settings.json` — contains `security.auth.selectedType`

---

### Snowflake Cortex (`cortex.py`)

Cortex uses PAT (Personal Access Token) auth via the `SNOWFLAKE_PAT` env var (or the `token_env` field in the handshake config). No OAuth — just a static token passed in the `Authorization` header.

---

## Status events and turn lifecycle

### Codex status events

Codex emits several `status` events that are internal protocol bookkeeping. These are **not shown to users**:

| `status` value | Meaning |
|----------------|---------|
| `thread/status/changed` | Codex thread state changed (internal) |
| `thread/tokenUsage/updated` | Token count updated (internal) |
| `thread_started` | `thread/start` RPC completed |
| `started` | Turn started (`turn/started` notification) |
| `completed` | Turn completed (`turn/completed` notification) |

The actual turn-end signal for frontends is the `response` event (from `item/agentMessage/delta` notifications) or `error`.

### Gemini turn lifecycle

```
Client sends:    {"text": "hello"}
                      │
Relay calls:     session/prompt (async, fire-and-forget via asyncio.create_task)
                      │
Gemini streams:  session/update notifications:
                   → agent_thought_chunk  → REASONING events
                   → agent_message_chunk  → RESPONSE events (streaming text)
                   → tool_call            → TOOL_CALL events
                   → tool_result          → TOOL_RESULT events
                   → session/request_permission → PERMISSION_REQUEST event
                      │
session/prompt returns (after all updates):
                   → success → RESPONSE event (empty text, signals turn end)
                   → error   → ERROR event (e.g. quota exceeded)
```

**Important:** The `session/prompt` RPC is called with `asyncio.create_task()` so it runs concurrently with the notification stream. The RPC result arrives **after** all `session/update` notifications for that turn. The RESPONSE or ERROR event emitted by the RPC result is what signals turn completion to the frontend.

---

## Permission approval flow

### General flow

```
1. CLI encounters a tool that requires user approval
2. CLI emits a permission request (via JSON-RPC or ACP notification)
3. Relay emits PERMISSION_REQUEST event:
   {
     "type": "permission_request",
     "request_id": "<id>",
     "tool": "<tool name>",
     "text": "<human-readable description>",
     "args": { <tool arguments or options> }
   }
4. Frontend displays Allow/Deny UI
5. User clicks Allow/Deny
6. Frontend sends permission_response message
7. Relay forwards to CLI subprocess
8. CLI proceeds or cancels
```

### Provider-specific details

**Claude Code:**
- Permission requests arrive as JSON-RPC server requests from Claude Code's structured protocol
- `request_id` is a string (may be numeric string from JSON-RPC `id`)
- Response forwarded as JSON-RPC response with `result` or `error`

**Codex:**
- Permission requests from `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`, `item/tool/call`
- `request_id` is the JSON-RPC request `id`
- Response sent back as JSON-RPC result via `_send_response()`

**Gemini:**
- Permission requests arrive as `session/request_permission` ACP notification
- `request_id` is the JSON-RPC `id` from the notification
- `args.options` contains the list of available choices (with `optionId`)
- Response is a JSON-RPC response sent directly: `{"id": <request_id>, "result": {"outcome": {"outcome": "selected", "optionId": "<choice>"}}}`
- **Timeout:** If no response arrives within 5 minutes, `session/prompt` times out and emits an ERROR event

**State persistence note:** Permission response state (allowed/denied) must be stored in the frontend **outside** the permission card component's local state, because the card may be unmounted and remounted during list re-renders.

---

## Timeouts

| Provider | Timeout | Context |
|----------|---------|---------|
| Claude Code | No hard relay timeout | PerTurnRuntime waits for subprocess EOF |
| Codex | No hard relay timeout | JSON-RPC `_send_request` has no timeout |
| Gemini `initialize` | 60 seconds | ACP startup |
| Gemini `session/new` | 60 seconds | Session creation (network call) |
| Gemini `session/prompt` | **300 seconds** | Full turn including user permission interactions |
| Gemini `session/cancel` | 60 seconds | Interrupt |

The 300-second timeout for `session/prompt` exists because Gemini permission dialogs require user interaction — the RPC doesn't return until after the user responds to any permission requests.

---

## Adding a new provider

1. **Create `ai_relay/adapters/myprovider.py`** implementing:
   - `MyRuntime(AgentRuntime)` — subprocess lifecycle + event parsing
   - `MyAdapter(BaseAdapter)` — `tool_name`, `protocol`, `build_command()`, `create_runtime()`

2. **Register in `ai_relay/adapters/__init__.py`:**
   ```python
   from .myprovider import MyAdapter
   _ADAPTERS["mytool"] = MyAdapter
   ```

3. **If the CLI exits after each turn:** wrap your runtime in `PerTurnRuntime` in `relay.py`'s `_PER_TURN_TOOLS` dict.

4. **If auth is needed:** create `ai_relay/myprovider_auth.py` with an `ensure_myprovider_auth(env)` function, call it in `MyRuntime.start()`.

5. **Map your tool's events** to `RelayEvent` types in `_notification_events()` or equivalent. Use `STREAM_EVENT` as a fallback for unrecognised messages.

### AgentRuntime interface

```python
class AgentRuntime:
    async def start(self) -> None:
        """Initialise subprocess/connection. Called once before any messages."""

    async def read_event(self) -> Optional[RelayEvent]:
        """Return the next event, or None on clean shutdown."""

    async def handle_client_message(self, msg: dict) -> None:
        """Handle an inbound WebSocket message from the client."""

    async def stop(self) -> None:
        """Terminate subprocess and signal the event queue with None."""

    async def wait(self) -> Optional[int]:
        """Wait for subprocess to exit; return exit code."""
```

---

## Reconnect and session resume

### What happens when the WebSocket disconnects?

When a client disconnects (network drop, browser tab closed, page refresh):

1. The relay detects the WS close and calls `session.stop()`
2. For **PerTurnRuntime (Claude):** the subprocess is killed if a turn is in progress. The `session_id` captured from the previous turn's `system/init` is **not persisted by the relay** — it exists only in the `PerTurnRuntime` instance, which is garbage collected on disconnect.
3. For **persistent runtimes (Codex, Gemini):** the subprocess exits.

> **There is no automatic resume.** On reconnect, the client must send a new handshake and the relay starts a fresh session.

### Resuming a Claude Code conversation after reconnect

Claude Code maintains its own on-disk conversation store keyed by `session_id`. If the client saved the `session_id` from a previous session (received via the `session_start` event's `metadata.session_id`), it can pass it in the handshake:

```json
{"tool": "claude", "folder": "/repo", "session_id": "<previous_session_id>"}
```

The relay forwards this as `--resume <session_id>` to Claude Code, which loads the previous conversation history.

**Client responsibility:** save `session_id` from the `session_start` event (or from `input_ack` / other events) and include it in the next handshake to resume.

### Status after reconnect

After sending the handshake on a fresh connection, the client should:

1. Wait for `session_start` — confirms the process started successfully
2. If the process fails to start, an `error` event is emitted before the WS closes
3. The client should treat `session_end` or `error` as signals to show a reconnect/retry UI

### Auth on reconnect

Auth modules run on every `session.start()` call — i.e. on every new WebSocket connection. This means:

- **Gemini:** `ensure_gemini_auth()` runs before the subprocess starts, proactively refreshing the OAuth token if it expires within 5 minutes. No manual re-auth needed as long as credentials are on disk.
- **Claude:** OAuth credentials are checked fresh on each connection; if the user has valid OAuth creds, the API key is not injected.
- **Codex:** Same pattern — ChatGPT auth checked on each connection.

If credentials are missing or expired (and refresh fails), the relay emits an `error` event and the WS closes. The frontend should prompt the user to re-authenticate.

---

## Containerised multi-user deployment

When running in a multi-user Docker environment (like OhWise Lab), each user gets an isolated container. The relay runs **inside the user container** (`ai-relay serve`), while auth credential management runs in the **orchestrator container** (lab-ctrl).

### Path split

| Path | Visible from | Contains |
|------|-------------|----------|
| `/var/ohwise-lab-workspaces/{user_id}/` | lab-ctrl | All user workspace files |
| `/home/labuser/` | User container | Same files (bind-mounted from host) |

### Environment variables set by lab-ctrl

| Variable | Value | Purpose |
|----------|-------|---------|
| `HOME` | `/home/labuser` | Subprocess home (user container path) |
| `GEMINI_CLI_HOME` | `/var/ohwise-lab-workspaces/{user_id}/.gemini` | Relay-side path for `ensure_gemini_auth` to read/write creds |
| `GEMINI_OAUTH_CLIENT_ID` | `681255809395-...` | Proactive Gemini token refresh |
| `GEMINI_OAUTH_CLIENT_SECRET` | `GOCSPX-...` | Proactive Gemini token refresh |
| `AI_RELAY_CLAUDE_PREFER_OAUTH` | `1` | Use Claude desktop OAuth over API key |
| `AI_RELAY_CODEX_PREFER_CHATGPT` | `1` | Use Codex ChatGPT auth over API key |
| `AI_RELAY_GEMINI_PREFER_OAUTH` | `1` | Use Gemini OAuth over API key |

### Security properties

- **Filesystem isolation:** Each user container gets only their own workspace directory mounted (`{host_path}/{user_id}` → `/home/labuser`). No user can read another user's files.
- **Privilege restriction:** All user containers run with `--security-opt no-new-privileges:true`, `--cap-drop ALL`, `--cap-add NET_BIND_SERVICE`.
- **No published ports:** User containers communicate over an internal Docker network only.
