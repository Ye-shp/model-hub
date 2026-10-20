"""Phone tools and Cowork session lifecycle, without device or model traffic."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
from agents.tool_context import ToolContext as Invocation
import phone_link
import social_missions
import store
import workspace as ws


class PhoneMissionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patchers = [patch.object(store, "DATA", Path(self.temp.name) / "data"),
                         patch.object(phone_link.BRIDGE, "last_screen",
                                      {"app": "com.facebook.katana", "elements": [], "jpeg": ""})]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)
        ws.init()
        self.job = {"id": "test", "project": "default", "thread": "chat", "profile": "fast"}

    def tools(self):
        return {t.name: t for t in phone_link.agent_tools(
            self.job, lambda *a: None, SimpleNamespace(active=lambda: None),
            "Find Turkish pottery groups and join relevant ones.", None, asyncio.Semaphore(1))}

    async def invoke(self, tool, **values):
        arguments = json.dumps(values)
        context = Invocation(context=None, tool_name=tool.name, tool_call_id="offline", tool_arguments=arguments)
        return await tool.on_invoke_tool(context, arguments)

    def test_rejected_submit_never_reaches_phone(self):
        with patch.object(phone_link, "screen", new_callable=AsyncMock) as screen, \
             patch.object(phone_link, "call", new_callable=AsyncMock) as call, \
             patch.object(social_missions.Session, "decide", new_callable=AsyncMock,
                          return_value={"navigation_only": False, "matches_review": False}):
            result = asyncio.run(self.invoke(self.tools()["phone_tap"], x=20, y=30))
        self.assertIn("not verified", str(result))
        screen.assert_awaited_once()
        call.assert_not_awaited()

    def test_same_source_transcript_is_processed_only_once(self):
        with patch.object(phone_link, "screen", new_callable=AsyncMock), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("frames.probe", new_callable=AsyncMock, return_value=({"frames": []}, "", None)) as probe, \
             patch("frames.read_frames", new_callable=AsyncMock, return_value=([], 0, set())), \
             patch.object(social_missions.Session, "observe", new_callable=AsyncMock, return_value={"subject": "Pottery"}):
            tool = self.tools()["phone_watch_post"]
            async def scenario():
                for _ in range(2):
                    await self.invoke(tool, source="https://www.tiktok.com/@potter/video/123")
            asyncio.run(scenario())
        probe.assert_awaited_once()

    def test_phone_session_finishes_without_quality_loop_or_next_phase_and_releases_lock(self):
        from cowork import runner
        ws.create_job("default", "Browse Turkish ceramics", "cowork", "fast", thread="chat")
        job = ws.claim_job()
        lease = MagicMock()
        def build(job, client, gate, space, state):
            state["phone_lease"] = lease
            state["next"] = "Keep browsing automatically"
            return SimpleNamespace()
        usage = SimpleNamespace(requests=1, input_tokens=10, output_tokens=10)
        result = SimpleNamespace(final_output="Finished this session.",
                                 context_wrapper=SimpleNamespace(usage=usage))
        space = SimpleNamespace(is_owner=True, prepare=lambda: space)
        client = SimpleNamespace(close=AsyncMock())
        with patch("sandbox.Workspace", return_value=space), \
             patch("hub.async_client", return_value=client), \
             patch.object(runner, "open_connectors", new_callable=AsyncMock), \
             patch.object(runner.orchestrator, "enabled", return_value=False), \
             patch.object(runner, "assign_gpus", return_value=("qwen-1", "qwen-2")), \
             patch("cowork.kickoff.clarify", new_callable=AsyncMock, return_value=""), \
             patch.object(runner, "direct_claude", new_callable=AsyncMock, return_value=None), \
             patch.object(runner, "build", side_effect=build), \
             patch.object(runner.Runner, "run", new_callable=AsyncMock, return_value=result) as run, \
             patch("cowork.judge.reply_gap", new_callable=AsyncMock) as gap:
            answer = asyncio.run(runner.run_job(job))
        self.assertEqual(answer, "Finished this session.")
        self.assertEqual(run.await_count, 1)
        gap.assert_not_awaited()
        self.assertFalse(ws.query("SELECT id FROM jobs WHERE parent=?", (job["id"],)))
        lease.__exit__.assert_called_once()
        client.close.assert_awaited_once()

    def test_shared_phone_lock_excludes_another_session_until_released(self):
        import social_automation
        with social_automation._phone_lock() as first:
            self.assertTrue(first)
            with social_automation._phone_lock() as second:
                self.assertFalse(second)
        with social_automation._phone_lock() as later:
            self.assertTrue(later)


if __name__ == "__main__":
    unittest.main()
