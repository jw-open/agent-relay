"""Core relay — spawns a subprocess and bridges it to a WebSocket connection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
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
from .transports import WS_MAX_SIZE

# Tools whose CLIs exit after each turn and need per-turn subprocess restarts.
_PER_TURN_TOOLS: dict[str, tuple[type[AgentRuntime], str]] = {
    "claude":      (ClaudeStructuredRuntime, "--resume"),
    "claude-code": (ClaudeStructuredRuntime, "--resume"),
}

logger = logging.getLogger(__name__)

# ── Handshake env-var allowlist ───────────────────────────────────────────────
# Client-supplied env vars (handshake "env" dict) are restricted to this set.
# Variables that influence the dynamic linker, Python import machinery, or shell
# initialization are blocked to prevent privilege escalation / code injection.

_SAFE_ENV_PREFIXES = (
    "AI_RELAY_",
    "CLAUDE_",
    "CODEX_",
    "GEMINI_",
    "GOOGLE_",
    "ANTHROPIC_",
    "OPENAI_",
    "AZURE_",
    "AWS_",
    "LAB_",
    "NODE_",
    "NPM_",
)

_SAFE_ENV_EXACT = frozenset({
    "HOME", "PATH", "TERM", "SHELL", "LANG", "LC_ALL",
    "XDG_DATA_HOME", "XDG_CONFIG_HOME",
})

_BLOCKED_ENV_EXACT = frozenset({
    # Dynamic-linker / loader injection vectors
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    # Python import path override
    "PYTHONPATH", "PYTHONSTARTUP",
    # Shell startup files
    "BASH_ENV", "ENV", "CDPATH",
})

# ── Extra-args validation ─────────────────────────────────────────────────────
# Block args that contain shell-special chars, null bytes, or look like paths.
# Callers that need arbitrary flags should run ai-relay programmatically rather
# than via the WebSocket protocol.
_SAFE_ARG_RE = re.compile(r'^[A-Za-z0-9_\-\.=,@:]+$')


def _filter_handshake_env(raw: dict[str, Any]) -> dict[str, str]:
    """Return a sanitised copy of the client-supplied env dict."""
    safe: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k)
        val = str(v)
        if key in _BLOCKED_ENV_EXACT:
            logger.warning("Blocked client env var %r (blocked list)", key)
            continue
        if key in _SAFE_ENV_EXACT or any(key.startswith(p) for p in _SAFE_ENV_PREFIXES):
            safe[key] = val
        else:
            logger.warning("Dropped client env var %r (not in allowlist)", key)
    return safe


def _validate_extra_args(args: list[str]) -> None:
    for arg in args:
        if not _SAFE_ARG_RE.match(arg):
            raise ValueError(
                f"extra_arg {arg!r} contains disallowed characters. "
                "Only alphanumerics, hyphens, underscores, dots, equals, commas, "
                "at-signs, and colons are permitted."
            )


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
        workspace_root: Optional[str] = None,
    ):
        self.session_id = session_id
        self.tool = tool
        self.folder = os.path.abspath(folder)
        self.model = model
        self.extra_args = extra_args or []
        self.config = config or {}
        self.workspace_root = os.path.abspath(workspace_root) if workspace_root else None
        self._adapter: type[BaseAdapter] = get_adapter(tool)
        self._runtime: Optional[AgentRuntime] = None
        self._mcp_tmp: Optional[str] = None  # temp MCP config file; cleaned up in stop()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, ws: WebSocketServerProtocol) -> None:
        """Spawn the subprocess and relay I/O until the process exits or WS closes."""
        cmd = self._adapter.build_command(self.folder, self.model, self.extra_args)
        logger.info("[%s] Starting: %s in %s", self.session_id, cmd, self.folder)

        env = self._build_env(cmd[0]) if cmd else self._build_env("")

        # Merge allowlisted client-supplied env vars from handshake config.
        handshake_env = self.config.get("env")
        if isinstance(handshake_env, dict):
            env.update(_filter_handshake_env(handshake_env))

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

            # MCP config: write a temp file and inject --mcp-config into cmd.
            # This applies to PerTurnRuntime tools (claude/claude-code) where cmd
            # is passed directly.  Other adapters handle mcp_config in create_runtime().
            mcp_config = self.config.get("mcp_config")
            if (per_turn_info and tool_lower in {"claude", "claude-code"}
                    and isinstance(mcp_config, dict) and mcp_config.get("mcpServers")):
                try:
                    fd, self._mcp_tmp = tempfile.mkstemp(suffix=".json", prefix="ai_relay_mcp_")
                    with os.fdopen(fd, "w") as f:
                        json.dump(mcp_config, f)
                    cmd = cmd + ["--mcp-config", self._mcp_tmp]
                    logger.debug("[%s] MCP config written to %s", self.session_id, self._mcp_tmp)
                except OSError as exc:
                    logger.warning("[%s] Failed to write MCP config: %s", self.session_id, exc)
                    self._mcp_tmp = None

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
        if self._mcp_tmp:
            try:
                os.unlink(self._mcp_tmp)
            except OSError:
                pass
            self._mcp_tmp = None

    def _build_env(self, binary: str) -> dict[str, str]:
        """Copy os.environ and auto-expand PATH with nvm/node bin dirs if binary not found."""
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")

        if not binary:
            return env

        if shutil.which(binary, path=env.get("PATH")) is not None:
            return env

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
        if self.workspace_root and not self.folder.startswith(self.workspace_root + os.sep):
            return (
                f"Folder {self.folder!r} is outside the permitted workspace root "
                f"{self.workspace_root!r}."
            )
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
        except ConnectionClosed:
            pass  # client disconnected — normal teardown path
        except Exception:
            logger.warning("WebSocket send failed for event type %r", event.type, exc_info=True)


class RelayServer:
    """
    WebSocket server that accepts connections and spawns a RelaySession per client.

    Protocol (client → server on connect):
        {
          "tool": "claude",
          "folder": "/path/to/project",
          "model": "sonnet",
          "session_id": "<optional uuid>",
          "extra_args": ["--flag"],        # optional, restricted allowlist
          "env": {"KEY": "value"},         # optional, allowlisted vars only
        }

    Security notes:
      - ``env`` keys are filtered against an allowlist; loader-injection vars
        (LD_PRELOAD, PYTHONPATH, etc.) are silently dropped.
      - ``extra_args`` values must match ``[A-Za-z0-9_\\-.=,@:]+``.
      - If *workspace_root* is set on the server, ``folder`` must be inside it.
      - OAuth/API secrets must come from server-side env vars, not the handshake.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        workspace_root: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.workspace_root = workspace_root

    async def handle(self, ws: WebSocketServerProtocol) -> None:
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
            workspace_root=self.workspace_root,
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
        if extra_args is not None:
            if not isinstance(extra_args, list):
                raise ValueError("extra_args must be a list of strings")
            if not all(isinstance(arg, str) for arg in extra_args):
                raise ValueError("extra_args must be a list of strings")
            _validate_extra_args(extra_args)

        return config

    async def _send_error(self, ws: WebSocketServerProtocol, text: str) -> None:
        try:
            await ws.send(RelayEvent(
                type=EventType.ERROR,
                session_id="",
                text=text,
            ).to_json())
        except Exception:
            pass

    async def serve(self) -> None:
        logger.info("ai-relay listening on ws://%s:%d", self.host, self.port)
        async with websockets.serve(self.handle, self.host, self.port, max_size=WS_MAX_SIZE):
            await asyncio.Future()  # run forever

    def run(self) -> None:
        asyncio.run(self.serve())
