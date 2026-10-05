"""Higgsfield's owner-only MCP transport and result delivery, without provider calls."""
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import connectors
from agents.tool_context import ToolContext


ENTRY = {"transport": "http", "url": "https://mcp.higgsfield.ai/mcp", "auth": "higgsfield"}
SCHEMA = {"type": "object", "properties": {"type": {"type": "string"}}}
REMOTE_TOOL = SimpleNamespace(name="models_list", description="Discover supported models", input_schema=SCHEMA)


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_gets_remote_schema_and_guidance_without_credentials(self):
        server = SimpleNamespace(connect=AsyncMock(), cleanup=AsyncMock(), list_tools=AsyncMock(return_value=[REMOTE_TOOL]))
        log = Mock()
        with patch.object(connectors, "load", return_value={"mcp": {"higgsfield": ENTRY}}), \
                patch.object(connectors, "_server", return_value=server):
            async with AsyncExitStack() as stack:
                tools, notes = await connectors.open_mcp(stack, SimpleNamespace(is_owner=True), Mock(), log)
                self.assertEqual(tools[0].name, "higgsfield__models_list")
                self.assertEqual(tools[0].params_json_schema, SCHEMA)
                self.assertIn(connectors.HIGGSFIELD_GUIDE, notes)
        server.cleanup.assert_awaited_once()

    async def test_guest_cannot_open_the_owner_connection(self):
        with patch.object(connectors, "load") as load, patch.object(connectors, "_server") as create:
            async with AsyncExitStack() as stack:
                self.assertEqual(await connectors.open_mcp(stack, SimpleNamespace(is_owner=False), Mock(), Mock()), ([], []))
        load.assert_not_called()
        create.assert_not_called()

    async def test_missing_auth_does_not_disable_other_connected_tools(self):
        other = SimpleNamespace(connect=AsyncMock(), cleanup=AsyncMock(), list_tools=AsyncMock(return_value=[REMOTE_TOOL]))
        secret = "private_token_never_print"
        with patch.object(connectors, "load", return_value={"mcp": {"higgsfield": ENTRY, "other": {"transport": "http"}}}), \
                patch.object(connectors, "_server", side_effect=[RuntimeError(secret), other]):
            async with AsyncExitStack() as stack:
                tools, notes = await connectors.open_mcp(stack, SimpleNamespace(is_owner=True), Mock(), Mock())
        self.assertEqual([tool.name for tool in tools], ["other__models_list"])
        self.assertIn("/connect higgsfield", "\n".join(notes))
        self.assertNotIn(secret, "\n".join(notes))

    async def test_media_result_ids_and_urls_survive_widget_and_text_blocks(self):
        result = SimpleNamespace(content=[SimpleNamespace(type="text", text="Result opens in the provider widget"),
                                          SimpleNamespace(type="resource_link", name="Video", uri="https://assets.higgsfield.ai/result.mp4")],
                                 structured_content={"job_id": "job-1", "result_url": "https://assets.higgsfield.ai/result.mp4"},
                                 is_error=False)
        server = SimpleNamespace(call_tool=AsyncMock(return_value=result))
        tool = connectors.wrap("higgsfield", server, REMOTE_TOOL, Mock(), Mock(), higgsfield=True)
        ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="hf-test", tool_arguments="{}")
        output = await tool.on_invoke_tool(ctx, "{}")
        self.assertIn('"job_id": "job-1"', output)
        self.assertIn("https://assets.higgsfield.ai/result.mp4", output)
        self.assertIn("Video:", output)
        server.call_tool.assert_awaited_once_with("models_list", {})

    async def test_failure_diagnostics_never_echo_credentials_or_encourage_repeat_submission(self):
        for error in (RuntimeError("Bearer private_token_never_print"), TimeoutError()):
            server = SimpleNamespace(call_tool=AsyncMock(side_effect=error))
            tool = connectors.wrap("higgsfield", server, REMOTE_TOOL, Mock(), Mock(), higgsfield=True)
            ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="hf-test", tool_arguments="{}")
            with self.subTest(error=type(error).__name__):
                output = await tool.on_invoke_tool(ctx, "{}")
                self.assertNotIn("private_token_never_print", output)
                self.assertIn("job", output)

    def test_native_oauth_cannot_be_redirected_to_another_mcp_server(self):
        import higgsfield
        auth = Mock()
        with patch.object(higgsfield, "auth", return_value=auth), \
                patch("agents.mcp.MCPServerStreamableHttp") as create:
            connectors._server("higgsfield", ENTRY, SimpleNamespace())
            self.assertIs(create.call_args.args[0]["auth"], auth)
            for changed in ({**ENTRY, "url": "https://another.invalid/mcp"}, {**ENTRY, "transport": "sse"}):
                with self.subTest(entry=changed), self.assertRaises(ValueError):
                    connectors._server("higgsfield", changed, SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
