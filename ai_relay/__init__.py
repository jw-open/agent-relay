"""
ai-relay: WebSocket relay for AI coding agent CLIs.

Bridges Claude Code, Codex, Gemini CLI, Snowflake Cortex (and more)
to any web interface over WebSocket.
"""

__version__ = "0.4.9"
__all__ = ["RelayServer", "RelaySession", "PtySession", "clean_pty_output", "CTRL_RE"]

from .relay import RelayServer, RelaySession
from .pty_session import PtySession, clean_pty_output, CTRL_RE
