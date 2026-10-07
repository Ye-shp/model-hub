"""Large MCP catalogs stay out of ordinary prompts without losing remote capabilities."""
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


def remote(name="models_list", schema=None, description="Find supported video models"):
    return SimpleNamespace(name=name, description=description,
                           input_schema=schema if schema is not None else {"type": "object", "properties": {}})


def result(text="Done"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False)


def compact(server_name, server, catalog, check=None, log=None, media=False):
    check, log = check or Mock(), log or Mock()
    wrapped = [connectors.wrap(server_name, server, tool, check, log, higgsfield=media) for tool in catalog]
    return connectors.discovery_tools(server_name, wrapped, catalog, check)


async def invoke(tool, arguments):
    encoded = json.dumps(arguments, ensure_ascii=False)
    context = ToolContext(context=None, tool_name=tool.name, tool_call_id="catalog-test", tool_arguments=encoded)
    return await tool.on_invoke_tool(context, encoded)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_huge_catalog_advertises_two_compact_tools_and_preserves_small_catalog(self):
        schema = {"type": "object", "properties": {"preset": {"type": "string", "enum": ["P" * 8000] * 20}}}
        catalog = [remote(f"generate_video_{n}", schema, "A" * 2000) for n in range(115)]
        server = SimpleNamespace(connect=AsyncMock(), cleanup=AsyncMock(), list_tools=AsyncMock(return_value=catalog))
        with patch.object(connectors, "load", return_value={"mcp": {"higgsfield": {"auth": "higgsfield"}}}), \
                patch.object(connectors, "_server", return_value=server):
            async with AsyncExitStack() as stack:
                tools, notes = await connectors.open_mcp(stack, SimpleNamespace(is_owner=True), Mock(), Mock())
        self.assertEqual(len(tools), 2)
        advertised = json.dumps([{"name": tool.name, "description": tool.description,
                                  "parameters": tool.params_json_schema} for tool in tools])
        self.assertLess(len(advertised), 2500)
        self.assertNotIn("P" * 8000, advertised)
        self.assertIn("115 tools available", notes[0])
        self.assertIn(connectors.HIGGSFIELD_GUIDE, notes)
        server.cleanup.assert_awaited_once()

        one = remote()
        server.list_tools.return_value = [one]
        with patch.object(connectors, "load", return_value={"mcp": {"small": {}}}), \
                patch.object(connectors, "_server", return_value=server):
            async with AsyncExitStack() as stack:
                tools, notes = await connectors.open_mcp(stack, SimpleNamespace(is_owner=True), Mock(), Mock())
        self.assertEqual([tool.name for tool in tools], ["small__models_list"])
        self.assertEqual(tools[0].params_json_schema, one.input_schema)

    async def test_direct_catalog_budget_is_shared_across_servers(self):
        def server():
            return SimpleNamespace(connect=AsyncMock(), cleanup=AsyncMock(),
                                   list_tools=AsyncMock(return_value=[remote(f"lookup_{n}") for n in range(8)]))
        with patch.object(connectors, "load", return_value={"mcp": {"one": {}, "two": {}, "three": {}}}), \
                patch.object(connectors, "_server", side_effect=[server(), server(), server()]):
            async with AsyncExitStack() as stack:
                tools, notes = await connectors.open_mcp(stack, SimpleNamespace(is_owner=True), Mock(), Mock())
        self.assertEqual(len(tools), 12)  # eight direct, two compact tools for each later server
        self.assertEqual(sum("tools available through" in note for note in notes), 2)

    async def test_one_oversized_schema_uses_discovery_even_with_a_small_tool_count(self):
        catalog = [remote("huge", {"type": "object", "description": "schema" * 10000})]
        server = SimpleNamespace(connect=AsyncMock(), cleanup=AsyncMock(), list_tools=AsyncMock(return_value=catalog))
        with patch.object(connectors, "load", return_value={"mcp": {"one": {}}}), \
                patch.object(connectors, "_server", return_value=server):
            async with AsyncExitStack() as stack:
                tools, _ = await connectors.open_mcp(stack, SimpleNamespace(is_owner=True), Mock(), Mock())
        self.assertEqual(len(tools), 2)

    async def test_search_is_ranked_bounded_and_paginated_without_losing_exact_names(self):
        names = [f"video_{n:03}" for n in range(30)]
        catalog = [remote(name, description="video capability " + "Z" * 2000) for name in names]
        catalog.append(remote("unrelated", description="Account balance"))
        search, _ = compact("media", SimpleNamespace(), catalog)
        found, offset = [], 0
        while True:
            output = await invoke(search, {"query": "video", "limit": 1000, "offset": offset})
            self.assertLessEqual(len(output), connectors.DISCOVERY_OUTPUT_CHARACTERS)
            page = json.loads(output)
            self.assertLessEqual(len(page["tools"]), connectors.DISCOVERY_RESULTS)
            self.assertTrue(all(len(tool["description"]) <= 240 for tool in page["tools"]))
            found.extend(tool["name"] for tool in page["tools"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual(found, names)
        self.assertEqual(json.loads(await invoke(search, {"query": "balance"}))["tools"][0]["name"], "unrelated")

    async def test_full_schema_reassembles_and_actual_escaped_output_stays_bounded(self):
        schema = {"type": "object", "$defs": {"Preset": {"type": "string", "enum": [
            'quote"\\slash\n雪\u0001' * 1200, "other"]}},
            "properties": {"preset": {"$ref": "#/$defs/Preset"}}, "required": ["preset"]}
        search, _ = compact("media", SimpleNamespace(), [remote("generate.video", schema, "\u0001" * 900)])
        fragments, offset = [], 0
        while True:
            output = await invoke(search, {"name": "generate.video", "offset": offset})
            self.assertLessEqual(len(output), connectors.DISCOVERY_OUTPUT_CHARACTERS)
            page = json.loads(output)
            self.assertEqual(page["offset"], offset)
            fragments.append(page["schema_json"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertGreater(len(fragments), 2)
        self.assertEqual(json.loads("".join(fragments)), schema)
        self.assertEqual("".join(fragments), json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
        self.assertIn("outside", await invoke(search, {"name": "generate.video", "offset": 10 ** 9}))

    async def test_colliding_aliases_and_long_names_dispatch_by_original_name(self):
        names = ["foo.bar", "foo_bar", "a" * 80 + ".one", "a" * 80 + ".two"]
        server = SimpleNamespace(call_tool=AsyncMock(return_value=result()))
        search, call = compact("media", server, [remote(name) for name in names])
        listed = json.loads(await invoke(search, {"limit": 8}))["tools"]
        self.assertEqual({tool["name"] for tool in listed}, set(names))
        for name in names:
            self.assertEqual(await invoke(call, {"name": name, "arguments": {"chosen": name}}), "Done")
        self.assertEqual([request.args for request in server.call_tool.await_args_list],
                         [(name, {"chosen": name}) for name in names])
        self.assertIn("Unknown", await invoke(call, {"name": "a" * 64, "arguments": {}}))
        self.assertEqual(server.call_tool.await_count, len(names))

    async def test_normalized_server_names_and_concurrent_jobs_keep_separate_handles(self):
        servers = [SimpleNamespace(call_tool=AsyncMock(return_value=result("first"))),
                   SimpleNamespace(call_tool=AsyncMock(return_value=result("second")))]
        first = compact("my-server", servers[0], [remote()])
        second = compact("my_server", servers[1], [remote()])
        self.assertFalse({tool.name for tool in first} & {tool.name for tool in second})
        self.assertTrue(all(len(tool.name) <= 64 for tool in first + second))
        answers = await asyncio.gather(invoke(first[1], {"name": "models_list", "arguments": {"job": 1}}),
                                       invoke(second[1], {"name": "models_list", "arguments": {"job": 2}}))
        self.assertEqual(answers, ["first", "second"])
        servers[0].call_tool.assert_awaited_once_with("models_list", {"job": 1})
        servers[1].call_tool.assert_awaited_once_with("models_list", {"job": 2})

    async def test_open_mcp_avoids_cross_server_and_intra_server_direct_alias_collisions(self):
        catalogs = [[remote()], [remote()], [remote("foo.bar"), remote("foo_bar")]]
        servers = [SimpleNamespace(connect=AsyncMock(), cleanup=AsyncMock(),
                                   list_tools=AsyncMock(return_value=catalog)) for catalog in catalogs]
        with patch.object(connectors, "load", return_value={"mcp": {"my-server": {}, "my_server": {}, "third": {}}}), \
                patch.object(connectors, "_server", side_effect=servers):
            async with AsyncExitStack() as stack:
                tools, notes = await connectors.open_mcp(stack, SimpleNamespace(is_owner=True), Mock(), Mock())
        self.assertEqual(len(tools), 5)  # first server direct; the conflicting catalogs use compact tools
        self.assertEqual(len({tool.name for tool in tools}), len(tools))
        self.assertEqual(sum("tools available through" in note for note in notes), 2)

    async def test_unknown_ambiguous_and_bad_arguments_do_not_call_remote(self):
        server = SimpleNamespace(call_tool=AsyncMock(return_value=result()))
        search, call = compact("media", server, [remote("duplicate"), remote("duplicate"), remote("safe")])
        self.assertIn("Unknown", await invoke(call, {"name": "missing", "arguments": {}}))
        self.assertIn("ambiguous", await invoke(call, {"name": "duplicate", "arguments": {}}))
        self.assertIn("Unknown", await invoke(search, {"name": "missing"}))
        self.assertIn("JSON object", await invoke(call, {"name": "safe", "arguments": "{}"}))
        self.assertIn("Discovery needs", await invoke(search, {"offset": True}))
        self.assertIn("Discovery needs", await invoke(search, {"offset": -1}))
        self.assertIn("Invocation needs", await call.on_invoke_tool(None, "[1]"))
        server.call_tool.assert_not_awaited()
        # Knowledge from a previous task is usable if this task still has the exact tool.
        self.assertEqual(await invoke(call, {"name": "safe", "arguments": {}}), "Done")

    async def test_stopped_job_blocks_discovery_and_invocation_and_rpc_cancellation_propagates(self):
        server = SimpleNamespace(call_tool=AsyncMock(return_value=result()))
        check = Mock(side_effect=RuntimeError("task stopped"))
        search, call = compact("media", server, [remote()], check=check)
        for tool in (search, call):
            with self.assertRaisesRegex(RuntimeError, "task stopped"):
                await invoke(tool, {"name": "models_list", "arguments": {}})
        server.call_tool.assert_not_awaited()
        server.call_tool.side_effect = asyncio.CancelledError()
        _, call = compact("media", server, [remote()])
        with self.assertRaises(asyncio.CancelledError):
            await invoke(call, {"name": "models_list", "arguments": {}})

    async def test_media_arguments_structured_results_logging_and_timeout_warning_survive(self):
        media_result = SimpleNamespace(content=[SimpleNamespace(type="text", text="Widget")],
            structured_content={"job_id": "job-123", "url": "https://assets.higgsfield.ai/video.mp4"}, is_error=False)
        server = SimpleNamespace(call_tool=AsyncMock(return_value=media_result))
        log, check = Mock(), Mock()
        _, call = compact("higgsfield", server, [remote("generate_video")], check=check, log=log, media=True)
        arguments = {"prompt": "雪 and quoted \"text\"", "seed": 123, "settings": {"audio": False}}
        output = await invoke(call, {"name": "generate_video", "arguments": arguments})
        server.call_tool.assert_awaited_once_with("generate_video", arguments)
        self.assertIn("job-123", output)
        self.assertIn("https://assets.higgsfield.ai/video.mp4", output)
        log.assert_called_once_with("tool", "higgsfield: generate_video")
        self.assertGreaterEqual(check.call_count, 2)
        server.call_tool.side_effect = TimeoutError()
        output = await invoke(call, {"name": "generate_video", "arguments": arguments})
        self.assertIn("may still be running", output)
        self.assertIn("before submitting again", output)
        server.call_tool.side_effect = RuntimeError("Bearer SECRET_DO_NOT_ECHO")
        output = await invoke(call, {"name": "generate_video", "arguments": arguments})
        self.assertNotIn("SECRET_DO_NOT_ECHO", output)
        self.assertIn("existing job status", output)


if __name__ == "__main__":
    unittest.main()
