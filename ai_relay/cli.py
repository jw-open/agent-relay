"""CLI entry point for ai-relay."""

import logging
import click
from .relay import RelayServer

_LOG_OPTS = dict(
    default="INFO",
    show_default=True,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
)


def _run_server(host: str, port: int, log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    RelayServer(host=host, port=port).run()


# ── CLI group ─────────────────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind host")
@click.option("--port", default=8765, show_default=True, help="Bind port")
@click.option("--log-level", **_LOG_OPTS)
@click.pass_context
def main(ctx: click.Context, host: str, port: int, log_level: str) -> None:
    """
    ai-relay — WebSocket relay for AI coding agent CLIs.

    \b
    Commands:
      serve   Run as a persistent WebSocket server (recommended for containers).
              Each incoming connection becomes one agent session.

    Running without a subcommand starts the server directly (backwards-compat):

    \b
        ai-relay --host 0.0.0.0 --port 8765

    Or use the explicit subcommand:

    \b
        ai-relay serve --port 9000

    Connect a WebSocket client and send a handshake to start a session:

    \b
        {"tool": "claude", "folder": "/path/to/project", "model": "sonnet"}
    """
    if ctx.invoked_subcommand is None:
        # Backwards-compatible: root command runs the server directly.
        _run_server(host, port, log_level)


# ── Subcommands ───────────────────────────────────────────────────────────────

@main.command("serve")
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind host")
@click.option("--port", default=9000, show_default=True, help="Bind port")
@click.option("--log-level", **_LOG_OPTS)
def serve_cmd(host: str, port: int, log_level: str) -> None:
    """
    Run as a persistent WebSocket server inside a container.

    Each incoming WebSocket connection becomes one independent agent session.
    The server stays running; multiple clients can connect simultaneously.

    \b
    Example (in Dockerfile or docker-compose):
        CMD ["ai-relay", "serve", "--port", "9000"]

    \b
    Example (local daemon):
        ai-relay serve --port 9000

    Connect a WebSocket client and send a handshake:

    \b
        {"tool": "claude", "folder": "/path/to/project", "model": "sonnet"}
    """
    _run_server(host, port, log_level)


if __name__ == "__main__":
    main()
