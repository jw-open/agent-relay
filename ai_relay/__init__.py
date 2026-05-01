"""
agent-relay: WebSocket relay for AI coding agent CLIs.

Bridges Claude Code, Codex, Gemini CLI, Snowflake Cortex (and more)
to any web interface over WebSocket.
"""

__version__ = "0.1.2"
__all__ = ["RelayServer", "RelaySession"]

from .relay import RelayServer, RelaySession
