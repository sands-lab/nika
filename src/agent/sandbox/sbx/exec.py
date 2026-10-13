"""Run commands inside a native Docker Sandbox via ``sbx exec``."""

from __future__ import annotations

import asyncio


async def start_sbx_exec(
    sandbox_name: str, shell_command: str
) -> asyncio.subprocess.Process:
    """Start ``bash -lc shell_command`` in *sandbox_name* (see ``agent.sandbox.exec``)."""
    from agent.sandbox.sbx.proxy import sbx_process_env

    return await asyncio.create_subprocess_exec(
        "sbx",
        "exec",
        sandbox_name,
        "bash",
        "-lc",
        shell_command,
        env=sbx_process_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
