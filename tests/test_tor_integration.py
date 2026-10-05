"""Tor's deployment-to-Cowork contract, without a model or onion connection."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import code_update
import crew
import skills
import tor_fetch
import web
from cowork.prompt import instructions
from cowork.tools import _read_onion_page
from agents.tool_context import ToolContext as InvocationContext


URL = "https://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/"


async def invoke(tool, arguments):
    text = json.dumps(arguments)
    context = InvocationContext(context=None, tool_name=tool.name, tool_call_id="tor-test", tool_arguments=text)
    return await tool.on_invoke_tool(context, text)


class TorReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_uses_private_transport_and_returns_page_continuation(self):
        reader = Mock()
        reader.fetch.return_value = {"status": 200, "final_url": URL + "about", "text": "<html>page</html>"}
        with patch.dict(os.environ, {"TOR_SOCKS_SOCKET": "/private/tor/run/socks.sock"}), \
                patch.object(tor_fetch, "_fetcher", return_value=reader), \
                patch.object(web, "_extract", return_value=("Source title", "a" * 15000)):
            result = await tor_fetch.read_page(URL, offset=1000)
        reader.fetch.assert_called_once_with(URL, timeout=60, rotate=False, http_only=True, max_bytes=web.MAX_BYTES)
        self.assertEqual(result["url"], URL)
        self.assertEqual(result["final_url"], URL + "about")
        self.assertEqual(result["title"], "Source title")
        self.assertEqual(len(result["content"]), 12000)
        self.assertEqual(result["next_offset"], 13000)

    async def test_bad_urls_and_offsets_never_reach_transport(self):
        with patch.object(tor_fetch, "_fetcher") as reader:
            for url in ("file:///etc/passwd", "https://example.com/", "http://127.0.0.1/",
                        "https://user:secret@example.onion/", "-K/tmp/options"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    await tor_fetch.read_page(url)
            with self.assertRaises(ValueError):
                await tor_fetch.read_page(URL, offset=-1)
        reader.assert_not_called()

    async def test_missing_hub_service_has_no_tcp_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(tor_fetch, "_fetcher") as reader:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                await tor_fetch.read_page(URL)
        reader.assert_not_called()

    async def test_http_and_transfer_failures_are_not_returned_as_content(self):
        for failure in (ConnectionError("transfer interrupted"), {"status": 503, "text": "error page"}):
            reader = Mock()
            if isinstance(failure, Exception):
                reader.fetch.side_effect = failure
            else:
                reader.fetch.return_value = failure
            with self.subTest(failure=failure), \
                    patch.dict(os.environ, {"TOR_SOCKS_SOCKET": "/private/tor/run/socks.sock"}), \
                    patch.object(tor_fetch, "_fetcher", return_value=reader), \
                    patch.object(web, "_extract") as extract:
                with self.assertRaises((ConnectionError, RuntimeError)):
                    await tor_fetch.read_page(URL)
                extract.assert_not_called()

    async def test_owner_tool_returns_reader_result_and_checks_task_budget(self):
        ctx = SimpleNamespace(space=SimpleNamespace(is_owner=True), budget=Mock(), log=Mock())
        tool = _read_onion_page(ctx)
        with patch.object(tor_fetch, "read_page", new_callable=AsyncMock, return_value={"content": "evidence"}) as read:
            result = await invoke(tool, {"url": URL, "offset": 12})
        read.assert_awaited_once_with(URL, 12)
        ctx.budget.active.assert_called_once()
        self.assertEqual(json.loads(result)["content"], "evidence")

    async def test_direct_guest_tool_invocation_cannot_reach_reader(self):
        ctx = SimpleNamespace(space=SimpleNamespace(is_owner=False), budget=Mock(), log=Mock())
        with patch.object(tor_fetch, "read_page", new_callable=AsyncMock) as read:
            result = await invoke(_read_onion_page(ctx), {"url": URL, "offset": 0})
        self.assertIn("owner", result)
        read.assert_not_awaited()
        ctx.budget.active.assert_not_called()


class SkillDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_tor_skill_runs_in_cowork_instead_of_legacy_team(self):
        import cowork
        job = {"skill": "tor-fetcher"}
        gate = asyncio.Semaphore(1)
        with patch.object(cowork, "run_job", new_callable=AsyncMock, return_value="done") as runner:
            self.assertEqual(await crew.run_job(job, gate), "done")
        runner.assert_awaited_once_with(job, gate)

    def test_skill_is_discoverable_and_loaded_for_owner_onion_requests(self):
        self.assertIn("tor-fetcher", {item["id"] for item in skills.catalog()})
        playbook = skills.load_skill("tor-fetcher")
        job = {"profile": "fast", "allow_images": False, "skill": "cowork", "task": f"Read {URL}"}
        owner = SimpleNamespace(dir="/workspace/owner/chat", is_owner=True)
        guest = SimpleNamespace(dir="/workspace/guest/chat", is_owner=False)
        owner_prompt = instructions(job, owner)
        self.assertIn(playbook["instructions"], owner_prompt)
        self.assertNotIn("read_onion_page", instructions(job, guest))
        self.assertIn(playbook["instructions"], instructions(job, owner, helper=True))
        self.assertNotIn("read_onion_page", instructions(job, guest, helper=True))
        job.update(skill="tor-fetcher", task="Read the source I supplied earlier")
        self.assertIn(playbook["instructions"], instructions(job, owner))
        self.assertIn(playbook["instructions"], instructions(job, owner, helper=True))
        job.update(skill="cowork", task="Research the topic")
        helper_prompt = instructions(job, owner, helper=True)
        self.assertIn("read_onion_page", helper_prompt)
        self.assertIn("shell fallback", helper_prompt)
        self.assertIn("untrusted evidence", helper_prompt)


if __name__ == "__main__":
    unittest.main()
