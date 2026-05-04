"""OpenAI Codex CLI app-server adapter."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from ..events import EventType, RelayEvent
from ..transports import StructuredProcessTransport
from .base import AgentRuntime, BaseAdapter


class CodexAppServerRuntime(AgentRuntime):
    """Codex runtime over `codex app-server --listen stdio://`."""

    def __init__(
        self,
        session_id: str,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        model: Optional[str],
        config: Optional[dict[str, Any]],
    ):
        super().__init__(session_id)
        self.transport = StructuredProcessTransport(cmd, cwd, env)
        self.cwd = cwd
        self.model = model
        self.config = config or {}
        self._queue: asyncio.Queue[Optional[RelayEvent]] = asyncio.Queue()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._server_requests: dict[str, dict[str, Any]] = {}
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._thread_id: Optional[str] = None
        self._turn_id: Optional[str] = None
        self._approval_policy = self.config.get("approvalPolicy") or self.config.get("approval_policy")
        self._sandbox = self.config.get("sandbox")

    async def start(self) -> None:
        from ..codex_auth import ensure_codex_auth
        ensure_codex_auth(self.transport.env)
        await self.transport.start()
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        await self._initialize()
        await self._start_thread()

    async def read_event(self) -> Optional[RelayEvent]:
        return await self._queue.get()

    async def handle_client_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "interrupt":
            await self._interrupt()
            return
        if msg_type == "permission_response":
            await self._handle_permission_response(msg)
            return
        if msg_type == "control_response":
            await self._send_control_response(msg)
            return
        if msg_type == "set_model":
            self.model = msg.get("model") or self.model
            return
        if msg_type in {"set_permission_mode", "set_approval_policy"}:
            self._approval_policy = msg.get("mode") or msg.get("approvalPolicy") or msg.get("approval_policy")
            return
        if msg_type == "raw_request":
            await self._send_request(msg["method"], msg.get("params"))
            return

        text = msg.get("text") or msg.get("content")
        if text:
            await self._start_turn(str(text), msg)

    async def stop(self) -> None:
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        await self.transport.stop()
        await self._queue.put(None)

    async def wait(self) -> Optional[int]:
        return await self.transport.wait()

    async def _initialize(self) -> None:
        await self._send_request(
            "initialize",
            {
                "clientInfo": {
                    "name": "ai-relay",
                    "title": "AI Relay",
                    "version": "0.0.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                },
            },
        )
        await self._send_notification("initialized")

    async def _start_thread(self) -> None:
        params: dict[str, Any] = {
            "cwd": self.cwd,
            "ephemeral": self.config.get("ephemeral", True),
            "serviceName": self.config.get("serviceName", "ai-relay"),
        }
        if self.model:
            params["model"] = self.model
        if self._approval_policy:
            params["approvalPolicy"] = self._approval_policy
        if self._sandbox:
            params["sandbox"] = self._sandbox
        for key in (
            "modelProvider",
            "serviceTier",
            "approvalsReviewer",
            "config",
            "baseInstructions",
            "developerInstructions",
            "personality",
        ):
            if key in self.config:
                params[key] = self.config[key]

        result = await self._send_request("thread/start", params)
        thread = result.get("thread") if isinstance(result, dict) else None
        self._thread_id = self._extract_id(thread) or self._extract_id(result)
        await self._queue.put(
            RelayEvent(
                type=EventType.STATUS,
                session_id=self.session_id,
                status="thread_started",
                metadata={"thread_id": self._thread_id, "result": result},
            )
        )

    async def _start_turn(self, text: str, msg: dict[str, Any]) -> None:
        if not self._thread_id:
            await self._queue.put(
                RelayEvent(type=EventType.ERROR, session_id=self.session_id, text="Codex thread is not ready")
            )
            return

        params: dict[str, Any] = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
        }
        model = msg.get("model") or self.model
        if model:
            params["model"] = model
        approval_policy = msg.get("approvalPolicy") or msg.get("approval_policy") or self._approval_policy
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        sandbox_policy = msg.get("sandboxPolicy") or msg.get("sandbox_policy")
        if sandbox_policy:
            params["sandboxPolicy"] = sandbox_policy
        for source_key, target_key in (
            ("cwd", "cwd"),
            ("effort", "effort"),
            ("summary", "summary"),
            ("serviceTier", "serviceTier"),
            ("personality", "personality"),
            ("outputSchema", "outputSchema"),
        ):
            if source_key in msg:
                params[target_key] = msg[source_key]

        result = await self._send_request("turn/start", params)
        turn = result.get("turn") if isinstance(result, dict) else None
        self._turn_id = self._extract_id(turn) or self._turn_id
        await self._queue.put(
            RelayEvent(
                type=EventType.INPUT_ACK,
                session_id=self.session_id,
                text=text,
                metadata={"turn_id": self._turn_id, "result": result},
            )
        )

    async def _interrupt(self) -> None:
        if not self._thread_id or not self._turn_id:
            await self._queue.put(
                RelayEvent(type=EventType.STATUS, session_id=self.session_id, status="no_active_turn")
            )
            return
        await self._send_request("turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id})
        await self._queue.put(
            RelayEvent(
                type=EventType.STATUS,
                session_id=self.session_id,
                status="interrupted",
                metadata={"thread_id": self._thread_id, "turn_id": self._turn_id},
            )
        )

    async def _handle_permission_response(self, msg: dict[str, Any]) -> None:
        request_id = str(msg.get("request_id") or msg.get("id") or "")
        if not request_id:
            return
        request = self._server_requests.pop(request_id, {})
        method = str(request.get("method", ""))
        response = msg.get("response")
        if response is None:
            allow = bool(msg.get("allow") or msg.get("approved"))
            decision = msg.get("decision") or ("accept" if allow else "decline")
            if method == "item/fileChange/requestApproval":
                response = {"decision": "accept" if decision in {"accept", "approved", "approved_for_session"} else "decline"}
            elif method == "item/permissions/requestApproval":
                response = msg.get("permissions") or {"permissions": {}, "scope": "turn"}
            elif method in {"execCommandApproval", "applyPatchApproval"}:
                legacy = "approved" if decision in {"accept", "approved"} else decision
                response = {"decision": legacy}
            else:
                response = {"decision": decision}
        await self._send_response(request_id, response)

    async def _send_control_response(self, msg: dict[str, Any]) -> None:
        request_id = str(msg.get("request_id") or msg.get("id") or "")
        if request_id:
            await self._send_response(request_id, msg.get("result", msg.get("response")))

    async def _send_request(self, method: str, params: Any = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        payload = {"id": request_id, "method": method, "params": params}
        await self.transport.write_json_line(json.dumps(payload))
        return await future

    async def _send_notification(self, method: str, params: Any = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self.transport.write_json_line(json.dumps(payload))

    async def _send_response(self, request_id: str, result: Any) -> None:
        ident: int | str = int(request_id) if request_id.isdigit() else request_id
        await self.transport.write_json_line(json.dumps({"id": ident, "result": result}))

    async def _read_stdout(self) -> None:
        try:
            while True:
                line = await self.transport.readline()
                if line is None:
                    await self._queue.put(None)
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    await self._queue.put(
                        RelayEvent(type=EventType.STDOUT, session_id=self.session_id, text=text)
                    )
                    continue
                await self._handle_payload(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._queue.put(RelayEvent(type=EventType.ERROR, session_id=self.session_id, text=str(exc)))

    async def _read_stderr(self) -> None:
        try:
            while True:
                line = await self.transport.read_stderr()
                if line is None:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    await self._queue.put(RelayEvent(type=EventType.STDERR, session_id=self.session_id, text=text))
        except asyncio.CancelledError:
            raise

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        ident = payload.get("id")
        if ident is not None and ("result" in payload or "error" in payload) and "method" not in payload:
            future = self._pending.pop(ident, None)
            if future is None and isinstance(ident, str) and ident.isdigit():
                future = self._pending.pop(int(ident), None)
            if future and not future.done():
                if "error" in payload:
                    future.set_exception(RuntimeError(json.dumps(payload["error"])))
                else:
                    future.set_result(payload.get("result") or {})
            return

        method = payload.get("method")
        params = payload.get("params") or {}
        if ident is not None and method:
            self._server_requests[str(ident)] = payload
            await self._queue.put(self._server_request_event(str(ident), method, params, payload))
            return

        if method:
            for event in self._notification_events(method, params, payload):
                await self._queue.put(event)
            return

        await self._queue.put(
            RelayEvent(type=EventType.STREAM_EVENT, session_id=self.session_id, raw=payload, content=payload)
        )

    def _server_request_event(
        self,
        request_id: str,
        method: str,
        params: dict[str, Any],
        raw: dict[str, Any],
    ) -> RelayEvent:
        event_type = EventType.PERMISSION_REQUEST
        if method == "item/tool/call":
            event_type = EventType.TOOL_CALL
        return RelayEvent(
            type=event_type,
            session_id=self.session_id,
            request_id=request_id,
            tool=self._request_tool(method, params),
            tool_use_id=params.get("itemId"),
            text=params.get("reason") or params.get("command") or method,
            args=params,
            raw=raw,
            metadata={"method": method},
        )

    def _notification_events(
        self,
        method: str,
        params: dict[str, Any],
        raw: dict[str, Any],
    ) -> list[RelayEvent]:
        base = {"session_id": self.session_id, "raw": raw, "metadata": {"method": method}}
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn") or {}
            self._turn_id = self._extract_id(turn) or self._turn_id
            return [
                RelayEvent(
                    type=EventType.STATUS,
                    status=method.split("/")[-1],
                    metadata={**base["metadata"], "turn": turn},
                    **{k: v for k, v in base.items() if k != "metadata"},
                )
            ]
        if method in {"thread/started", "thread/status/changed", "thread/tokenUsage/updated"}:
            return [RelayEvent(type=EventType.STATUS, status=method, content=params, **base)]
        if method == "thread/compacted":
            return [RelayEvent(type=EventType.CONTEXT_COMPACTED, content=params, **base)]
        if method == "item/agentMessage/delta":
            return [RelayEvent(type=EventType.RESPONSE, text=str(params.get("delta", "")), **base)]
        if method in {"item/reasoning/textDelta", "item/reasoning/summaryTextDelta", "item/plan/delta"}:
            return [RelayEvent(type=EventType.REASONING, text=str(params.get("delta", "")), **base)]
        if method in {"command/exec/outputDelta", "item/commandExecution/outputDelta"}:
            return [
                RelayEvent(
                    type=EventType.TOOL_PROGRESS,
                    tool="commandExecution",
                    tool_use_id=params.get("itemId") or params.get("callId"),
                    text=str(params.get("delta", "")),
                    **base,
                )
            ]
        if method in {"item/fileChange/outputDelta", "item/fileChange/patchUpdated", "turn/diff/updated"}:
            return [RelayEvent(type=EventType.FILE_DIFF, content=params, diff=params.get("delta"), **base)]
        if method == "item/mcpToolCall/progress":
            return [
                RelayEvent(
                    type=EventType.TOOL_PROGRESS,
                    tool=params.get("tool"),
                    tool_use_id=params.get("itemId"),
                    content=params,
                    **base,
                )
            ]
        if method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            event = self._item_event(item, method, raw)
            return [event] if event else [RelayEvent(type=EventType.STREAM_EVENT, content=params, **base)]
        if method == "rawResponseItem/completed":
            return self._raw_response_item_events(params, raw)
        if method in {"warning", "guardianWarning", "configWarning", "deprecationNotice"}:
            return [RelayEvent(type=EventType.CONTEXT_WARNING, text=str(params.get("message") or params), **base)]
        if method == "error":
            return [RelayEvent(type=EventType.ERROR, text=str(params.get("message") or params), **base)]
        return [RelayEvent(type=EventType.STREAM_EVENT, content=params, **base)]

    def _item_event(self, item: dict[str, Any], method: str, raw: dict[str, Any]) -> Optional[RelayEvent]:
        item_type = item.get("type")
        status = "started" if method == "item/started" else "completed"
        common = {
            "session_id": self.session_id,
            "tool_use_id": item.get("id"),
            "raw": raw,
            "status": item.get("status") or status,
            "metadata": {"method": method, "item_type": item_type},
        }
        if item_type == "agentMessage":
            return RelayEvent(type=EventType.ASSISTANT_MESSAGE, text=item.get("text"), **common)
        if item_type in {"reasoning", "plan"}:
            return RelayEvent(type=EventType.REASONING, text=item.get("text"), content=item, **common)
        if item_type == "commandExecution":
            if status == "started":
                return RelayEvent(
                    type=EventType.TOOL_CALL,
                    tool="commandExecution",
                    text=item.get("command"),
                    args={"command": item.get("command"), "cwd": item.get("cwd")},
                    **common,
                )
            return RelayEvent(
                type=EventType.TOOL_RESULT,
                tool="commandExecution",
                text=item.get("aggregatedOutput"),
                exit_code=item.get("exitCode"),
                content=item,
                **common,
            )
        if item_type == "fileChange":
            return RelayEvent(type=EventType.FILE_DIFF, content=item.get("changes") or item, **common)
        if item_type in {"mcpToolCall", "dynamicToolCall", "collabAgentToolCall", "webSearch"}:
            event_type = EventType.TOOL_CALL if status == "started" else EventType.TOOL_RESULT
            return RelayEvent(
                type=event_type,
                tool=item.get("tool") or item_type,
                args=item.get("arguments") if isinstance(item.get("arguments"), dict) else None,
                content=item,
                **common,
            )
        if item_type == "contextCompaction":
            return RelayEvent(type=EventType.CONTEXT_COMPACTED, content=item, **common)
        return None

    def _raw_response_item_events(self, params: dict[str, Any], raw: dict[str, Any]) -> list[RelayEvent]:
        item = params.get("item") or {}
        item_type = item.get("type")
        if item_type == "message":
            text = "".join(
                part.get("text", "")
                for part in item.get("content", [])
                if isinstance(part, dict) and part.get("type") in {"output_text", "input_text"}
            )
            return [
                RelayEvent(
                    type=EventType.ASSISTANT_MESSAGE if item.get("role") == "assistant" else EventType.USER_MESSAGE,
                    session_id=self.session_id,
                    text=text,
                    content=item,
                    raw=raw,
                    metadata={"method": "rawResponseItem/completed"},
                )
            ]
        if item_type == "reasoning":
            return [
                RelayEvent(
                    type=EventType.REASONING,
                    session_id=self.session_id,
                    content=item,
                    raw=raw,
                    metadata={"method": "rawResponseItem/completed"},
                )
            ]
        return [
            RelayEvent(
                type=EventType.STREAM_EVENT,
                session_id=self.session_id,
                content=params,
                raw=raw,
                metadata={"method": "rawResponseItem/completed"},
            )
        ]

    @staticmethod
    def _extract_id(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            ident = value.get("id") or value.get("threadId") or value.get("turnId")
            return str(ident) if ident is not None else None
        return None

    @staticmethod
    def _request_tool(method: str, params: dict[str, Any]) -> str:
        if method == "item/commandExecution/requestApproval":
            return "commandExecution"
        if method == "item/fileChange/requestApproval":
            return "fileChange"
        if method == "item/permissions/requestApproval":
            return "permissions"
        if method == "item/tool/call":
            return str(params.get("tool") or "dynamicTool")
        if method == "execCommandApproval":
            return "execCommand"
        if method == "applyPatchApproval":
            return "applyPatch"
        return method


class CodexAdapter(BaseAdapter):
    tool_name = "codex"
    protocol = "json-rpc"

    @classmethod
    def build_command(cls, folder: str, model: Optional[str] = None,
                      extra_args: Optional[list[str]] = None) -> list[str]:
        cmd = ["codex", "app-server", "--listen", "stdio://"]
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
        return CodexAppServerRuntime(
            session_id=session_id,
            cmd=cls.build_command(folder, model, extra_args),
            cwd=folder,
            env=env,
            model=model,
            config=config,
        )
