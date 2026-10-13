"""Work around MCP FastMCP / pydantic_settings forward-ref warning.

``Settings.lifespan`` is annotated with a forward reference to ``FastMCP``.
Constructing ``Settings`` before ``model_rebuild()`` emits
``IncompleteFieldDefinitionWarning`` (noisy in ``nika benchmark run``).
"""

from __future__ import annotations

_ready = False


def ensure_fastmcp_settings_ready() -> None:
    """Call once before the first ``FastMCP(...)`` in this process."""
    global _ready
    if _ready:
        return
    from mcp.server.fastmcp.server import Settings

    Settings.model_rebuild()
    _ready = True
