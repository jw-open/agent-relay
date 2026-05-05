"""Process transports used by relay adapters."""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from .pty_session import PtySession

# StreamReader line buffer: 100 MB — Claude Code echoes full user messages
# (including base64 images) as a single JSON line.  The default 64 KB limit
# raises LimitOverrunError for any image-containing turn.
SUBPROCESS_STREAM_LIMIT = 100 * 1024 * 1024

# WebSocket max frame size exposed for callers that create the WS server.
WS_MAX_SIZE = 50 * 1024 * 1024


class PtyTransport:
    """PTY transport for terminal-first CLIs."""

    def __init__(
        self,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        session_id: str,
        auto_confirm_delay: float = 0,
    ):
        self._pty = PtySession(
            cmd=cmd,
            cwd=cwd,
            env=env,
            session_id=session_id,
            auto_confirm_delay=auto_confirm_delay,
        )

    async def start(self) -> None:
        await self._pty.start()

    async def read(self) -> Optional[bytes]:
        return await self._pty.read()

    async def write(self, data: bytes) -> None:
        await self._pty.write(data)

    async def stop(self) -> None:
        await self._pty.stop()

    async def wait(self) -> Optional[int]:
        if self._pty._process:
            return await self._pty._process.wait()
        return None


class StructuredProcessTransport:
    """Subprocess transport for newline-delimited JSON protocols."""

    def __init__(self, cmd: list[str], cwd: str, env: dict[str, str]):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self._process: Optional[asyncio.subprocess.Process] = None

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self.cmd,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            limit=SUBPROCESS_STREAM_LIMIT,
        )

    async def readline(self) -> Optional[bytes]:
        if self._process is None:
            raise RuntimeError("Transport not started — call start() first")
        if self._process.stdout is None:
            raise RuntimeError("stdout pipe is not available")
        line = await self._process.stdout.readline()
        return line or None

    async def read_stderr(self) -> Optional[bytes]:
        if self._process is None:
            raise RuntimeError("Transport not started — call start() first")
        if self._process.stderr is None:
            raise RuntimeError("stderr pipe is not available")
        line = await self._process.stderr.readline()
        return line or None

    async def write_json_line(self, line: str) -> None:
        if self._process is None:
            raise RuntimeError("Transport not started — call start() first")
        if self._process.stdin is None:
            raise RuntimeError("stdin pipe is not available")
        self._process.stdin.write(line.encode("utf-8") + b"\n")
        await self._process.stdin.drain()

    async def write_bytes(self, data: bytes) -> None:
        if self._process is None:
            raise RuntimeError("Transport not started — call start() first")
        if self._process.stdin is None:
            raise RuntimeError("stdin pipe is not available")
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def stop(self) -> None:
        if not self._process:
            return
        if self._process.stdin:
            try:
                self._process.stdin.close()
                await self._process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

    async def wait(self) -> Optional[int]:
        if self._process:
            return await self._process.wait()
        return None

    @property
    def returncode(self) -> Optional[int]:
        return self._process.returncode if self._process else None


def build_process_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = (base or os.environ).copy()
    env.setdefault("TERM", "xterm-256color")
    return env
