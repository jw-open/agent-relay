"""Google Gemini CLI adapter."""

from __future__ import annotations
import asyncio
import json
from typing import Any, Optional

from ..events import EventType, RelayEvent
from ..transports import StructuredProcessTransport
from .base import AgentRuntime, BaseAdapter


class GeminiStructuredRuntime(AgentRuntime):
    """Gemini CLI persistent stream-json runtime using ACP mode (JSON-RPC 2.0)."""

    def __init__(
        self,
        session_id: str,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
    ):
        super().__init__(session_id)
        self.transport = StructuredProcessTransport(cmd, cwd, env)
        self.cwd = cwd
        self._event_queue: asyncio.Queue[Optional[RelayEvent]] = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._next_id = 1
        self._pending_requests: dict[int, asyncio.Future[Any]] = {}
        self._acp_session_id: Optional[str] = None

    async def start(self) -> None:
        await self.transport.start()
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        
        # ACP Handshake
        await self._request("initialize", {"protocolVersion": 1})
        
        # Create ACP session
        res = await self._request("session/new", {
            "cwd": self.cwd,
            "mcpServers": [],
        })
        if "result" in res:
            self._acp_session_id = res["result"].get("sessionId")

    async def read_event(self) -> Optional[RelayEvent]:
        return await self._event_queue.get()

    async def handle_client_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "interrupt":
            await self._request("session/cancel", {"sessionId": self._acp_session_id} if self._acp_session_id else {})
            return
        if msg_type == "set_model":
            await self._request("session/set_model", {
                "sessionId": self._acp_session_id,
                "model": msg.get("model")
            } if self._acp_session_id else {"model": msg.get("model")})
            return

        content = msg.get("content")
        if content is None:
            content = msg.get("text", "")
        if not content:
            return
            
        # Format prompt as array of parts if it is a string
        prompt_parts = []
        if isinstance(content, str):
            prompt_parts = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            prompt_parts = content
        else:
            prompt_parts = [{"type": "text", "text": str(content)}]

        # Send prompt
        params = {
            "prompt": prompt_parts,
        }
        if self._acp_session_id:
            params["sessionId"] = self._acp_session_id
            
        asyncio.create_task(self._request("session/prompt", params))

    async def stop(self) -> None:
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        await self.transport.stop()
        await self._event_queue.put(None)

    async def wait(self) -> Optional[int]:
        return await self.transport.wait()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        req_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending_requests[req_id] = future
        
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": req_id
        }
        await self.transport.write_json_line(json.dumps(payload))
        return await future

    async def _read_stdout(self) -> None:
        try:
            while True:
                line = await self.transport.readline()
                if not line:
                    break
                
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if not text:
                    continue
                    
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    await self._event_queue.put(RelayEvent(type=EventType.STDOUT, session_id=self.session_id, text=text))
                    continue

                if not isinstance(msg, dict):
                    await self._event_queue.put(RelayEvent(type=EventType.STDOUT, session_id=self.session_id, text=text))
                    continue

                # Handle JSON-RPC
                if "method" in msg:
                    method = msg["method"]
                    params = msg.get("params", {})
                    if method == "session/update":
                        for event in self._events_from_update(params):
                            await self._event_queue.put(event)
                    else:
                        await self._event_queue.put(RelayEvent(
                            type=EventType.STREAM_EVENT,
                            session_id=self.session_id,
                            raw=msg,
                        ))
                elif "update" in msg:
                    # Native Gemini update format: {"sessionId": "...", "update": {...}}
                    for event in self._events_from_update(msg["update"]):
                        await self._event_queue.put(event)
                elif "id" in msg:
                    req_id = msg["id"]
                    if req_id in self._pending_requests:
                        self._pending_requests[req_id].set_result(msg)
                    await self._event_queue.put(RelayEvent(
                        type=EventType.STATUS,
                        session_id=self.session_id,
                        status="result",
                        raw=msg,
                    ))
                else:
                    await self._event_queue.put(RelayEvent(type=EventType.STDOUT, session_id=self.session_id, text=text))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await self._event_queue.put(RelayEvent(type=EventType.ERROR, session_id=self.session_id, text=f"Reader error: {e}"))
        finally:
            await self._event_queue.put(None)

    async def _read_stderr(self) -> None:
        try:
            while True:
                line = await self.transport.read_stderr()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if text:
                    await self._event_queue.put(RelayEvent(
                        type=EventType.STDERR,
                        session_id=self.session_id,
                        text=text,
                    ))
        except asyncio.CancelledError:
            pass

    def _events_from_update(self, update: dict[str, Any]) -> list[RelayEvent]:
        events = []
        kind = update.get("sessionUpdate")
        
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            text = content.get("text", "")
            if text:
                events.append(RelayEvent(
                    type=EventType.ASSISTANT_MESSAGE,
                    session_id=self.session_id,
                    text=text,
                    raw=update,
                ))
        elif kind == "agent_thought_chunk":
            content = update.get("content", {})
            text = content.get("text", "")
            if text:
                events.append(RelayEvent(
                    type=EventType.REASONING,
                    session_id=self.session_id,
                    text=text,
                    raw=update,
                ))
        elif kind in {"tool_call", "tool_call_update"}:
            events.append(RelayEvent(
                type=EventType.TOOL_CALL,
                session_id=self.session_id,
                tool=update.get("toolName"),
                args=update.get("args"),
                tool_use_id=update.get("toolCallId"),
                text=update.get("title"),
                raw=update,
            ))
        
        # Handle 'delta' format just in case (ACP spec)
        if "delta" in update:
            delta = update["delta"]
            if delta.get("type") == "message":
                content = delta.get("content")
                text = ""
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text":
                            text += block.get("text", "")
                elif isinstance(content, str):
                    text = content
                if text:
                    events.append(RelayEvent(
                        type=EventType.ASSISTANT_MESSAGE,
                        session_id=self.session_id,
                        text=text,
                        raw=update,
                    ))
            elif delta.get("type") == "tool_call":
                events.append(RelayEvent(
                    type=EventType.TOOL_CALL,
                    session_id=self.session_id,
                    tool=delta.get("name"),
                    args=delta.get("args"),
                    tool_use_id=delta.get("id"),
                    raw=update,
                ))

        if not events:
            events.append(RelayEvent(
                type=EventType.STREAM_EVENT,
                session_id=self.session_id,
                raw=update,
            ))
        return events


class GeminiAdapter(BaseAdapter):
    tool_name = "gemini"
    protocol = "structured"

    @classmethod
    def build_command(cls, folder: str, model: Optional[str] = None,
                      extra_args: Optional[list[str]] = None) -> list[str]:
        cmd = ["gemini", "--acp", "--output-format", "stream-json"]
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
        return GeminiStructuredRuntime(
            session_id=session_id,
            cmd=cls.build_command(folder, model, extra_args),
            cwd=folder,
            env=env,
        )
