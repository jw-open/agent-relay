"""CLI entry point for ai-relay."""

import logging
import click
from .relay import RelayServer


@click.command()
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind host")
@click.option("--port", default=8765, show_default=True, help="Bind port")
@click.option("--log-level", default="INFO", show_default=True,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False))
def main(host: str, port: int, log_level: str) -> None:
    """
    ai-relay — WebSocket relay for AI coding agent CLIs.

    Start the relay server, then connect from OhWise Lab (or any WebSocket
    client) and send a handshake JSON to start a session:

    \b
        {"tool": "claude", "folder": "/path/to/project", "model": "sonnet"}

    The relay will spawn the CLI, stream its output as structured events,
    and forward your messages as stdin to the process.
    """
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    server = RelayServer(host=host, port=port)
    server.run()


if __name__ == "__main__":
    main()
