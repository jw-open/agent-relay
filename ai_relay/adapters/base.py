"""Base adapter — defines the interface all tool adapters must implement."""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Optional

from ..events import EventType, RelayEvent
from ..pty_session import clean_pty_output
from ..transports import PtyTransport


class AgentRuntime(ABC):
    """Running adapter instance for one relay session."""

    def __init__(self, session_id: str, config: Optional[dict[str, Any]] = None):
        self.session_id = session_id
        self.config = config or {}

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def read_event(self) -> Optional[RelayEvent]:
        ...

    @abstractmethod
    async def handle_client_message(self, msg: dict[str, Any]) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    @abstractmethod
    async def wait(self) -> Optional[int]:
        ...


class PtyAgentRuntime(AgentRuntime):
    """Default runtime that bridges client text to an interactive PTY."""

    def __init__(
        self,
        session_id: str,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        adapter: type["BaseAdapter"],
        config: Optional[dict[str, Any]] = None,
    ):
        super().__init__(session_id, config)
        self.adapter = adapter
        self.transport = PtyTransport(cmd, cwd, env, session_id)

    async def start(self) -> None:
        await self.transport.start()

    async def read_event(self) -> Optional[RelayEvent]:
        chunk = await self.transport.read()
        if not chunk:
            return None
        cleaned = clean_pty_output(chunk)
        decoded = self.adapter.postprocess_line(cleaned)
        if not decoded:
            return RelayEvent(type=EventType.STDOUT, session_id=self.session_id, text="")
        return RelayEvent.from_raw(self.session_id, "stdout", decoded)

    async def handle_client_message(self, msg: dict[str, Any]) -> None:
        text = msg.get("text", "")
        if text:
            processed = self.adapter.preprocess_input(str(text))
            await self.transport.write(processed.encode())

    async def stop(self) -> None:
        await self.transport.stop()

    async def wait(self) -> Optional[int]:
        return await self.transport.wait()


class BaseAdapter(ABC):
    """
    An adapter knows how to:
    1. Build the subprocess argv for a given tool + config
    2. Pre-process input before sending to the process stdin
    3. Post-process raw output lines before event parsing
    """

    tool_name: str = "generic"
    protocol: str = "pty"
    requires_executable: bool = True

    @classmethod
    @abstractmethod
    def build_command(cls, folder: str, model: Optional[str] = None,
                      extra_args: Optional[list[str]] = None) -> list[str]:
        """Return the argv list to spawn the tool subprocess."""
        ...

    @classmethod
    def preprocess_input(cls, text: str) -> str:
        """Transform user input before writing to process stdin.

        TUI apps (Claude Code, Codex, Gemini CLI) run in raw terminal mode where
        pressing Enter sends CR (\\r), not LF (\\n).  The PTY line discipline converts
        \\r → \\n in cooked mode, but TUIs disable that (icrnl=off).  Send \\r so the
        app sees the actual Enter keypress and submits the prompt.
        """
        # Strip any trailing newline/carriage-return and send a bare CR
        text = text.rstrip("\r\n")
        return text + "\r"

    @classmethod
    def postprocess_line(cls, line: str) -> str:
        """Optional cleanup of raw output lines (strip ANSI, etc.)."""
        import re
        # Strip ANSI escape codes
        ansi = re.compile(r"\x1b\[[0-9;]*m|\x1b\[[0-9;]*[A-Za-z]|\r")
        return ansi.sub("", line)

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
        return PtyAgentRuntime(
            session_id=session_id,
            cmd=cls.build_command(folder, model, extra_args),
            cwd=folder,
            env=env,
            adapter=cls,
            config=config,
        )
