"""Google Gemini CLI adapter."""

from __future__ import annotations
import asyncio
import json
import uuid
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
        config: Optional[dict[str, Any]] = None,
    ):
        super().__init__(session_id, config)
        self.transport = StructuredProcessTransport(cmd, cwd, env)
        self.cwd = cwd
        self._event_queue: asyncio.Queue[Optional[RelayEvent]] = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._next_id = 1
        self._pending_requests: dict[int, asyncio.Future[Any]] = {}
        self._acp_session_id: Optional[str] = None
        self._initialized = False

    async def start(self) -> None:
        from ..gemini_auth import ensure_gemini_auth

        ensure_gemini_auth(self.transport.env)
        await self.transport.start()
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        
        await self._request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "ai-relay", "version": "0"},
            "clientCapabilities": {
                "auth": {"terminal": False},
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
        })
        self._initialized = True
        
        res = await self._request("session/new", {
            "cwd": self.cwd,
            "mcpServers": [],
        })
        result = self._result_or_raise(res, "session/new")
        self._acp_session_id = result.get("sessionId")

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
                "modelId": msg.get("model")
            } if self._acp_session_id else {"modelId": msg.get("model")})
            return
        if msg_type == "permission_response":
            request_id = msg.get("request_id")
            if request_id:
                behavior = msg.get("behavior", "allow")
                option_id = msg.get("optionId") or msg.get("option_id")
                if not option_id:
                    option_id = "allow_once" if behavior in {"allow", "approve"} else "reject_once"
                payload = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": option_id,
                        }
                    }
                }
                await self.transport.write_json_line(json.dumps(payload))
            return

        content = msg.get("content")
        if content is None:
            content = msg.get("text", "")
        if content == "" or content is None:
            return
            
        # Format prompt as array of parts
        prompt_parts = []
        if isinstance(content, str):
            prompt_parts = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    prompt_parts.append({"type": "text", "text": str(block)})
                    continue
                
                b_type = block.get("type")
                if b_type == "text":
                    prompt_parts.append({"type": "text", "text": block.get("text", "")})
                elif b_type == "image":
                    # Translate Anthropic-style image block to Google-style
                    source = block.get("source", {})
                    prompt_parts.append({
                        "type": "image",
                        "mimeType": source.get("media_type", "image/png"),
                        "data": source.get("data", ""),
                    })
                elif b_type in {"inline_data", "image"}:
                    prompt_parts.append(self._normalize_content_block(block))
                else:
                    prompt_parts.append({"type": "text", "text": json.dumps(block)})
        else:
            prompt_parts = [{"type": "text", "text": str(content)}]

        # Handle top-level images field if present
        images = msg.get("images")
        if isinstance(images, list):
            for img in images:
                if isinstance(img, dict):
                    prompt_parts.append(self._normalize_content_block({
                        "type": "image",
                        "mimeType": img.get("mime_type") or img.get("mimeType") or "image/png",
                        "data": img.get("data") or img.get("base64", ""),
                    }))

        # Send prompt
        params = {
            "prompt": prompt_parts,
            "messageId": str(uuid.uuid4()),
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
        try:
            return await asyncio.wait_for(future, timeout=60)
        except asyncio.TimeoutError as exc:
            self._pending_requests.pop(req_id, None)
            raise RuntimeError(f"Timed out waiting for Gemini ACP {method}") from exc

    @staticmethod
    def _result_or_raise(response: Any, method: str) -> dict[str, Any]:
        if isinstance(response, dict) and isinstance(response.get("error"), dict):
            error = response["error"]
            raise RuntimeError(f"Gemini ACP {method} failed: {error.get('message') or error}")
        result = response.get("result") if isinstance(response, dict) else None
        if isinstance(result, dict):
            return result
        return {}

    @staticmethod
    def _normalize_content_block(block: dict[str, Any]) -> dict[str, Any]:
        if block.get("type") == "inline_data":
            return {
                "type": "image",
                "mimeType": block.get("mime_type") or block.get("mimeType") or "image/png",
                "data": block.get("data", ""),
            }
        if block.get("type") == "image":
            return {
                "type": "image",
                "mimeType": block.get("mimeType") or block.get("mime_type") or "image/png",
                "data": block.get("data", ""),
            }
        return block

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
                    elif method == "session/request_permission":
                        tool_call = params.get("toolCall", {}) if isinstance(params, dict) else {}
                        await self._event_queue.put(RelayEvent(
                            type=EventType.PERMISSION_REQUEST,
                            session_id=self.session_id,
                            request_id=msg.get("id"),
                            tool=tool_call.get("title") or tool_call.get("toolCallId"),
                            args={"options": params.get("options", []), "toolCall": tool_call},
                            text=tool_call.get("title"),
                            raw=msg,
                        ))
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
                        future = self._pending_requests.pop(req_id)
                        if not future.done():
                            future.set_result(msg)
                    
                    # If it's a result of session/prompt, we might want to emit it
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
            for future in self._pending_requests.values():
                if not future.done():
                    future.set_exception(e)
            self._pending_requests.clear()
            await self._event_queue.put(RelayEvent(type=EventType.ERROR, session_id=self.session_id, text=f"Reader error: {e}"))
        finally:
            for future in self._pending_requests.values():
                if not future.done():
                    future.set_exception(RuntimeError("Gemini ACP process exited"))
            self._pending_requests.clear()
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
                text=update.get("title") or update.get("toolName"),
                raw=update,
            ))
        elif kind == "tool_result":
            events.append(RelayEvent(
                type=EventType.TOOL_RESULT,
                session_id=self.session_id,
                tool_use_id=update.get("toolCallId"),
                content=update.get("content"),
                raw=update,
            ))
        elif kind == "agent_response":
            content = update.get("content", {})
            text = content.get("text", "")
            events.append(RelayEvent(
                type=EventType.RESPONSE,
                session_id=self.session_id,
                text=text,
                raw=update,
            ))
        elif kind == "agent_error":
            events.append(RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=update.get("message"),
                raw=update,
            ))
        elif kind == "status":
            events.append(RelayEvent(
                type=EventType.STATUS,
                session_id=self.session_id,
                status=update.get("status"),
                text=update.get("message"),
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
            config=config,
        )
