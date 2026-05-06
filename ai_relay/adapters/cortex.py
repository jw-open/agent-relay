"""Snowflake Cortex API adapter.

Supports three Cortex modes:
  - agent   : Cortex Agents REST API  (/api/v2/cortex/agent:run)    [default]
  - chat    : Cortex Inference         (/api/v2/cortex/v1/chat/completions)
  - analyst : Cortex Analyst           (/api/v2/cortex/analyst/message)

Authentication:
  PAT  : Bearer token + X-Snowflake-Authorization-Token-Type: PROGRAMMATIC_ACCESS_TOKEN
  JWT  : Bearer token (no special header — use token_type="JWT" in config)

Config keys (all under handshake["snowflake"] dict or top-level handshake):
  account_url    : https://<account>.snowflakecomputing.com
  token          : PAT or JWT string
  token_type     : "PROGRAMMATIC_ACCESS_TOKEN" (default for PAT) | "KEYPAIR_JWT" | none
  model          : Snowflake model name (default: claude-3-5-sonnet)
  mode           : agent | chat | analyst  (default: agent)
  tools          : list of tool specs for agent mode (optional)
  semantic_model_file / semantic_model / semantic_view : required for analyst mode
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from ..events import EventType, RelayEvent
from .base import AgentRuntime, BaseAdapter

logger = logging.getLogger(__name__)


class CortexRuntime(AgentRuntime):
    """Snowflake Cortex runtime using REST requests and SSE streaming."""

    def __init__(
        self,
        session_id: str,
        model: Optional[str],
        config: dict[str, Any],
    ):
        super().__init__(session_id)
        self.model = model
        self.config = config
        self.snowflake = config.get("snowflake") if isinstance(config.get("snowflake"), dict) else {}
        self.mode = str(config.get("mode") or self.snowflake.get("mode") or "agent")
        self._events: asyncio.Queue[Optional[RelayEvent]] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        self._history: list[dict[str, Any]] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Accumulate assistant text across streamed deltas (for history)
        self._current_assistant_text: str = ""
        self._current_assistant_thinking: str = ""

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        return

    async def read_event(self) -> Optional[RelayEvent]:
        return await self._events.get()

    async def handle_client_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "interrupt":
            for task in list(self._tasks):
                task.cancel()
            self._tasks.clear()
            await self._events.put(RelayEvent(
                type=EventType.STATUS,
                session_id=self.session_id,
                status="interrupted",
            ))
            return

        content = msg.get("content")
        if content is None:
            content = msg.get("text", "")
        text = self._extract_text(content)
        if not text:
            return
        task = asyncio.create_task(self._run_request(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        await self._events.put(None)

    async def wait(self) -> Optional[int]:
        return None

    async def _run_request(self, text: str) -> None:
        try:
            # Append user message to history before streaming
            user_msg: dict[str, Any]
            if self.mode in ("agent", "analyst"):
                user_msg = {"role": "user", "content": [{"type": "text", "text": text}]}
            else:
                user_msg = {"role": "user", "content": text}
            self._history.append(user_msg)
            self._current_assistant_text = ""
            self._current_assistant_thinking = ""

            await asyncio.to_thread(self._request_sync)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._events.put(RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=str(exc),
            ))

    def _request_sync(self) -> None:
        url = self._endpoint()
        body = self._request_body()
        data = json.dumps(body).encode("utf-8")
        headers = self._headers()
        logger.info("[%s] Cortex %s → %s model=%s", self.session_id, self.mode, url,
                    body.get("model", "?"))
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=float(self._setting("timeout", 300))) as response:
                self._consume_sse(response)
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            self._put(RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=f"Snowflake Cortex HTTP {exc.code}: {body_text}",
            ))

    def _consume_sse(self, response: Any) -> None:
        """Parse SSE stream and dispatch events. Handles both event: / data: and bare data: formats."""
        event_name = "message"
        data_lines: list[str] = []
        for raw in response:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                self._flush_sse_event(event_name, data_lines)
                event_name = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        # Flush any trailing event
        self._flush_sse_event(event_name, data_lines)
        # Append completed assistant message to history
        if self._current_assistant_text:
            self._history.append({
                "role": "assistant",
                "content": [{"type": "text", "text": self._current_assistant_text}],
            })
            self._current_assistant_text = ""

    def _flush_sse_event(self, event_name: str, data_lines: list[str]) -> None:
        if not data_lines:
            return
        data_text = "\n".join(data_lines)
        if data_text == "[DONE]":
            self._put(RelayEvent(
                type=EventType.STATUS,
                session_id=self.session_id,
                status="done",
            ))
            return
        try:
            payload = json.loads(data_text)
        except json.JSONDecodeError:
            payload = {"data": data_text}
        for event in self._events_from_sse(event_name, payload):
            self._put(event)

    def _events_from_sse(self, event_name: str, payload: dict[str, Any]) -> list[RelayEvent]:
        if self.mode == "analyst":
            return self._analyst_events(event_name, payload)
        if self.mode == "agent":
            return self._agent_events(event_name, payload)
        return self._chat_events(event_name, payload)

    # ── Agent mode (Cortex Agents REST API) ──────────────────────────────────

    def _agent_events(self, event_name: str, payload: dict[str, Any]) -> list[RelayEvent]:
        """Parse Cortex Agents API SSE events.

        Event types emitted by /api/v2/cortex/agent:run:
          response.text.delta        — streaming text chunk
          response.thinking.delta    — extended thinking / reasoning chunk
          response.tool_use          — agent invoking a tool (server-side)
          response.tool_result       — result of a server-side tool call
          response.status            — status updates (queued, running, done, error)
          response                   — final complete message (non-streaming)
          error                      — API error
          message.start / message.stop — message lifecycle markers
          content.block.start / .stop  — content block lifecycle
        """
        ev_type = payload.get("type") or event_name

        # ── Text streaming ──────────────────────────────────────────────────
        if ev_type == "response.text.delta":
            delta = payload.get("delta") or payload.get("text") or ""
            self._current_assistant_text += delta
            return [RelayEvent(
                type=EventType.RESPONSE,
                session_id=self.session_id,
                text=delta,
                raw=payload,
            )]

        # ── Thinking / reasoning ────────────────────────────────────────────
        if ev_type == "response.thinking.delta":
            delta = payload.get("delta") or payload.get("thinking") or ""
            self._current_assistant_thinking += delta
            return [RelayEvent(
                type=EventType.REASONING,
                session_id=self.session_id,
                text=delta,
                raw=payload,
            )]

        # ── Tool calls (server-side; info-only, no client-side approval needed) ─
        if ev_type == "response.tool_use":
            tool_info = payload.get("tool_use") or {}
            tool_name = tool_info.get("name") or tool_info.get("type") or "tool"
            tool_input = tool_info.get("input") or {}
            tool_id = tool_info.get("id") or ""
            logger.info("[%s] Cortex tool_use name=%s id=%s", self.session_id, tool_name, tool_id)
            return [RelayEvent(
                type=EventType.TOOL_CALL,
                session_id=self.session_id,
                tool=tool_name,
                tool_use_id=tool_id,
                args=tool_input,
                text=self._format_tool_call(tool_name, tool_input),
                raw=payload,
            )]

        # ── Tool results ────────────────────────────────────────────────────
        if ev_type == "response.tool_result":
            result_info = payload.get("tool_result") or {}
            tool_id = result_info.get("tool_use_id") or ""
            content = result_info.get("content") or []
            result_text = self._extract_tool_result_text(content)
            return [RelayEvent(
                type=EventType.TOOL_RESULT,
                session_id=self.session_id,
                tool_use_id=tool_id,
                text=result_text,
                raw=payload,
            )]

        # ── Status events ────────────────────────────────────────────────────
        if ev_type == "response.status":
            status = payload.get("status") or event_name
            return [RelayEvent(
                type=EventType.STATUS,
                session_id=self.session_id,
                status=str(status),
                raw=payload,
            )]

        # ── Final complete response (non-streaming or summary) ───────────────
        if ev_type == "response":
            output = payload.get("output") or []
            events: list[RelayEvent] = []
            for block in output:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text:
                        events.append(RelayEvent(
                            type=EventType.RESPONSE,
                            session_id=self.session_id,
                            text=text,
                            raw=block,
                        ))
                elif btype == "tool_use":
                    tool_name = block.get("name") or "tool"
                    tool_input = block.get("input") or {}
                    events.append(RelayEvent(
                        type=EventType.TOOL_CALL,
                        session_id=self.session_id,
                        tool=tool_name,
                        args=tool_input,
                        text=self._format_tool_call(tool_name, tool_input),
                        raw=block,
                    ))
            stop_reason = payload.get("stop_reason")
            if stop_reason:
                events.append(RelayEvent(
                    type=EventType.STATUS,
                    session_id=self.session_id,
                    status=str(stop_reason),
                    raw=payload,
                ))
            return events

        # ── Error ─────────────────────────────────────────────────────────────
        if ev_type == "error" or "error" in payload:
            err = payload.get("error") or payload
            msg = err.get("message") or err.get("detail") or json.dumps(err) if isinstance(err, dict) else str(err)
            return [RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=msg,
                raw=payload,
            )]

        # ── Anthropic-style content block delta (model may emit this format too) ─
        if ev_type == "content.block.delta":
            delta = payload.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text") or ""
                self._current_assistant_text += text
                return [RelayEvent(
                    type=EventType.RESPONSE,
                    session_id=self.session_id,
                    text=text,
                    raw=payload,
                )]
            if dtype == "thinking_delta":
                thinking = delta.get("thinking") or ""
                return [RelayEvent(
                    type=EventType.REASONING,
                    session_id=self.session_id,
                    text=thinking,
                    raw=payload,
                )]

        # ── SQL / chart / table special outputs ──────────────────────────────
        if ev_type in ("response.sql", "response.chart", "response.table"):
            label = ev_type.split(".")[-1].upper()
            content = payload.get("sql") or payload.get("data") or json.dumps(payload)
            return [RelayEvent(
                type=EventType.TOOL_RESULT,
                session_id=self.session_id,
                tool=label.lower(),
                text=f"[{label}]\n{content}",
                raw=payload,
            )]

        # ── Lifecycle markers (no-op) ─────────────────────────────────────────
        if ev_type in ("message.start", "message.stop", "content.block.start", "content.block.stop",
                       "message.delta", "ping"):
            return []

        # ── Unknown — pass through as stream_event ────────────────────────────
        return [RelayEvent(
            type=EventType.STREAM_EVENT,
            session_id=self.session_id,
            metadata={"event": ev_type},
            raw=payload,
        )]

    @staticmethod
    def _format_tool_call(name: str, inputs: dict[str, Any]) -> str:
        """Format a tool call for display (Claude Code style)."""
        if not inputs:
            return name
        try:
            args_str = json.dumps(inputs, indent=2, ensure_ascii=False)
        except Exception:
            args_str = str(inputs)
        return f"{name}\n{args_str}"

    @staticmethod
    def _extract_tool_result_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(item.get("text") or item.get("value") or json.dumps(item))
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(content or "")

    # ── Analyst mode ─────────────────────────────────────────────────────────

    def _analyst_events(self, event_name: str, payload: dict[str, Any]) -> list[RelayEvent]:
        if event_name == "status":
            return [RelayEvent(
                type=EventType.STATUS,
                session_id=self.session_id,
                status=payload.get("status"),
                raw=payload,
            )]
        if event_name == "message.content.delta":
            delta_type = payload.get("type")
            if delta_type == "sql":
                statement = payload.get("statement_delta") or payload.get("statement")
                return [RelayEvent(
                    type=EventType.TOOL_CALL,
                    session_id=self.session_id,
                    tool="sql",
                    args={"statement_delta": statement, "index": payload.get("index")},
                    text=statement,
                    raw=payload,
                )]
            return [RelayEvent(
                type=EventType.RESPONSE,
                session_id=self.session_id,
                text=payload.get("text_delta") or payload.get("text") or "",
                raw=payload,
            )]
        if event_name == "warnings":
            return [RelayEvent(
                type=EventType.CONTEXT_WARNING,
                session_id=self.session_id,
                text=json.dumps(payload),
                raw=payload,
            )]
        if event_name == "error":
            return [RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=payload.get("message") or json.dumps(payload),
                raw=payload,
            )]
        if event_name == "response_metadata":
            return [RelayEvent(
                type=EventType.STATUS,
                session_id=self.session_id,
                status="response_metadata",
                metadata=payload,
                raw=payload,
            )]
        if event_name == "done":
            return [RelayEvent(type=EventType.STATUS, session_id=self.session_id, status="done", raw=payload)]
        return [RelayEvent(type=EventType.STREAM_EVENT, session_id=self.session_id,
                           metadata={"event": event_name}, raw=payload)]

    # ── Chat mode (OpenAI-compatible) ─────────────────────────────────────────

    def _chat_events(self, event_name: str, payload: dict[str, Any]) -> list[RelayEvent]:
        if "error" in payload:
            return [RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=json.dumps(payload["error"]),
                raw=payload,
            )]
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                self._current_assistant_text += content
                return [RelayEvent(
                    type=EventType.RESPONSE,
                    session_id=self.session_id,
                    text=content,
                    raw=payload,
                )]
            finish_reason = choices[0].get("finish_reason")
            if finish_reason:
                return [RelayEvent(
                    type=EventType.STATUS,
                    session_id=self.session_id,
                    status=str(finish_reason),
                    raw=payload,
                )]
        return [RelayEvent(type=EventType.STREAM_EVENT, session_id=self.session_id,
                           metadata={"event": event_name}, raw=payload)]

    # ── Endpoint / headers / body ─────────────────────────────────────────────

    def _endpoint(self) -> str:
        account_url = str(self._setting("account_url", "")).rstrip("/")
        if not account_url:
            raise ValueError(
                "Snowflake Cortex requires snowflake.account_url "
                "(e.g. https://<account>.snowflakecomputing.com)"
            )
        if self.mode == "analyst":
            return f"{account_url}/api/v2/cortex/analyst/message"
        if self.mode == "agent":
            return f"{account_url}/api/v2/cortex/agent:run"
        return f"{account_url}/api/v2/cortex/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        token = self._setting("token")
        token_env = self._setting("token_env", "SNOWFLAKE_PAT")
        if not token and token_env:
            token = os.environ.get(str(token_env))
        if not token:
            raise ValueError(
                "Snowflake Cortex requires a token (PAT). "
                "Set snowflake.token in config or SNOWFLAKE_PAT env var."
            )
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        # Default to PAT token type unless explicitly overridden
        token_type = self._setting("token_type", "PROGRAMMATIC_ACCESS_TOKEN")
        if token_type:
            headers["X-Snowflake-Authorization-Token-Type"] = str(token_type)
        return headers

    def _request_body(self) -> dict[str, Any]:
        model = self.model or str(self._setting("model", "claude-3-5-sonnet"))

        if self.mode == "analyst":
            body: dict[str, Any] = {
                "messages": self._history,
                "stream": True,
            }
            for key in ("semantic_model_file", "semantic_model", "semantic_models", "semantic_view"):
                value = self._setting(key)
                if value is not None:
                    body[key] = value
            if not any(k in body for k in ("semantic_model_file", "semantic_model", "semantic_models", "semantic_view")):
                raise ValueError(
                    "Cortex Analyst requires one of: snowflake.semantic_model_file, "
                    "semantic_model, semantic_models, or semantic_view."
                )
            return body

        if self.mode == "agent":
            # Cortex Agents REST API body
            tools = self._setting("tools") or []
            body = {
                "model": model,
                "messages": self._history,
                "stream": True,
            }
            if tools:
                body["tools"] = tools
            # Optional response format
            response_format = self._setting("response_format")
            if response_format:
                body["response_format"] = response_format
            return body

        # chat mode (OpenAI-compatible)
        messages: list[dict[str, Any]] = []
        for msg in self._history:
            if isinstance(msg.get("content"), list):
                # Convert agent-style content to flat string for chat mode
                text_parts = [
                    b.get("text", "") for b in msg["content"]
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                messages.append({"role": msg["role"], "content": " ".join(text_parts)})
            else:
                messages.append(msg)
        return {
            "model": model,
            "messages": messages,
            "stream": True,
        }

    def _setting(self, key: str, default: Any = None) -> Any:
        if key in self.snowflake:
            return self.snowflake[key]
        return self.config.get(key, default)

    def _put(self, event: RelayEvent) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._events.put_nowait, event)
        else:
            self._events.put_nowait(event)

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                    parts.append(str(item.get("text", "")))
            return "\n".join(part for part in parts if part)
        return str(content or "")


class CortexAdapter(BaseAdapter):
    tool_name = "cortex"
    protocol = "http-sse"
    requires_executable = False

    @classmethod
    def build_command(
        cls,
        folder: str,
        model: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
    ) -> list[str]:
        return []

    @classmethod
    def create_runtime(
        cls,
        session_id: str,
        folder: str,
        model: Optional[str],
        extra_args: Optional[list[str]],
        env: dict[str, str],
        config: Optional[dict[str, Any]] = None,
    ) -> AgentRuntime:
        return CortexRuntime(session_id=session_id, model=model, config=config or {})
