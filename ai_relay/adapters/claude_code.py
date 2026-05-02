"""Claude Code adapter."""

from __future__ import annotations
from typing import Optional
from .base import BaseAdapter


class ClaudeCodeAdapter(BaseAdapter):
    tool_name = "claude-code"

    @classmethod
    def build_command(cls, folder: str, model: Optional[str] = None,
                      extra_args: Optional[list[str]] = None) -> list[str]:
        cmd = ["claude", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        if extra_args:
            cmd += extra_args
        return cmd
