"""Entrypoint for the in-node Kubernetes MCP server."""

from __future__ import annotations

import argparse

from nika.service.k8s_mcp_server import DEFAULT_BIND, DEFAULT_PORT
from nika.service.k8s_mcp_server.server import run


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="NIKA Kubernetes MCP server")
    parser.add_argument("--host", default=DEFAULT_BIND)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
