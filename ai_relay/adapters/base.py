"""Base adapter — defines the interface all tool adapters must implement."""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional


class BaseAdapter(ABC):
    """
    An adapter knows how to:
    1. Build the subprocess argv for a given tool + config
    2. Pre-process input before sending to the process stdin
    3. Post-process raw output lines before event parsing
    """

    tool_name: str = "generic"

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
