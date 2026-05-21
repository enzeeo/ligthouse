"""Command dispatch for Lighthouse."""

from __future__ import annotations

import argparse
import errno
import sys

from storage_dashboard import server, tui


def main() -> None:
    parser = argparse.ArgumentParser(prog="lighthouse", description="Run Lighthouse.")
    parser.add_argument("command", nargs="?", choices=("tui", "web"), default="tui")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.command == "web":
        try:
            server.run(port=args.port)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            next_port = args.port + 1
            print(
                f"Port {args.port} is already in use. "
                f"Stop the process using it or run: lighthouse web --port {next_port}",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        return

    tui.run()


if __name__ == "__main__":
    main()
