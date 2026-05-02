"""Google Gemini CLI adapter."""

from __future__ import annotations
from typing import Optional
from .base import BaseAdapter


class GeminiAdapter(BaseAdapter):
    tool_name = "gemini"

    @classmethod
    def build_command(cls, folder: str, model: Optional[str] = None,
                      extra_args: Optional[list[str]] = None) -> list[str]:
        cmd = ["gemini"]
        if model:
            cmd += ["--model", model]
        if extra_args:
            cmd += extra_args
        return cmd
