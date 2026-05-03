"""
Auto-refresh Claude Code OAuth tokens before starting a subprocess.

Replicates exactly what Claude Code does internally:
  POST https://platform.claude.com/v1/oauth/token
  { grant_type: "refresh_token", refresh_token, client_id, scope }

Refreshes if the token expires within the next 5 minutes (same buffer
Claude Code uses). Safe to call before every subprocess launch — no-ops
if the token is still fresh or credentials are not found.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# Sourced from Claude Code 2.1.88 src-extracted/src/constants/oauth.ts
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_SCOPES = (
    "user:profile user:inference user:sessions:claude_code "
    "user:mcp_servers user:file_upload"
)
# Refresh if the token expires within 5 minutes (Claude Code uses same buffer)
REFRESH_BUFFER_MS = 5 * 60 * 1000


def _credentials_path(env: dict[str, str]) -> str:
    home = env.get("HOME", os.path.expanduser("~"))
    return os.path.join(home, ".claude", ".credentials.json")


def _read_credentials(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_credentials(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic write


def _env_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _prefer_oauth(env: dict[str, str], oauth: dict) -> None:
    if not oauth.get("accessToken") or not _env_truthy(env.get("AI_RELAY_CLAUDE_PREFER_OAUTH")):
        return
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if key in env:
            env.pop(key, None)
            logger.info("Removed %s from Claude subprocess env to prefer OAuth credentials", key)


def _needs_refresh(creds: dict) -> bool:
    oauth = creds.get("claudeAiOauth", {})
    expires_at = oauth.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)
    return (now_ms + REFRESH_BUFFER_MS) >= expires_at


def _do_refresh(refresh_token: str) -> Optional[dict]:
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
        "scope": OAUTH_SCOPES,
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
        logger.warning("Claude OAuth refresh HTTP %s: %s", e.code, e.read()[:200])
        return None
    except Exception as e:
        logger.warning("Claude OAuth refresh failed: %s", e)
        return None


def ensure_claude_token(env: dict[str, str]) -> None:
    """
    Read HOME/.claude/.credentials.json, refresh the OAuth token if it is
    expired or expiring within the next 5 minutes, and write it back.

    Mimics the exact refresh flow used by Claude Code itself.
    No-ops if credentials are not found, have no refresh token, or are still fresh.
    """
    path = _credentials_path(env)
    creds = _read_credentials(path)
    if not creds:
        logger.debug("No credentials file at %s — skipping token refresh", path)
        return

    oauth = creds.get("claudeAiOauth", {})
    _prefer_oauth(env, oauth)
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        logger.debug("No refreshToken in credentials — skipping token refresh")
        return

    if not _needs_refresh(creds):
        expires_at = oauth.get("expiresAt", 0)
        now_ms = int(time.time() * 1000)
        logger.debug(
            "Claude token still valid — %.1f min remaining",
            (expires_at - now_ms) / 60000,
        )
        return

    expires_at = oauth.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)
    remaining_min = (expires_at - now_ms) / 60000
    logger.info(
        "Claude token expires in %.1f min — refreshing via OAuth", remaining_min
    )

    result = _do_refresh(refresh_token)
    if not result:
        logger.warning("OAuth refresh failed — Claude subprocess may hit 401")
        return

    now_ms = int(time.time() * 1000)
    oauth["accessToken"] = result["access_token"]
    oauth["refreshToken"] = result.get("refresh_token", refresh_token)
    oauth["expiresAt"] = now_ms + int(result["expires_in"]) * 1000
    if "scope" in result:
        oauth["scopes"] = result["scope"].split()

    creds["claudeAiOauth"] = oauth
    _write_credentials(path, creds)

    new_expiry_min = int(result["expires_in"]) / 60
    logger.info("Claude token refreshed — valid for %.0f min", new_expiry_min)
