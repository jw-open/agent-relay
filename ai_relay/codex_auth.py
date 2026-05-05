"""Codex CLI auth preparation before starting a subprocess."""

from __future__ import annotations

import logging
import os

from .auth_utils import env_truthy, read_json_file

logger = logging.getLogger(__name__)


def _auth_path(env: dict[str, str]) -> str:
    home = env.get("HOME", os.path.expanduser("~"))
    return os.path.join(home, ".codex", "auth.json")


def _has_chatgpt_auth(path: str) -> bool:
    data = read_json_file(path)
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return False
    return bool(tokens.get("access_token") and tokens.get("refresh_token"))


def ensure_codex_auth(env: dict[str, str]) -> None:
    """
    Prefer isolated Codex ChatGPT auth from HOME/.codex/auth.json when present.

    Codex falls back to OPENAI_API_KEY from the environment. In Lab, that can
    accidentally override a user's mounted ChatGPT auth, so strip API-key style
    variables when AI_RELAY_CODEX_PREFER_CHATGPT=1 and auth.json is usable.
    """
    if not env_truthy(env.get("AI_RELAY_CODEX_PREFER_CHATGPT")):
        return

    path = _auth_path(env)
    if not _has_chatgpt_auth(path):
        logger.debug("No Codex ChatGPT auth at %s", path)
        return

    for key in ("OPENAI_API_KEY", "OPENAI_AUTH_TOKEN"):
        if key in env:
            env.pop(key, None)
            logger.info("Removed %s from Codex subprocess env to prefer ChatGPT auth", key)
