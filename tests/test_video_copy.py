"""Reference adaptation routing and bounded provider calls; no real generation or Telegram messages."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import store
import workspace as ws
import telegram_bot as tg
import video_copy as vc


class Space:
    def __init__(self, root):
        self.dir = Path(root)
        self.is_owner = True
    def prepare(self):
        return self
    def resolve(self, path):
        result = (self.dir / path).resolve()
        if not result.is_relative_to(self.dir.resolve()):
            raise ValueError("Outside workspace")
        return result
    def write_bytes(self, path, content):
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target
    def write_text(self, path, text):
        return self.write_bytes(path, text.encode())
    def read_text(self, path):
        return self.resolve(path).read_text(encoding="utf-8")
    def relative(self, path):
        return str(Path(path).relative_to(self.dir))


class VideoCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.space = Space(Path(self.temp.name) / "chat")
        self.space.dir.mkdir()
        self.patchers = [patch.object(store, "DATA", Path(self.temp.name) / "data"),
                         patch.object(tg, "send", new_callable=AsyncMock),
                         patch.object(tg, "_watch"),
                         patch("sandbox.Workspace", return_value=self.space)]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)
        ws.init()
        tg.save({"token": "test", "owner_chat": 42, "owner_user": 42, "threads": {}, "pending": {}})
        self.message = {"message_id": 1, "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}

    def handle(self, text, **extra):
        return asyncio.run(tg.handle({**self.message, "text": text, **extra}))

    def configure(self):
        vc.save_reference("default", "character", "Our fictional presenter in a blue jacket")
        vc.save_reference("default", "background", "Our bright ceramics studio")

    def test_bare_copy_waits_and_next_video_routes_without_generic_study(self):
        self.configure()
        self.handle("/copy")
        self.assertIn("42", tg.load()["copy_waiting"])
        with patch("higgsfield.status", return_value={"connected": True}):
            self.handle("https://www.instagram.com/reel/Example/")
        rows = ws.query("SELECT skill,task FROM jobs")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["skill"], "video-copy")
        self.assertEqual(json.loads(rows[0]["task"])["source"], "https://www.instagram.com/reel/Example/")
        self.assertNotIn("42", tg.load()["copy_waiting"])

    def test_missing_references_and_disconnected_provider_do_not_queue_generation(self):
        self.handle("/copy https://www.tiktok.com/@ceramics/video/123")
        self.assertEqual(ws.query("SELECT * FROM jobs"), [])
        self.configure()
        with patch("higgsfield.status", return_value={"connected": False}):
            self.handle("/copy https://www.tiktok.com/@ceramics/video/123")
        self.assertEqual(ws.query("SELECT * FROM jobs"), [])
        self.assertIn("/connect higgsfield", tg.send.call_args.args[2])

    def test_reply_to_uploaded_video_retains_unique_source(self):
        self.configure()
        reply = {"message_id": 9, "video": {"file_id": "x", "file_size": 11, "file_name": "clip.mp4"}}
        with patch.object(tg, "download", new_callable=AsyncMock, return_value=b"video bytes"), \
             patch("higgsfield.status", return_value={"connected": True}):
            self.handle("/copy", reply_to_message=reply)
        request = json.loads(ws.query("SELECT task FROM jobs")[0]["task"])
        self.assertTrue(request["source"].startswith("copy-inputs/source-"))
        self.assertEqual(self.space.resolve(request["source"]).read_bytes(), b"video bytes")

    def test_new_chat_and_cancel_clear_pending_copy(self):
        self.handle("/copy")
        self.handle("/copy cancel")
        self.assertNotIn("42", tg.load()["copy_waiting"])
        self.handle("/copy")
        self.handle("/new")
        self.assertNotIn("42", tg.load()["copy_waiting"])

    def test_stranger_cannot_set_persona_or_start_copy(self):
        asyncio.run(tg.handle({**self.message, "from": {"id": 99}, "text": "/persona malicious"}))
        import social_missions
        self.assertIn("No custom persona", social_missions.persona("default"))
        self.assertFalse(tg.send.called)

    def test_provider_generation_is_not_retried_after_uncertain_result(self):
        from agents import FunctionTool
        callback = AsyncMock(side_effect=TimeoutError("uncertain"))
        tool = FunctionTool(name="higgsfield__generate", description="Generate", params_json_schema={"type": "object"},
                            on_invoke_tool=callback, strict_json_schema=False)
        wrapped = vc.bounded_provider_tools([tool], self.space, "copy/test", lambda: None)[0]
        with self.assertRaises(TimeoutError):
            asyncio.run(wrapped.on_invoke_tool(None, '{"prompt":"a bowl"}'))
        with self.assertRaisesRegex(RuntimeError, "already been attempted"):
            asyncio.run(wrapped.on_invoke_tool(None, '{"prompt":"a bowl"}'))
        # The same fence survives rebuilding the tools after a restart.
        again = vc.bounded_provider_tools([tool], self.space, "copy/test", lambda: None)[0]
        with self.assertRaises(RuntimeError):
            asyncio.run(again.on_invoke_tool(None, '{"prompt":"a bowl"}'))
        self.assertEqual(callback.await_count, 1)

    def test_provider_result_is_saved_and_unknown_media_url_is_rejected(self):
        from agents import FunctionTool
        tool = FunctionTool(name="higgsfield__generate", description="Generate", params_json_schema={"type": "object"},
                            on_invoke_tool=AsyncMock(return_value='{"url":"https://cdn.example/video.mp4"}'),
                            strict_json_schema=False)
        wrapped = vc.bounded_provider_tools([tool], self.space, "copy/test", lambda: None)[0]
        result = asyncio.run(wrapped.on_invoke_tool(None, "{}"))
        self.assertIn("Saved receipt", result)
        with patch("web._public"):
            vc.provider_url("https://cdn.example/video.mp4", self.space, "copy/test")
            with self.assertRaises(ValueError):
                vc.provider_url("https://elsewhere.example/video.mp4", self.space, "copy/test")

    def test_storyboard_cache_avoids_second_analysis(self):
        self.space.write_text("video-copy/job/storyboard.md", "Finished shot plan")
        with patch("frames.probe", new_callable=AsyncMock) as probe:
            result = asyncio.run(vc.analyze("source", {}, self.space, {"id": "job"}, None, None, lambda *a: None))
        self.assertEqual(result[1], "Finished shot plan")
        probe.assert_not_awaited()

    def test_production_tools_are_bounded_and_restart_does_not_generate_again(self):
        from agents import FunctionTool
        from agents.tool_context import ToolContext
        job_id = ws.create_job("default", json.dumps({"source": "source.mp4", "settings": {}}),
                               "video-copy", "fast", thread="chat")
        job = ws.claim_job()
        provider = FunctionTool(name="higgsfield__generate", description="Generate",
                                params_json_schema={"type": "object"}, strict_json_schema=False,
                                on_invoke_tool=AsyncMock(return_value="queued"))
        unrelated = FunctionTool(name="other__send", description="Send",
                                 params_json_schema={"type": "object"}, strict_json_schema=False,
                                 on_invoke_tool=AsyncMock())
        root = f"video-copy/{job_id}"
        client = SimpleNamespace(close=AsyncMock())
        async def production(agent, prompt, **kwargs):
            self.assertEqual(kwargs["max_turns"], 24)
            tools = {t.name: t for t in agent.tools}
            self.assertIn("higgsfield__generate", tools)
            self.assertNotIn("other__send", tools)
            self.assertNotIn("delegate", tools)
            self.assertNotIn("analyze_video", tools)
            arguments = json.dumps({"path": "output.mp4"})
            context = ToolContext(context=None, tool_name="inspect_output", tool_call_id="test",
                                  tool_arguments=arguments)
            await tools["inspect_output"].on_invoke_tool(context, arguments)
            second = await tools["inspect_output"].on_invoke_tool(context, arguments)
            self.assertIn("already ran", str(second))
            return SimpleNamespace(final_output="Generated output.mp4")
        with patch.object(vc, "analyze", new_callable=AsyncMock, return_value=(root, "Shot plan")), \
             patch("connectors.open_mcp", new_callable=AsyncMock, return_value=([provider, unrelated], [])), \
             patch("hub.async_client", return_value=client) as create_client, \
             patch("hub.model", return_value="qwen"), \
             patch("frames.probe", new_callable=AsyncMock, return_value=({"frames": []}, "", None)) as probe, \
             patch("frames.read_frames", new_callable=AsyncMock, return_value=([], 0, set())), \
             patch("agents.Runner.run", new_callable=AsyncMock, side_effect=production) as run:
            answer = asyncio.run(vc.run_job(job))
            self.assertIn("frames were inspected once", answer)
            again = asyncio.run(vc.run_job(job))
            self.assertIn("duplicate paid", again)
        self.assertEqual(run.await_count, 1)
        self.assertEqual(probe.await_count, 1)
        self.assertEqual(create_client.call_count, 1)
        client.close.assert_awaited_once()

    def test_unavailable_provider_returns_storyboard_without_starting_generation(self):
        ws.create_job("default", json.dumps({"source": "source.mp4", "settings": {}}),
                      "video-copy", "fast", thread="chat")
        job = ws.claim_job()
        root = f"video-copy/{job['id']}"
        with patch.object(vc, "analyze", new_callable=AsyncMock, return_value=(root, "Shot plan")), \
             patch("connectors.open_mcp", new_callable=AsyncMock, return_value=([], [])), \
             patch("hub.async_client", return_value=SimpleNamespace(close=AsyncMock())), \
             patch("agents.Runner.run", new_callable=AsyncMock) as run:
            answer = asyncio.run(vc.run_job(job))
        self.assertIn("reconnect", answer)
        self.assertFalse(self.space.resolve(root + "/generation-started.json").exists())
        run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
