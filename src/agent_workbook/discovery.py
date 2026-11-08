"""Experimental facade; never enabled by a profile (see APPROVAL_MODEL.md)."""

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult


def configure_discovery(app: FastMCP, exposed: set[str]) -> None:
    """Replace the allowed tools with one facade; retain their original validation.

    Named handlers are removed from registration, not merely hidden from
    tools/list. The retained Tool objects can only be reached through the
    facade's allowlist. This deliberately uses the same Tool.run entry point
    FastMCP uses, including argument validation and result conversion.
    """
    from .server import _slim_schema

    selected = {
        tool.name: tool
        for tool in app._tool_manager.list_tools()
        if tool.name in exposed
    }
    if not exposed or set(selected) != exposed:
        raise ValueError("Lazy discovery requires registered, allowed tools")
    definitions = {
        name: {
            "name": name,
            "description": tool.description,
            "inputSchema": _slim_schema(tool.parameters),
        }
        for name, tool in selected.items()
    }

    async def workspool(
        tool: str, arguments: dict[str, Any] | None = None
    ) -> CallToolResult:
        if tool not in selected:
            # Keep the allowlist check even though the enum also validates it.
            raise ValueError(f"Unknown or disabled tool: {tool}")
        if arguments is None:
            return app._format_result("workspool", {}, definitions[tool])
        result = await selected[tool].run(
            arguments, context=app.get_context(), convert_result=True
        )
        return app._format_result(tool, arguments, result)

    # A dynamic Literal advertises exactly the configured allowlist and is also
    # enforced by FastMCP's generated argument model at execution time.
    workspool.__annotations__["tool"] = Literal[tuple(sorted(exposed))]
    for tool in list(app._tool_manager.list_tools()):
        app.remove_tool(tool.name)
    app.add_tool(
        workspool,
        description=(
            "Call a workbook tool. Omit arguments to get its schema; "
            "pass {} to call with defaults."
        ),
        structured_output=False,
    )
