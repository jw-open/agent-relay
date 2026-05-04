"""Core relay — spawns a subprocess and bridges it to a WebSocket connection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.server import WebSocketServerProtocol

from .adapters import get_adapter, BaseAdapter
from .adapters.base import AgentRuntime
from .adapters.claude_code import ClaudeStructuredRuntime
from .adapters.gemini import GeminiStructuredRuntime
from .events import EventType, RelayEvent
from .per_turn import PerTurnRuntime

# Tools whose CLIs exit after each turn and need per-turn subprocess restarts.
_PER_TURN_TOOLS: dict[str, tuple[type[AgentRuntime], str]] = {
    "claude":      (ClaudeStructuredRuntime, "--resume"),
    "claude-code": (ClaudeStructuredRuntime, "--resume"),
}

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
        config: Optional[dict[str, Any]] = None,
    ):
        self.session_id = session_id
        self.tool = tool
        self.folder = os.path.abspath(folder)
        self.model = model
        self.extra_args = extra_args or []
        self.config = config or {}
        self._adapter: type[BaseAdapter] = get_adapter(tool)
        self._runtime: Optional[AgentRuntime] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, ws: WebSocketServerProtocol) -> None:
        """Spawn the subprocess and relay I/O until the process exits or WS closes."""
        cmd = self._adapter.build_command(self.folder, self.model, self.extra_args)
        logger.info("[%s] Starting: %s in %s", self.session_id, cmd, self.folder)

        env = self._build_env(cmd[0]) if cmd else self._build_env("")
        
        # Merge handshake-provided environment variables
        handshake_env = self.config.get("env")
        if isinstance(handshake_env, dict):
            env.update({str(k): str(v) for k, v in handshake_env.items()})
            
        # Dynamically ingest OAuth credentials from handshake if present
        # This matches the "Codex way" of injecting settings into the environment.
        if "oauth_client_id" in self.config:
            client_id = str(self.config["oauth_client_id"])
            env.setdefault("CLAUDE_OAUTH_CLIENT_ID", client_id)
            env.setdefault("GEMINI_OAUTH_CLIENT_ID", client_id)
            
        if "oauth_client_secret" in self.config:
            client_secret = str(self.config["oauth_client_secret"])
            env.setdefault("GEMINI_OAUTH_CLIENT_SECRET", client_secret)

        logger.debug("[%s] PATH: %s", self.session_id, env.get("PATH", "(not set)"))
        if cmd:
            logger.debug("[%s] resolved binary: %s", self.session_id, shutil.which(cmd[0], path=env.get("PATH")))

        error = self._preflight(cmd, env)
        if error:
            await self._send(ws, RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=error,
            ))
            return

        await self._send(ws, RelayEvent(
            type=EventType.SESSION_START,
            session_id=self.session_id,
            metadata={"tool": self.tool, "folder": self.folder, "model": self.model, "cmd": cmd},
        ))

        try:
            tool_lower = self.tool.lower()
            per_turn_info = _PER_TURN_TOOLS.get(tool_lower)
            if per_turn_info:
                runtime_class, resume_flag = per_turn_info
                self._runtime = PerTurnRuntime(
                    tool=self.tool,
                    session_id=self.session_id,
                    cmd=cmd,
                    cwd=self.folder,
                    env=env,
                    runtime_class=runtime_class,
                    resume_flag=resume_flag,
                    config=self.config,
                )
            else:
                self._runtime = self._adapter.create_runtime(
                    session_id=self.session_id,
                    folder=self.folder,
                    model=self.model,
                    extra_args=self.extra_args,
                    env=env,
                    config=self.config,
                )
            await self._runtime.start()
        except FileNotFoundError:
            await self._send(ws, RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=f"Command not found: {cmd[0]}. Is it installed and on PATH?",
            ))
            return
        except Exception as exc:
            logger.exception("[%s] Failed to start %s runtime", self.session_id, self.tool)
            await self._send(ws, RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=str(exc) or f"Failed to start {self.tool} runtime",
            ))
            return

        # Run the process/API reader and WS input pump concurrently. Some
        # structured adapters are persistent and only emit after client input,
        # so either side ending should tear down the session.
        ws_task = asyncio.create_task(self._read_ws(ws))
        runtime_task = asyncio.create_task(self._read_runtime(ws))
        try:
            done, pending = await asyncio.wait(
                {ws_task, runtime_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
        finally:
            for task in (ws_task, runtime_task):
                task.cancel()
            await asyncio.gather(ws_task, runtime_task, return_exceptions=True)
            await self.stop()

        if self._runtime:
            exit_code = await self._runtime.wait()
        else:
            exit_code = None
        await self._send(ws, RelayEvent(
            type=EventType.SESSION_END,
            session_id=self.session_id,
            exit_code=exit_code,
        ))
        logger.info("[%s] Process exited with code %s", self.session_id, exit_code)

    async def stop(self) -> None:
        if self._runtime:
            await self._runtime.stop()

    def _build_env(self, binary: str) -> dict[str, str]:
        """Copy os.environ and auto-expand PATH with nvm/node bin dirs if binary not found."""
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")

        if not binary:
            return env

        # If binary is already findable, nothing to do.
        if shutil.which(binary, path=env.get("PATH")) is not None:
            return env

        # Auto-detect: scan ~/.nvm/versions/node/*/bin for the binary.
        nvm_root = os.path.expanduser("~/.nvm/versions/node")
        extra: list[str] = []
        if os.path.isdir(nvm_root):
            try:
                for version_dir in sorted(os.listdir(nvm_root), reverse=True):
                    bin_dir = os.path.join(nvm_root, version_dir, "bin")
                    if os.path.isfile(os.path.join(bin_dir, binary)):
                        extra.append(bin_dir)
                        logger.debug("auto-detected nvm bin dir for %s: %s", binary, bin_dir)
                        break
            except OSError:
                pass

        # Also check ~/.local/bin, /usr/local/bin as fallbacks.
        for d in [os.path.expanduser("~/.local/bin"), "/usr/local/bin"]:
            if os.path.isfile(os.path.join(d, binary)) and d not in extra:
                extra.append(d)

        if extra:
            current = env.get("PATH", "")
            env["PATH"] = ":".join(extra + ([current] if current else []))
            logger.debug("expanded PATH with: %s", extra)

        return env

    def _preflight(self, cmd: list[str], env: dict[str, str]) -> Optional[str]:
        if not os.path.isdir(self.folder):
            return f"Working folder not found: {self.folder}"
        if not cmd and self._adapter.requires_executable:
            return "Adapter produced an empty command."
        if cmd and self._adapter.requires_executable and shutil.which(cmd[0], path=env.get("PATH")) is None:
            return (
                f"Command not found: {cmd[0]}. Is it installed and on PATH?\n"
                f"PATH searched: {env.get('PATH', '(not set)')}"
            )
        return None

    # ── I/O pumps ─────────────────────────────────────────────────────────────

    async def _read_runtime(self, ws: WebSocketServerProtocol) -> None:
        while True:
            if not self._runtime:
                break
            event = await self._runtime.read_event()
            if not event:
                logger.debug("[%s] runtime EOF", self.session_id)
                break
            if event.text == "" and event.raw is None and event.content is None:
                continue
            logger.debug("[%s] sending event: %s", self.session_id, event.to_json()[:200])
            await self._send(ws, event)

    async def _read_ws(self, ws: WebSocketServerProtocol) -> None:
        """Forward WebSocket messages to the subprocess stdin."""
        async for raw in ws:
            logger.debug("[%s] WS recv: %r", self.session_id, raw[:200] if isinstance(raw, str) else raw)
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else {}
                # json.loads may return a non-dict (int, list…) for bare values like "1"
                msg = parsed if isinstance(parsed, dict) else {"text": raw}
            except json.JSONDecodeError:
                msg = {"text": raw}

            text = msg.get("text", "")
            if not text and "type" not in msg and "content" not in msg:
                continue

            logger.debug("[%s] WS -> runtime: %r", self.session_id, msg)
            if self._runtime:
                await self._runtime.handle_client_message(msg)
                await self._send(ws, RelayEvent(
                    type=EventType.INPUT_ACK,
                    session_id=self.session_id,
                    text=text or msg.get("type"),
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
            config = await self._recv_handshake(ws)
        except ConnectionClosed:
            logger.debug("Client disconnected before sending handshake")
            return
        except asyncio.TimeoutError:
            await self._send_error(ws, "Handshake timeout: first message must be session config JSON")
            return
        except ValueError as exc:
            await self._send_error(ws, f"Invalid handshake: {exc}")
            return
        except Exception as exc:
            logger.exception("Unexpected handshake failure")
            await self._send_error(ws, f"Invalid handshake: {exc}")
            return

        session = RelaySession(
            session_id=config.get("session_id") or str(uuid.uuid4()),
            tool=config.get("tool", "claude"),
            folder=config.get("folder", "."),
            model=config.get("model"),
            extra_args=config.get("extra_args"),
            config=config,
        )
        try:
            await session.start(ws)
        finally:
            await session.stop()

    async def _recv_handshake(self, ws: WebSocketServerProtocol) -> dict[str, Any]:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        if not isinstance(raw, str):
            raise ValueError("first message must be a JSON text frame")

        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(exc.msg) from exc

        if not isinstance(config, dict):
            raise ValueError("handshake must be a JSON object")

        extra_args = config.get("extra_args")
        if extra_args is not None and not isinstance(extra_args, list):
            raise ValueError("extra_args must be a list of strings")
        if extra_args is not None and not all(isinstance(arg, str) for arg in extra_args):
            raise ValueError("extra_args must be a list of strings")

        return config

    async def _send_error(self, ws: WebSocketServerProtocol, text: str) -> None:
        await ws.send(RelayEvent(
            type=EventType.ERROR,
            session_id="",
            text=text,
        ).to_json())

    async def serve(self) -> None:
        logger.info("ai-relay listening on ws://%s:%d", self.host, self.port)
        # max_size raised to 50MB to support image attachment payloads (base64-encoded).
        # The default 1MB limit causes WebSocket 1009 errors for images > ~750KB.
        async with websockets.serve(self.handle, self.host, self.port, max_size=50 * 1024 * 1024):
            await asyncio.Future()  # run forever

    def run(self) -> None:
        asyncio.run(self.serve())
