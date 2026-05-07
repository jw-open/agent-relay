# Changelog

All notable changes to ai-relay are documented here.

---

## [0.4.24] — 2026-05-07

### Fixed
- `pyproject.toml`, `README.md`: corrected license classifier from MIT to Apache-2.0.

---

## [0.4.23] — 2026-05-07

### Fixed
- `gemini.py`: Gemini CLI ACP mode exits the subprocess after every `session/prompt`
  turn (exit code 0). Previously the relay always called `session/new` on reconnect,
  silently losing conversation history between turns. Now the relay saves the ACP
  `sessionId` to `.gemini_acp_session` inside the session working folder and calls
  `session/load` on the next connection, restoring context. Falls back to `session/new`
  if the session cannot be loaded (e.g. expired or first turn).

  **Why Gemini exits per-turn:** Gemini CLI's ACP implementation (≥ 0.40.x) is
  designed to exit after each `session/prompt` completes rather than remaining as
  a persistent server. This differs from Claude Code (which uses `--resume` to
  reload a persistent session) and Codex (which uses a persistent app-server).
  The `session/load` call is the ACP-standard mechanism for resuming a prior
  session in a fresh process.

---

## [0.4.22] — 2026-05-06

### Added
- Snowflake Cortex `agent` mode via `/api/v2/cortex/agent:run` SSE endpoint.
  Full event parsing: text deltas → `RESPONSE`, thinking deltas → `REASONING`,
  tool calls → `TOOL_CALL`, tool results → `TOOL_RESULT`, status → `STATUS`.
  PAT auth (`X-Snowflake-Authorization-Token-Type: PROGRAMMATIC_ACCESS_TOKEN`).
  Multi-turn conversation history tracking. `chat` and `analyst` modes unchanged.

---

## [0.4.17] — 2026-05-04

### Fixed
- `gemini_auth.py`: `_creds_path()` now checks `GEMINI_CLI_HOME` env var first
  (mirrors `_settings_path` behaviour). Fixes containerised deployments where
  `HOME` is the container-side path (`/home/labuser`) but the relay process runs
  inside a different container that sees the workspace at a different path.

---

## [0.4.16] — 2026-05-04

### Fixed
- `gemini.py`: `session/prompt` timeout raised from 60 s to 300 s (5 min).
  Gemini permission dialogs require the user to respond before the RPC
  completes; the 60 s hard limit caused spurious "Timed out" errors during
  any interactive permission flow. Other RPCs (initialize, session/new,
  session/cancel) retain the 60 s timeout.

---

## [0.4.15] — 2026-05-04

### Fixed
- `codex.py`: `rawResponseItem/completed` for reasoning items now emits `text` from
  the item's `summary` array (o1-style visible thinking). Previously no text was set,
  so the reasoning event was invisible in the UI.

---

## [0.4.14] — 2026-05-04

### Fixed
- `codex.py`: `error` notification now correctly extracts `params["error"]["message"]`
  instead of `params["message"]` (which was always `None`). Previously Codex policy
  errors (e.g. cybersecurity flagged content) rendered as raw Python dict repr;
  now shows the user-friendly message string.

---

## [0.4.13] — 2026-05-04

### Fixed
- `gemini.py`: `session/prompt` RPC result was silently discarded, leaving the
  frontend stuck in "working" state indefinitely after each Gemini turn.
  Now emits `RESPONSE` on success and `ERROR` on failure (quota exceeded,
  auth error, timeout) so the frontend can reset its processing indicator.

---

## [0.4.12] — 2026-05-04

### Fixed
- `gemini.py`: `session/update` events now correctly extract the inner `update`
  object from ACP JSON-RPC params before passing to `_events_from_update`.
  Previously, the full `params` dict (`{"sessionId": "...", "update": {...}}`)
  was passed, causing `sessionUpdate` lookup to return `None` and silently
  dropping ALL Gemini agent responses (text, reasoning, tool calls) to the
  frontend.

---

## [0.4.11] — 2026-05-04

### Fixed
- `gemini.py`: STATUS event no longer emitted for internal RPC responses
  (`initialize`, `session/new`) — only user-visible updates reach the frontend.
- `gemini.py`: Dead code `elif b_type in {"inline_data", "image"}` corrected to
  `elif b_type == "inline_data"` (image was already handled above).
- `relay.py`: Removed incorrect `CLAUDE_OAUTH_CLIENT_ID` env injection (unused
  by Claude Code); Gemini OAuth config keys renamed to `gemini_oauth_client_id`
  / `gemini_oauth_client_secret`.

---

## [0.4.10] — 2026-05-04

### Fixed
- Runtime startup failures are now emitted as structured `error` events before
  the WebSocket closes. Gemini auth failures no longer appear as silent
  frontend disconnects.

---

## [0.4.9] — 2026-05-04

### Fixed
- Gemini ACP startup now fails fast with surfaced errors instead of hanging when
  authentication is missing or invalid.
- Gemini OAuth credentials in `HOME/.gemini/oauth_creds.json` now set
  `security.auth.selectedType=oauth-personal` and can take precedence over API
  environment credentials with `AI_RELAY_GEMINI_PREFER_OAUTH=1`.
- Gemini image blocks and permission responses now follow the current ACP
  schema used by Gemini CLI 0.40.x.

---

## [0.4.8] — 2026-05-03

### Fixed
- Codex ChatGPT auth in `HOME/.codex/auth.json` can now take precedence over
  API-key environment credentials when `AI_RELAY_CODEX_PREFER_CHATGPT=1`,
  matching Lab's per-user isolated auth model.

---

## [0.4.7] — 2026-05-03

### Fixed
- Claude Code desktop OAuth credentials can now take precedence over environment
  API-key credentials when `AI_RELAY_CLAUDE_PREFER_OAUTH=1`, matching Lab's
  per-user isolated auth model.

---

## [0.4.6] — 2026-05-03

### Fixed
- `PerTurnRuntime._pump_turn`: transport errors (e.g. `LimitOverrunError`, `ValueError`)
  are now caught and emitted as `error` relay events to the frontend.  Previously these
  exceptions propagated unhandled, leaving the session in an indefinite "working" state
  with no visible feedback.

---

## [0.4.5] — 2026-05-03

### Fixed
- `StructuredProcessTransport.start()`: raised asyncio StreamReader `limit` from 64 KB
  to 100 MB.  Claude Code echoes the full user message (including base64-encoded images)
  as a single JSON line on stdout; the default 64 KB limit raised `LimitOverrunError` /
  `ValueError` on any image-containing turn, causing the relay to silently drop the
  response and leave the session hanging.

---

## [0.4.4] — 2026-05-03

### Fixed
- `RelayServer.serve()`: raised WebSocket `max_size` from 1MB to 50MB.
  Image payloads (base64-encoded) were triggering WebSocket 1009 "message too big"
  errors for any image over ~750KB, causing the connection to drop silently.

---

## [0.4.3] — 2026-05-03

### Fixed
- `PerTurnRuntime._extract_prompt`: image-only content blocks (no text) now return
  a non-empty sentinel `"[image]"` so the turn is executed. Previously the runtime
  silently skipped image-only messages, causing the agent to hang indefinitely.

---

## [0.4.2] — 2026-05-03

### Fixed
- `PerTurnRuntime.start()` no longer queues a duplicate `SESSION_START` event.
  `relay.py` already emits `SESSION_START` before calling `runtime.start()`,
  so this caused two `session_start` events per connection in server mode.
- `ClaudeStructuredRuntime`: `session_id` field in the stream-json stdin payload
  now uses Claude Code's own conversation ID (captured from `system/init`), not
  the relay's DB UUID. Previously, Claude Code tried to `--resume` the DB UUID
  and errored with "No conversation found".

---

## [0.4.1] — 2026-05-03

### Added
- **`ai-relay serve` subcommand** — persistent WebSocket server mode designed for containers and daemons. Each incoming connection becomes one independent agent session. Ideal for running ai-relay inside Docker with `CMD ["ai-relay", "serve", "--port", "9000"]`.
- **`PerTurnRuntime`** — new internal runtime that restarts the agent subprocess per user turn and captures `session_id` from the `system/init` event for `--resume` on the next turn. Enables true multi-turn conversations in server mode without keeping a persistent subprocess alive between turns.
- Claude Code and Gemini CLI now use `PerTurnRuntime` automatically in server mode.

### Changed
- Gemini adapter improvements for headless `stream-json` mode.

---

## [0.4.0] — 2026-05-02

### Added
- Initial `PerTurnRuntime` implementation.
- Gemini CLI adapter improvements.

---

## [0.3.0] — 2026-04-15

### Added
- Snowflake Cortex adapter (chat + analyst modes).
- `context_compacted` event type.

---

## [0.2.9] — 2026-04-01

### Added
- `permission_cancelled` and `control_response` event types.
- `tool_progress` event for native tool progress streaming.

---

## [0.2.x]

- OpenAI Codex adapter (`codex app-server` JSON-RPC protocol).
- Gemini CLI adapter (`stream-json` headless mode).
- Structured event parsing: `reasoning`, `tool_call`, `tool_result`, `file_diff`, `response`.
- `quota_warning` and `context_warning` events.
- `interrupt` and `permission_response` client messages.
