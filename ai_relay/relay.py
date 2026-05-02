"""Core relay — spawns a subprocess and bridges it to a WebSocket connection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Optional

import websockets
from websockets.server import WebSocketServerProtocol

from .adapters import get_adapter, BaseAdapter
from .events import EventType, RelayEvent
from .pty_session import PtySession, clean_pty_output

logger = logging.getLogger(__name__)


class RelaySession:
    """
    Manages one coding-agent subprocess and one WebSocket connection.
    Streams subprocess output as structured RelayEvents to the client,
    and forwards client messages as stdin to the subprocess.
    """

    def __init__(
        self,
        session_id: str,
        tool: str,
        folder: str,
        model: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
    ):
        self.session_id = session_id
        self.tool = tool
        self.folder = os.path.abspath(folder)
        self.model = model
        self.extra_args = extra_args or []
        self._adapter: type[BaseAdapter] = get_adapter(tool)
        self._pty: Optional[PtySession] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, ws: WebSocketServerProtocol) -> None:
        """Spawn the subprocess and relay I/O until the process exits or WS closes."""
        cmd = self._adapter.build_command(self.folder, self.model, self.extra_args)
        logger.info("[%s] Starting: %s in %s", self.session_id, cmd, self.folder)

        await self._send(ws, RelayEvent(
            type=EventType.SESSION_START,
            session_id=self.session_id,
            metadata={"tool": self.tool, "folder": self.folder, "model": self.model, "cmd": cmd},
        ))

        try:
            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")
            self._pty = PtySession(
                cmd=cmd,
                cwd=self.folder,
                env=env,
                session_id=self.session_id,
                auto_confirm_delay=0,
            )
            await self._pty.start()
        except FileNotFoundError:
            await self._send(ws, RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=f"Command not found: {cmd[0]}. Is it installed and on PATH?",
            ))
            return

        # Run the PTY reader and WS input pump concurrently.
        # PTYs combine stdout/stderr and often emit screen redraw chunks without newlines.
        ws_task = asyncio.create_task(self._read_ws(ws))
        try:
            await self._read_pty(ws)
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass

        if self._pty and self._pty._process:
            exit_code = await self._pty._process.wait()
        else:
            exit_code = None
        await self._send(ws, RelayEvent(
            type=EventType.SESSION_END,
            session_id=self.session_id,
            exit_code=exit_code,
        ))
        logger.info("[%s] Process exited with code %s", self.session_id, exit_code)

    async def stop(self) -> None:
        if self._pty:
            await self._pty.stop()

    # ── I/O pumps ─────────────────────────────────────────────────────────────

    async def _read_pty(self, ws: WebSocketServerProtocol) -> None:
        while True:
            if not self._pty:
                break
            chunk = await self._pty.read()
            if not chunk:
                break
            decoded = self._adapter.postprocess_line(clean_pty_output(chunk))
            if not decoded:
                continue
            event = RelayEvent.from_raw(self.session_id, "stdout", decoded)
            await self._send(ws, event)

    async def _read_ws(self, ws: WebSocketServerProtocol) -> None:
        """Forward WebSocket messages to the subprocess stdin."""
        async for raw in ws:
            try:
                msg = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                msg = {"text": raw}

            text = msg.get("text", "")
            if not text:
                continue

            processed = self._adapter.preprocess_input(text)
            if self._pty:
                await self._pty.write(processed.encode())
                await self._send(ws, RelayEvent(
                    type=EventType.INPUT_ACK,
                    session_id=self.session_id,
                    text=text,
                ))

    @staticmethod
    async def _send(ws: WebSocketServerProtocol, event: RelayEvent) -> None:
        try:
            await ws.send(event.to_json())
        except Exception:
            pass


class RelayServer:
    """
    WebSocket server that accepts connections and spawns a RelaySession per client.

    Protocol (client → server on connect):
        { "tool": "claude", "folder": "/path/to/project", "model": "sonnet" }
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port

    async def handle(self, ws: WebSocketServerProtocol) -> None:
        # First message must be the session config
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            config = json.loads(raw)
        except Exception as exc:
            await ws.send(RelayEvent(
                type=EventType.ERROR,
                session_id="",
                text=f"Invalid handshake: {exc}",
            ).to_json())
            return

        session = RelaySession(
            session_id=config.get("session_id") or str(uuid.uuid4()),
            tool=config.get("tool", "claude"),
            folder=config.get("folder", "."),
            model=config.get("model"),
            extra_args=config.get("extra_args"),
        )
        try:
            await session.start(ws)
        finally:
            await session.stop()

    async def serve(self) -> None:
        logger.info("ai-relay listening on ws://%s:%d", self.host, self.port)
        async with websockets.serve(self.handle, self.host, self.port):
            await asyncio.Future()  # run forever

    def run(self) -> None:
        asyncio.run(self.serve())
