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


class Base(unittest.TestCase):
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


class VideoCopyTests(Base):
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
             patch.object(vc, "review_output", new_callable=AsyncMock,
                          return_value=('{"verdict": "deliver"}', "glm")) as review, \
             patch("agents.Runner.run", new_callable=AsyncMock, side_effect=production) as run:
            answer = asyncio.run(vc.run_job(job))
            self.assertIn("frames were inspected once", answer)
            again = asyncio.run(vc.run_job(job))
            self.assertIn("duplicate paid", again)
        self.assertEqual(run.await_count, 1)
        self.assertEqual(review.await_count, 1)  # the output is checked once, against the storyboard
        self.assertEqual(review.await_args.args[0], "Shot plan")
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



class RecreateTests(Base):
    """/recreate: multi-image references, albums, written sheets and the GLM-written plan."""

    def png(self, colour="red"):
        from PIL import Image
        import io
        out = io.BytesIO()
        Image.new("RGB", (32, 32), colour).save(out, "PNG")
        return out.getvalue()

    def photo(self, text, file_id="p1", **extra):
        return asyncio.run(tg.handle({**self.message, "caption": text,
                                      "photo": [{"file_id": file_id, "file_size": 100}], **extra}))

    def test_recreate_routes_like_copy_and_copy_still_works(self):
        self.configure()
        with patch("higgsfield.status", return_value={"connected": True}), patch("glm.available", return_value=None):
            self.handle("/recreate https://www.tiktok.com/@ceramics/video/123")
            self.handle("/copy https://www.instagram.com/reel/Example/")
        rows = ws.query("SELECT skill,task,allow_frontier FROM jobs ORDER BY created_at")
        self.assertEqual([r["skill"] for r in rows], ["video-copy", "video-copy"])
        self.assertTrue(all(r["allow_frontier"] for r in rows))
        self.assertIn("GLM", tg.send.call_args.args[2])

    def test_album_photos_build_one_character_set(self):
        with patch.object(tg, "download", new_callable=AsyncMock, side_effect=lambda token, fid: self.png()):
            self.photo("/recreate character Mila, our host", "a", media_group_id="g1")
            # The album's other photos arrive without a caption.
            asyncio.run(tg.handle({**self.message, "message_id": 2, "media_group_id": "g1",
                                   "photo": [{"file_id": "b", "file_size": 100}]}))
            asyncio.run(tg.handle({**self.message, "message_id": 3, "media_group_id": "g1",
                                   "photo": [{"file_id": "c", "file_size": 100}]}))
            value = vc.settings("default")["character"]
            self.assertEqual(len(value["images"]), 3)
            self.assertEqual(value["description"], "Mila, our host")
            # add keeps the set; a new captioned photo starts a new one.
            self.photo("/recreate character add", "d")
            self.assertEqual(len(vc.settings("default")["character"]["images"]), 4)
            self.photo("/recreate character Mila, new look", "e")
            self.assertEqual(len(vc.settings("default")["character"]["images"]), 1)
        self.assertEqual(ws.query("SELECT * FROM jobs"), [])  # album photos never became Cowork tasks

    def test_uncaptioned_album_photos_from_other_albums_stay_normal_messages(self):
        with patch.object(tg, "download", new_callable=AsyncMock, return_value=self.png()):
            self.assertFalse(asyncio.run(vc.album_item("t", 42, {**self.message, "media_group_id": "other",
                                                                 "photo": [{"file_id": "x", "file_size": 1}]})))

    def test_image_limit_and_bad_images(self):
        for _ in range(vc.MAX_IMAGES):
            vc.save_reference("default", "background", "Studio", self.png(), add=True)
        with self.assertRaisesRegex(ValueError, "already has"):
            vc.save_reference("default", "background", "", self.png(), add=True)
        with self.assertRaisesRegex(ValueError, "isn't an image"):
            vc.save_reference("default", "character", "Mila", b"not an image")

    def test_first_release_settings_are_read_as_a_list(self):
        root = vc.folder("default")
        root.mkdir(parents=True)
        (root / "character-old.jpg").write_bytes(self.png())
        (root / "settings.json").write_text(json.dumps({"character": {"description": "Old", "image": "character-old.jpg"},
                                                        "background": {"description": "Studio"}}))
        self.assertEqual(vc.settings("default")["character"]["images"], ["character-old.jpg"])
        config = vc.snapshot("default", self.space)
        self.assertEqual(len(config["character"]["images"]), 1)
        self.assertTrue(self.space.resolve(config["character"]["images"][0]).is_file())

    def test_uploaded_sheet_and_frame_sheet(self):
        with patch.object(tg, "download", new_callable=AsyncMock, return_value=b"# Mila\nRed bob, green coat"):
            asyncio.run(tg.handle({**self.message, "caption": "/recreate character sheet",
                                   "document": {"file_id": "s", "file_size": 30, "file_name": "mila.md"}}))
        value = vc.settings("default")["character"]
        self.assertEqual(value["sheet_label"], "uploaded mila.md")
        # A /frame sheet from any of the owner's chats, newest first.
        import sandbox
        root = Path(self.temp.name) / "cowork"
        sheet = root / "owner" / "threads" / "tg-42-1" / "character-sheets" / "studio-20261020-101010" / "sheet.md"
        sheet.parent.mkdir(parents=True)
        sheet.write_text("# Studio\nWhite brick, wheel")
        with patch.object(sandbox, "ROOT", root):
            self.handle("/recreate background sheet Studio")
            with self.assertRaisesRegex(ValueError, "No /frame sheet"):
                vc.frame_sheet("nobody")
        self.assertIn("studio-20261020", vc.settings("default")["background"]["sheet_label"])
        config = vc.snapshot("default", self.space)
        self.assertIn("Red bob", self.space.read_text(config["character"]["sheet"]))
        self.assertIn("White brick", self.space.read_text(config["background"]["sheet"]))

    def test_snapshot_needs_both_kinds_and_clear_removes_one(self):
        vc.save_reference("default", "character", "Mila")
        with self.assertRaisesRegex(ValueError, "background"):
            vc.snapshot("default", self.space)
        vc.save_reference("default", "background", "Studio")
        vc.snapshot("default", self.space)
        self.handle("/recreate clear character")
        self.assertNotIn("character", vc.settings("default"))

    def test_storyboard_uses_the_sheet_and_the_reference_images(self):
        vc.save_reference("default", "character", "Mila", self.png())
        vc.save_sheet("default", "character", "# Mila\nRed bob, green coat", "uploaded mila.md")
        vc.save_reference("default", "background", "Studio")
        config = vc.snapshot("default", self.space)
        asks = []

        async def ask(messages, vision, client, gate, tokens):
            asks.append((messages, vision))
            return ("Reference notes" if vision else "Storyboard"), 0.01, "glm-model"
        with patch("frames.probe", new_callable=AsyncMock, return_value=({"frames": []}, "", None)), \
             patch("frames.read_frames", new_callable=AsyncMock, return_value=([], 0.0, set())), \
             patch("frames._ask", side_effect=ask), patch.object(ws, "write_artifact"):
            root, brief = asyncio.run(vc.analyze("source.mp4", config, self.space, {"id": "j1", "project": "default"},
                                                 None, None, lambda *a: None))
        self.assertEqual(brief, "Storyboard")
        vision = [m for m, v in asks if v]
        self.assertEqual(len(vision), 1)  # one read of the character images; the background has none
        self.assertEqual(sum(1 for part in vision[0][0]["content"] if part["type"] == "image_url"), 1)
        writer = [m for m, v in asks if not v][0][0]["content"]
        self.assertIn("Red bob, green coat", writer)
        self.assertIn("Reference notes", writer)
        self.assertIn("Continuity", writer)


if __name__ == "__main__":
    unittest.main()
