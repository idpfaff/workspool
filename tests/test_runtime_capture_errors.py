"""Exercise FastMCP's returned error objects and raised ToolError capture paths."""

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from agent_workbook.budget import serialized_result
from agent_workbook.continuity import recent, session_status
from agent_workbook.discovery import configure_discovery
from agent_workbook.server import server_for
from agent_workbook.store import Workbook


def application(tmp_path, probe, discovery):
    workbook = Workbook(tmp_path / "workbook")
    app = server_for(
        workbook,
        profile="runtime",
        tools=["inspect"],
        agent_id="capture-owner",
    )
    app.add_tool(probe, name="capture_probe", structured_output=False)
    if discovery == "lazy":
        configure_discovery(app, {"capture_probe"})
    return workbook, app


def invoke(app, discovery):
    return asyncio.run(
        app.call_tool("workspool", {"tool": "capture_probe", "arguments": {}})
        if discovery == "lazy"
        else app.call_tool("capture_probe", {})
    )


@pytest.mark.parametrize("discovery", ["eager", "lazy"])
@pytest.mark.parametrize("structured", [False, True])
def test_returned_plain_error_is_redacted_unverified_and_preserves_response(
    tmp_path, discovery, structured
):
    secret = "sk-proj-" + "x" * 40
    response = CallToolResult(
        isError=True,
        content=[
            TextContent(type="text", text="Not JSON: upstream request failed"),
            TextContent(
                type="text",
                text=f"Authorization=Bearer {secret}; password=plain-private-value",
            ),
        ],
        structuredContent={"state": "complete", "columns": ["n"], "rows": [[123]]}
        if structured
        else None,
    )

    def probe() -> CallToolResult:
        return response

    workbook, app = application(tmp_path, probe, discovery)
    # No tokenizer download: a deterministic counter tests the accounting path.
    app.count_tokens = len
    returned = invoke(app, discovery)
    assert returned == response and returned.isError
    assert app.capture_errors == 0
    records = recent(workbook)
    assert len(records) == 1
    episode = records[0]["episode"]
    assert episode["verified"] is False
    assert episode["actor"] == "capture-owner"
    assert episode["error"].startswith("Not JSON: upstream request failed")
    assert "[REDACTED]" in episode["error"]
    assert secret not in json.dumps(
        records
    ) and "plain-private-value" not in json.dumps(records)
    assert episode["estimated_rendered_tokens"] == len(serialized_result(response))
    status = session_status(workbook, app.capture_session.session_id)
    assert status["episode_count"] == status["estimate_events"] == 1
    if structured:
        assert episode["facts"]["state"] == "complete"  # Still a failed execution.


@pytest.mark.parametrize(
    "content",
    [[], [ImageContent(type="image", mimeType="image/png", data="aA==")]],
)
def test_error_without_text_still_records_unverified_failure(tmp_path, content):
    response = CallToolResult(isError=True, content=content)

    def probe() -> CallToolResult:
        return response

    workbook, app = application(tmp_path, probe, "eager")
    assert invoke(app, "eager").isError
    episode = recent(workbook)[0]["episode"]
    assert episode["verified"] is False
    assert episode["error"] == "Tool returned an MCP error without a text message"
    assert app.capture_errors == 0


@pytest.mark.parametrize("discovery", ["eager", "lazy"])
def test_sdk_raised_tool_error_remains_unverified(tmp_path, discovery):
    def probe() -> CallToolResult:
        raise ValueError("request rejected: password=plain-private-value")

    workbook, app = application(tmp_path, probe, discovery)
    with pytest.raises(ToolError):
        invoke(app, discovery)
    records = recent(workbook)
    assert len(records) == 1 and records[0]["episode"]["verified"] is False
    assert "request rejected" in records[0]["episode"]["error"]
    assert "plain-private-value" not in json.dumps(records)
    assert app.capture_errors == 0


def test_successful_structured_result_remains_verified(tmp_path):
    response = CallToolResult(
        isError=False,
        content=[TextContent(type="text", text='{"state":"complete"}')],
        structuredContent={"state": "complete"},
    )

    def probe() -> CallToolResult:
        return response

    workbook, app = application(tmp_path, probe, "eager")
    assert not invoke(app, "eager").isError
    episode = recent(workbook)[0]["episode"]
    assert episode["verified"] is True and episode["error"] is None
    assert app.capture_errors == 0
