"""Prepare Gemini CLI auth before starting a subprocess."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
REFRESH_BUFFER_MS = 5 * 60 * 1000

AUTH_LOGIN_WITH_GOOGLE = "oauth-personal"
AUTH_GEMINI_API_KEY = "gemini-api-key"
AUTH_VERTEX_AI = "vertex-ai"


def _creds_path(env: dict[str, str]) -> str:
    home = env.get("HOME", os.path.expanduser("~"))
    return os.path.join(home, ".gemini", "oauth_creds.json")


def _settings_path(env: dict[str, str]) -> str:
    gemini_home = env.get("GEMINI_CLI_HOME")
    if gemini_home:
        return os.path.join(gemini_home, "settings.json")
    home = env.get("HOME", os.path.expanduser("~"))
    return os.path.join(home, ".gemini", "settings.json")


def _env_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_creds(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_creds(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic write
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _has_oauth_creds(creds: Optional[dict]) -> bool:
    if not isinstance(creds, dict):
        return False
    return bool(creds.get("access_token") and creds.get("refresh_token"))


def _selected_auth_from_env(env: dict[str, str]) -> Optional[str]:
    if _env_truthy(env.get("GOOGLE_GENAI_USE_VERTEXAI")):
        return AUTH_VERTEX_AI
    if env.get("GEMINI_API_KEY"):
        return AUTH_GEMINI_API_KEY
    if _env_truthy(env.get("GOOGLE_GENAI_USE_GCA")):
        return AUTH_LOGIN_WITH_GOOGLE
    return None


def _write_selected_auth(env: dict[str, str], auth_type: str) -> None:
    path = _settings_path(env)
    try:
        with open(path) as f:
            settings = json.load(f)
            if not isinstance(settings, dict):
                settings = {}
    except (OSError, json.JSONDecodeError):
        settings = {}

    security = settings.setdefault("security", {})
    if not isinstance(security, dict):
        security = {}
        settings["security"] = security
    auth = security.setdefault("auth", {})
    if not isinstance(auth, dict):
        auth = {}
        security["auth"] = auth

    if auth.get("selectedType") == auth_type:
        return

    auth["selectedType"] = auth_type
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, path)


def _needs_refresh(creds: dict) -> bool:
    expiry_date = creds.get("expiry_date", 0)
    now_ms = int(time.time() * 1000)
    return (now_ms + REFRESH_BUFFER_MS) >= expiry_date


def _do_refresh(refresh_token: str, client_id: str, client_secret: str) -> Optional[dict]:
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning("Gemini OAuth refresh HTTP %s: %s", e.code, e.read()[:200])
        return None
    except Exception as e:
        logger.warning("Gemini OAuth refresh failed: %s", e)
        return None


def ensure_gemini_auth(env: dict[str, str]) -> None:
    """
    Prepare Gemini CLI auth for ACP/headless use.

    Gemini ACP uses `security.auth.selectedType` from settings when creating a
    session. In Lab, OAuth credentials live under the isolated HOME, so make the
    selected auth explicit and remove API-key env vars when OAuth is preferred.
    """
    path = _creds_path(env)
    creds = _read_creds(path)
    has_oauth = _has_oauth_creds(creds)
    prefer_oauth = _env_truthy(env.get("AI_RELAY_GEMINI_PREFER_OAUTH"))

    if has_oauth and prefer_oauth:
        for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"):
            if key in env:
                env.pop(key, None)
                logger.info("Removed %s from Gemini subprocess env to prefer OAuth credentials", key)
        _write_selected_auth(env, AUTH_LOGIN_WITH_GOOGLE)
    elif _selected_auth_from_env(env):
        _write_selected_auth(env, _selected_auth_from_env(env) or AUTH_GEMINI_API_KEY)
    elif has_oauth:
        _write_selected_auth(env, AUTH_LOGIN_WITH_GOOGLE)
    else:
        return

    if not has_oauth:
        return

    refresh_token = creds.get("refresh_token") if creds else None
    if not refresh_token:
        return

    if not _needs_refresh(creds):
        return

    client_id = env.get("GEMINI_OAUTH_CLIENT_ID") or os.getenv("GEMINI_OAUTH_CLIENT_ID")
    client_secret = env.get("GEMINI_OAUTH_CLIENT_SECRET") or os.getenv("GEMINI_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.debug("Gemini OAuth client configuration missing; Gemini CLI will refresh tokens if needed.")
        return

    logger.info("Gemini token expiring soon — refreshing via OAuth")
    result = _do_refresh(refresh_token, client_id, client_secret)
    if not result:
        return

    now_ms = int(time.time() * 1000)
    creds["access_token"] = result["access_token"]
    if "refresh_token" in result:
        creds["refresh_token"] = result["refresh_token"]
    creds["expiry_date"] = now_ms + int(result["expires_in"]) * 1000
    
    _write_creds(path, creds)
    logger.info("Gemini token refreshed")
