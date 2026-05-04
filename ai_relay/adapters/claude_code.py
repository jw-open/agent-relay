"""Claude Code adapter."""

from __future__ import annotations
import asyncio
import json
from typing import Any, Optional

from ..events import EventType, RelayEvent
from ..transports import StructuredProcessTransport
from .base import AgentRuntime, BaseAdapter


class ClaudeStructuredRuntime(AgentRuntime):
    """Claude Code runtime using stream-json stdin/stdout instead of TUI scraping."""

    def __init__(
        self,
        session_id: str,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        claude_session_id: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ):
        super().__init__(session_id, config)
        # claude_session_id is Claude Code's own conversation ID (captured from
        # system/init on the first turn).  It goes into the stream-json stdin payload
        # and is separate from the relay's session_id (which is a DB UUID).
        # None on first turn means: start a new conversation.
        self._claude_session_id = claude_session_id
        self.transport = StructuredProcessTransport(cmd, cwd, env)
        self._stderr_queue: asyncio.Queue[RelayEvent] = asyncio.Queue()
        self._event_queue: asyncio.Queue[RelayEvent] = asyncio.Queue()
        self._stderr_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        from ..claude_auth import ensure_claude_token
        ensure_claude_token(self.transport.env)
        await self.transport.start()
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def read_event(self) -> Optional[RelayEvent]:
        if not self._event_queue.empty():
            return await self._event_queue.get()
        if not self._stderr_queue.empty():
            return await self._stderr_queue.get()
        line = await self.transport.readline()
        if not line:
            if not self._stderr_queue.empty():
                return await self._stderr_queue.get()
            return None
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        if not text:
            return RelayEvent(type=EventType.STDOUT, session_id=self.session_id, text="")
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return RelayEvent(type=EventType.STDOUT, session_id=self.session_id, text=text)
        if not isinstance(msg, dict):
            return RelayEvent(type=EventType.STDOUT, session_id=self.session_id, text=text)
        events = self._events_from_sdk_message(msg)
        for event in events[1:]:
            await self._event_queue.put(event)
        return events[0]

    async def handle_client_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "permission_response":
            await self._send_permission_response(msg)
            return
        if msg_type == "interrupt":
            await self._send_control_request({"subtype": "interrupt"})
            return
        if msg_type == "set_model":
            await self._send_control_request({"subtype": "set_model", "model": msg.get("model")})
            return
        if msg_type == "set_permission_mode":
            mode = msg.get("mode") or msg.get("permission_mode")
            if mode:
                await self._send_control_request({"subtype": "set_permission_mode", "mode": mode})
            return
        if msg_type == "control_request" and isinstance(msg.get("request"), dict):
            await self._send_control_request(msg["request"], msg.get("request_id"))
            return
        if msg_type == "control_response" and isinstance(msg.get("response"), dict):
            await self.transport.write_json_line(json.dumps({
                "type": "control_response",
                "response": msg["response"],
            }))
            return

        content = msg.get("content")
        if content is None:
            content = msg.get("text", "")
        if content == "" or content is None:
            return
        await self._send_user_message(content, msg.get("uuid"))

    async def stop(self) -> None:
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        await self.transport.stop()

    async def wait(self) -> Optional[int]:
        return await self.transport.wait()

    async def _send_user_message(self, content: Any, uuid: Optional[str] = None) -> None:
        payload: dict[str, Any] = {
            "type": "user",
            # Use Claude Code's own conversation ID, NOT the relay DB session UUID.
            # On the first turn _claude_session_id is None → empty string → new conversation.
            "session_id": self._claude_session_id or "",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }
        if uuid:
            payload["uuid"] = uuid
        await self.transport.write_json_line(json.dumps(payload))

    async def _send_permission_response(self, msg: dict[str, Any]) -> None:
        request_id = msg.get("request_id")
        if not request_id:
            return
        behavior = msg.get("behavior")
        response: dict[str, Any]
        if behavior == "deny":
            response = {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "deny",
                    "message": msg.get("message", "Denied by user"),
                    **({"toolUseID": msg["tool_use_id"]} if msg.get("tool_use_id") else {}),
                },
            }
        else:
            response = {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "allow",
                    "updatedInput": msg.get("updatedInput", msg.get("updated_input", msg.get("input", {}))),
                    **({"toolUseID": msg["tool_use_id"]} if msg.get("tool_use_id") else {}),
                },
            }
        await self.transport.write_json_line(json.dumps({
            "type": "control_response",
            "response": response,
        }))

    async def _send_control_request(
        self,
        request: dict[str, Any],
        request_id: Optional[str] = None,
    ) -> None:
        await self.transport.write_json_line(json.dumps({
            "type": "control_request",
            "request_id": request_id or f"relay-{id(request)}",
            "request": request,
        }))

    async def _read_stderr(self) -> None:
        while True:
            line = await self.transport.read_stderr()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            if text:
                await self._stderr_queue.put(RelayEvent(
                    type=EventType.STDERR,
                    session_id=self.session_id,
                    text=text,
                ))

    def _events_from_sdk_message(self, msg: dict[str, Any]) -> list[RelayEvent]:
        msg_type = msg.get("type")
        if msg_type == "assistant":
            events = [RelayEvent(
                type=EventType.ASSISTANT_MESSAGE,
                session_id=self.session_id,
                content=msg.get("message", {}).get("content"),
                raw=msg,
            )]
            for block in self._content_blocks(msg):
                if block.get("type") == "tool_use":
                    events.append(RelayEvent(
                        type=EventType.TOOL_CALL,
                        session_id=self.session_id,
                        tool=block.get("name"),
                        tool_use_id=block.get("id"),
                        args=block.get("input"),
                        raw=msg,
                    ))
            return events
        if msg_type == "user":
            events = [RelayEvent(
                type=EventType.USER_MESSAGE,
                session_id=self.session_id,
                content=msg.get("message", {}).get("content"),
                raw=msg,
            )]
            for block in self._content_blocks(msg):
                if block.get("type") == "tool_result":
                    events.append(RelayEvent(
                        type=EventType.TOOL_RESULT,
                        session_id=self.session_id,
                        tool_use_id=block.get("tool_use_id"),
                        content=block.get("content"),
                        raw=msg,
                    ))
            return events
        if msg_type == "stream_event":
            return [RelayEvent(
                type=EventType.STREAM_EVENT,
                session_id=self.session_id,
                raw=msg,
                metadata={"event": msg.get("event")},
            )]
        if msg_type == "system":
            subtype = msg.get("subtype")
            if subtype == "status":
                return [RelayEvent(
                    type=EventType.STATUS,
                    session_id=self.session_id,
                    status=msg.get("status"),
                    raw=msg,
                )]
            if subtype == "compact_boundary":
                return [RelayEvent(
                    type=EventType.CONTEXT_COMPACTED,
                    session_id=self.session_id,
                    text="Conversation compacted",
                    metadata=msg.get("compact_metadata"),
                    raw=msg,
                )]
            return [RelayEvent(type=EventType.STATUS, session_id=self.session_id, raw=msg)]
        if msg_type == "result":
            return [RelayEvent(
                type=EventType.RESPONSE if msg.get("subtype") == "success" else EventType.ERROR,
                session_id=self.session_id,
                text=msg.get("result") or ", ".join(msg.get("errors") or []),
                raw=msg,
            )]
        if msg_type == "tool_progress":
            return [RelayEvent(
                type=EventType.TOOL_PROGRESS,
                session_id=self.session_id,
                tool=msg.get("tool_name"),
                tool_use_id=msg.get("tool_use_id"),
                metadata=msg,
                raw=msg,
            )]
        if msg_type == "control_request":
            request = msg.get("request") or {}
            if request.get("subtype") == "can_use_tool":
                return [RelayEvent(
                    type=EventType.PERMISSION_REQUEST,
                    session_id=self.session_id,
                    request_id=msg.get("request_id"),
                    tool=request.get("tool_name"),
                    args=request.get("input"),
                    tool_use_id=request.get("tool_use_id"),
                    text=request.get("description") or request.get("decision_reason"),
                    metadata={
                        "permission_suggestions": request.get("permission_suggestions"),
                        "blocked_path": request.get("blocked_path"),
                        "agent_id": request.get("agent_id"),
                    },
                    raw=msg,
                )]
            return [RelayEvent(
                type=EventType.STATUS,
                session_id=self.session_id,
                request_id=msg.get("request_id"),
                metadata={"control_request": request},
                raw=msg,
            )]
        if msg_type == "control_response":
            return [RelayEvent(
                type=EventType.CONTROL_RESPONSE,
                session_id=self.session_id,
                request_id=(msg.get("response") or {}).get("request_id"),
                raw=msg,
            )]
        if msg_type == "control_cancel_request":
            return [RelayEvent(
                type=EventType.PERMISSION_CANCELLED,
                session_id=self.session_id,
                request_id=msg.get("request_id"),
                raw=msg,
            )]
        return [RelayEvent(type=EventType.STDOUT, session_id=self.session_id, raw=msg)]

    @staticmethod
    def _content_blocks(msg: dict[str, Any]) -> list[dict[str, Any]]:
        content = (msg.get("message") or {}).get("content")
        if not isinstance(content, list):
            return []
        return [block for block in content if isinstance(block, dict)]


class ClaudeCodeAdapter(BaseAdapter):
    tool_name = "claude-code"
    protocol = "structured"

    @classmethod
    def build_command(cls, folder: str, model: Optional[str] = None,
                      extra_args: Optional[list[str]] = None) -> list[str]:
        cmd = [
            "claude",
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--replay-user-messages",
            "--permission-prompt-tool",
            "stdio",
        ]
        if model:
            cmd += ["--model", model]
        if extra_args:
            cmd += extra_args
        return cmd

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
        return ClaudeStructuredRuntime(
            session_id=session_id,
            cmd=cls.build_command(folder, model, extra_args),
            cwd=folder,
            env=env,
            config=config,
        )
