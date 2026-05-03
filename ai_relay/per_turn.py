"""
PerTurnRuntime — restarts the agent subprocess for every user turn.

Claude Code and Gemini CLI in --print / headless mode read ALL stdin until
EOF, then process and respond.  They do NOT support an interactive
request-response loop over a single persistent stdin pipe.

PerTurnRuntime works around this by:
  - Starting a fresh subprocess for each user message
  - Passing ``--resume <session_id>`` on subsequent turns so the model
    picks up conversation history from its own on-disk store
  - Forwarding permission responses / control messages to the current
    subprocess immediately (Claude Code processes them on receipt without
    needing EOF)

The outward-facing AgentRuntime interface is unchanged — callers read
a single continuous event stream via ``read_event()`` and send messages
via ``handle_client_message()``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Type

from .adapters.base import AgentRuntime
from .events import EventType, RelayEvent

logger = logging.getLogger(__name__)


class PerTurnRuntime(AgentRuntime):
    """Wraps any AgentRuntime subclass, restarting per user turn."""

    #: Message types that are forwarded to the current subprocess directly
    #: (no restart, no queueing).
    CONTROL_TYPES = frozenset(
        {"interrupt", "permission_response", "set_model", "set_permission_mode"}
    )

    def __init__(
        self,
        tool: str,
        session_id: str,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        runtime_class: Type[AgentRuntime],
        resume_flag: str = "--resume",
    ) -> None:
        super().__init__(session_id)
        self.tool = tool
        self._base_cmd = cmd
        self._cwd = cwd
        self._env = env
        self._runtime_class = runtime_class
        self._resume_flag = resume_flag

        self._events: asyncio.Queue[Optional[RelayEvent]] = asyncio.Queue()
        self._current: Optional[AgentRuntime] = None
        self._reader_task: Optional[asyncio.Task] = None
        # Claude-specific: internal session_id for --resume
        self._agent_session_id: Optional[str] = None
        self._turn_lock = asyncio.Lock()
        self._stopped = False

    # ── AgentRuntime interface ────────────────────────────────────────────────

    async def start(self) -> None:
        """No-op: relay.py already emits SESSION_START before calling start().
        Subprocess starts on first user message via _run_turn()."""

    async def read_event(self) -> Optional[RelayEvent]:
        return await self._events.get()

    async def handle_client_message(self, msg: dict[str, Any]) -> None:
        if self._stopped:
            return

        msg_type = msg.get("type", "")

        # Control / permission messages: forward to running subprocess only.
        if msg_type in self.CONTROL_TYPES:
            if self._current:
                await self._current.handle_client_message(msg)
            return

        # User prompt: check there is actual content.
        if not self._extract_prompt(msg):
            return

        async with self._turn_lock:
            await self._run_turn(msg)

    async def stop(self) -> None:
        self._stopped = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._current:
            await self._current.stop()
        await self._events.put(None)

    async def wait(self) -> Optional[int]:
        return None  # multi-turn; no single meaningful exit code

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run_turn(self, msg: dict[str, Any]) -> None:
        """Cancel previous reader, stop old subprocess, start a new one."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self._current:
            try:
                await self._current.stop()
            except Exception:
                pass

        cmd = list(self._base_cmd)
        if self._agent_session_id:
            cmd = cmd + [self._resume_flag, self._agent_session_id]

        logger.info(
            "[per-turn:%s] Starting subprocess %s (resume=%s)",
            self.session_id, cmd[0], self._agent_session_id,
        )

        # Pass the captured Claude conversation ID so ClaudeStructuredRuntime
        # sends the correct session_id in its stream-json stdin payload.
        # On the first turn _agent_session_id is None → new conversation.
        try:
            self._current = self._runtime_class(
                self.session_id, cmd, self._cwd, self._env,
                claude_session_id=self._agent_session_id,
            )
        except TypeError:
            # Fallback for runtimes that don't accept claude_session_id (e.g. Gemini)
            self._current = self._runtime_class(self.session_id, cmd, self._cwd, self._env)
        await self._current.start()
        await self._current.handle_client_message(msg)

        self._reader_task = asyncio.create_task(self._pump_turn(self._current))

    async def _pump_turn(self, runtime: AgentRuntime) -> None:
        """Forward events from one turn's subprocess to the shared event queue."""
        try:
            while not self._stopped:
                event = await runtime.read_event()
                if event is None:
                    logger.debug("[per-turn:%s] turn EOF", self.session_id)
                    break
                # Capture Claude's internal session_id for --resume on next turn.
                if event.raw:
                    raw = event.raw
                    if raw.get("type") == "system" and raw.get("subtype") == "init":
                        sid = raw.get("session_id")
                        if sid:
                            self._agent_session_id = sid
                            logger.info(
                                "[per-turn:%s] agent session_id=%s",
                                self.session_id, sid,
                            )
                await self._events.put(event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[per-turn:%s] transport error: %s", self.session_id, exc)
            await self._events.put(RelayEvent(
                type=EventType.ERROR,
                session_id=self.session_id,
                text=f"Relay error: {exc}",
            ))

    @staticmethod
    def _extract_prompt(msg: dict[str, Any]) -> Optional[str]:
        content = msg.get("content")
        if content is None:
            content = msg.get("text", "")
        if isinstance(content, list):
            parts = [
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") in {"text", "input_text"}
            ]
            prompt = "\n".join(x for x in parts if x)
            # Image-only message: treat as non-empty so the turn is executed
            if not prompt and any(
                isinstance(p, dict) and p.get("type") == "image"
                for p in content
            ):
                prompt = "[image]"
        else:
            prompt = str(content)
        return prompt.strip() or None
