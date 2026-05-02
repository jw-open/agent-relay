from .base import BaseAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter
from .generic import GenericAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {
    "claude": ClaudeCodeAdapter,
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
    "cortex": GenericAdapter,
    "generic": GenericAdapter,
}


def get_adapter(tool: str) -> type[BaseAdapter]:
    return ADAPTERS.get(tool.lower(), GenericAdapter)
