"""/frame character sheets (agents/frames.py, the Telegram command) and the chat site's GLM model
(integrations/openwebui_glm.py, /api/glm/chat)."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "agents" / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import workspace as ws
import sandbox
import console
import crew
import frames
import glm
import telegram_bot
import video_probe
from openai import AsyncOpenAI
import httpx2 as httpx
from fastapi.testclient import TestClient
from test_glm_orchestrator import Base, KEY, client_for, sse
from test_telegram_study import FakeTelegram

NOTES = [{"shot": "medium close-up", "subjects": [{"label": "A", "hair": "long copper waves",
                                                   "outfit": [{"item": "blazer", "colour": "camel"}]}],
          "setting": "white kitchen", "action": "talks to camera"}]
SHEET = "# Mara — character sheet\n\n## At a glance\n- Long copper waves, camel blazer\n\n## Prompt kit\n- Base prompt: ..."


def completion(text, model):
    return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 1, "model": model,
                                     "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                                  "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 2000, "completion_tokens": 500, "total_tokens": 2500}})


def has_image(body):
    return any(isinstance(m["content"], list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in body["messages"])


class FramePieces(Base):
    def test_dense_sampling_covers_the_video_and_every_shot(self):
        times = video_probe.dense_times(20.0, [(0.0, 4.0), (4.0, 4.6), (4.6, 20.0)], 10)
        self.assertLessEqual(len(times), 10)
        self.assertTrue(times[0] < 2 and times[-1] > 17)
        self.assertIn(4.15, times)  # the 0.6 s shot is caught
        many = video_probe.dense_times(60.0, [(i, i + 1) for i in range(60)], 12)
        self.assertEqual(len(many), 12)
        self.assertTrue(many[-1] > 50)
        self.assertEqual(video_probe.dense_times(0, [], 10), [0.0])

    def test_task_round_trip_palette_and_lenient_notes(self):
        self.assertEqual(frames.parse_task(frames.task_text("https://vm.tiktok.com/x/", "Mara")),
                         ("https://vm.tiktok.com/x/", "Mara"))
        self.assertEqual(frames.parse_task(frames.task_text("uploads/clip.mp4")), ("uploads/clip.mp4", ""))
        from PIL import Image
        path = Path(self.temp.name) / "red.jpg"
        Image.new("RGB", (64, 64), (200, 20, 20)).save(path)
        colours = frames.palette(str(path))
        self.assertTrue(colours[0]["hex"].startswith("#c") and colours[0]["share"] > 0.9)
        batch = [{"at": 1.0}, {"at": 2.0}]
        parsed = frames.parse_notes('Here you go:\n[{"shot": "wide"}, {"shot": "close-up"}]', batch)
        self.assertEqual([(n["t"], n["shot"]) for n in parsed], [(1.0, "wide"), (2.0, "close-up")])
        self.assertIn("not json", frames.parse_notes("not json", batch)[0]["notes"])


class FramePipeline(Base):
    def fake_probe(self, count=6):
        async def probe(space, source, folder):
            from PIL import Image
            out = space.resolve(folder)
            (out / "frames").mkdir(parents=True, exist_ok=True)
            found = []
            for i in range(count):
                path = out / "frames" / f"{i:02d}_{i * 1.5:06.2f}s.jpg"
                Image.new("RGB", (90, 160), (30 * i, 120, 200)).save(path)
                found.append({"at": i * 1.5, "path": str(path)})
            return ({"source": source, "frames": found, "video": {"duration": 9.0, "width": 1080, "height": 1920},
                     "transcript": {"text": "hey guys, quick fit check"}, "shots": {"list": [[0, 9]]}}, out, None)
        return probe

    def run_pipeline(self, glm_handler=None, hub_handler=None):
        space = sandbox.Workspace("owner", "tg-42-1").prepare()
        identity = ws.create_job("default", frames.task_text("https://vm.tiktok.com/x/", "Mara"), frames.SKILL, "fast",
                                 allow_frontier=True, thread="tg-42-1")
        job = ws.claim_job()
        hub_client = client_for(hub_handler or (lambda r: completion("[]", "qwen")))
        with patch.object(frames, "probe", self.fake_probe()), \
             patch.object(glm, "client", lambda: client_for(glm_handler)):
            result = asyncio.run(frames.extract("https://vm.tiktok.com/x/", space, "default", identity, hub_client,
                                                asyncio.Semaphore(2), lambda *a: None, "Mara"))
        return identity, job, space, result

    def test_glm_reads_the_frames_and_writes_a_saved_shared_sheet(self):
        glm.save_key(KEY)
        seen = []

        def glm_handler(request):
            body = json.loads(request.content)
            seen.append((body["model"], has_image(body)))
            if has_image(body):
                count = sum(1 for m in body["messages"] for p in m["content"] if p.get("type") == "image_url")
                return completion(json.dumps(NOTES * count), body["model"])
            self.assertIn("MEASURED PALETTE", body["messages"][0]["content"])
            self.assertIn("quick fit check", body["messages"][0]["content"])
            return completion("```markdown\n" + SHEET + "\n```", body["model"])

        identity, _, space, result = self.run_pipeline(glm_handler)
        self.assertTrue(result["ok"], result)
        vision_calls = [s for s in seen if s[1]]
        self.assertEqual(len(vision_calls), 2)  # 6 frames, 4 per call
        self.assertEqual({m for m, _ in vision_calls}, {glm.VISION_MODEL})
        self.assertIn((glm.MODEL, False), seen)
        sheet = (space.dir / result["sheet"]).read_text()
        self.assertTrue(sheet.startswith("# Mara — character sheet"))
        self.assertIn("## Frame by frame", sheet)
        self.assertIn("copper waves", sheet)
        names = [r["name"] for r in ws.query("SELECT name FROM artifacts WHERE job_id=?", (identity,))]
        self.assertEqual(names, ["mara-character-sheet.md", "mara-contact-sheet.jpg", "mara-frames.zip"])
        with zipfile.ZipFile(space.dir / result["folder"] / "frames.zip") as archive:
            self.assertEqual(sum(n.startswith("frames/") for n in archive.namelist()), 6)
        self.assertTrue(ws.search("default", "copper waves blazer"))
        expected = 2 * glm.cost(2000, 0, 500, glm.VISION_MODEL) + glm.cost(2000, 0, 500, glm.MODEL)
        self.assertAlmostEqual(result["cost"], round(expected, 4), places=4)
        self.assertAlmostEqual(glm.this_month()["usd"], expected, places=6)
        self.assertIn("Mara — character sheet", frames.summary(result, "Mara"))

    def test_without_glm_qwen_reads_and_writes_for_free(self):
        models = []

        def hub_handler(request):
            body = json.loads(request.content)
            models.append(body["model"])
            return completion(json.dumps(NOTES * 4) if has_image(body) else SHEET, body["model"])

        _, _, _, result = self.run_pipeline(hub_handler=hub_handler)
        self.assertTrue(result["ok"])
        self.assertEqual((result["readers"], result["writer"], result["cost"]), (["qwen"], "qwen", 0))
        self.assertEqual(set(models), {"qwen"})

    def test_frame_jobs_run_the_pipeline_not_an_agent(self):
        identity = ws.create_job("default", frames.task_text("uploads/clip.mp4"), frames.SKILL, "fast", allow_frontier=True,
                                 thread="tg-42-1")
        job = ws.claim_job()
        called = {}

        async def extract(source, space, project, job_id, client, gate, log, name="", share=True):
            called.update(source=source, owner=space.is_owner, job=job_id)
            return {"ok": True, "folder": "character-sheets/x", "sheet": "character-sheets/x/sheet.md", "frames": 3,
                    "cost": 0.02, "readers": [glm.VISION_MODEL], "writer": glm.MODEL, "saved_to_knowledge": True,
                    "glance": "- copper waves", "seconds": 5}
        import hub
        with patch.object(frames, "extract", extract), patch.object(hub, "async_client", lambda: client_for(lambda r: None)):
            reply = asyncio.run(crew.run_job(job))
        self.assertEqual(called, {"source": "uploads/clip.mp4", "owner": True, "job": identity})
        self.assertIn("character sheet", reply)
        self.assertIn("knowledge base", reply)


class TelegramFrameCommand(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeTelegram()
        patcher = patch.object(telegram_bot, "api", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        state = asyncio.run(telegram_bot.connect("123456789:" + "A" * 35))
        asyncio.run(telegram_bot.handle(self.message(f"/start {state['pairing_code']}")))

    def message(self, text, **extra):
        return {"message_id": 7, "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text, **extra}

    def test_frame_with_a_link_or_a_captioned_video_starts_a_frame_job(self):
        async def download(token, file_id):
            return b"video bytes"

        async def scenario(message):
            with patch.object(telegram_bot, "watch", lambda job, chat: asyncio.sleep(0)), \
                    patch.object(telegram_bot, "download", download):
                await telegram_bot.handle(message)
        asyncio.run(scenario(self.message("/frame https://vm.tiktok.com/ZMabc123/ Mara")))
        job = ws.query("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1")[0]
        self.assertEqual(job["skill"], "frame-extraction")
        self.assertEqual(frames.parse_task(job["task"]), ("https://vm.tiktok.com/ZMabc123/", "Mara"))
        self.assertIn(job["id"], telegram_bot.load()["pending"])
        self.assertIn("Extracting frames for Mara", self.fake.sent()[-1])
        message = {"message_id": 8, "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "caption": "/frame",
                   "video": {"file_id": "f1", "file_size": 11, "file_name": "fit.mp4"}}
        asyncio.run(scenario(message))
        job = ws.query("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1")[0]
        self.assertEqual(frames.parse_task(job["task"]), ("uploads/fit.mp4", ""))
        asyncio.run(scenario(self.message("/frame")))
        self.assertIn("Send /frame with a video link", self.fake.sent()[-1])
        self.assertEqual(ws.query("SELECT COUNT(*) AS n FROM jobs WHERE skill='frame-extraction'")[0]["n"], 2)


class GlmChat(Base):
    def glm_stream(self, seen):
        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            pieces = [{"id": "c", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                       "choices": [{"index": 0, "delta": {"reasoning": "thinking…"}, "finish_reason": None}]},
                      {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                       "choices": [{"index": 0, "delta": {"content": "Hello from GLM"}, "finish_reason": "stop"}]},
                      {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": body["model"], "choices": [],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join("data: " + json.dumps(p) + "\n\n" for p in pieces) + "data: [DONE]\n\n")
        return handler

    def events(self, client, messages):
        response = client.post("/api/glm/chat", headers={"Authorization": "Bearer " + "k" * 40}, json={"messages": messages})
        self.assertEqual(response.status_code, 200, response.text)
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def test_streamed_metered_chat_and_images_go_to_the_vision_model(self):
        client = TestClient(console.create_app("k" * 40, run_worker=False))
        self.assertIn("not connected", self.events(client, [{"role": "user", "content": "hi"}])[0]["error"])
        glm.save_key(KEY)
        seen = []
        with patch.object(glm, "client", lambda: client_for(self.glm_stream(seen))):
            events = self.events(client, [{"role": "user", "content": "hi"}])
            self.assertEqual([list(e)[0] for e in events], ["reasoning", "content", "done"])
            self.assertEqual(events[-1]["done"]["model"], glm.MODEL)
            picture = [{"type": "text", "text": "what's this?"},
                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
            events = self.events(client, [{"role": "user", "content": picture}])
        self.assertEqual([b["model"] for b in seen], [glm.MODEL, glm.VISION_MODEL])
        self.assertEqual(glm.this_month()["calls"], 2)
        self.assertEqual(client.post("/api/glm/chat", headers={"Authorization": "Bearer " + "k" * 40},
                                     json={"messages": [{"role": "tool", "content": "x"}]}).status_code, 400)

    def test_the_chat_site_pipe_streams_thinking_and_text_for_the_owner_only(self):
        spec = importlib.util.spec_from_file_location("glm_pipe", ROOT / "integrations" / "openwebui_glm.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = console.create_app("k" * 40, run_worker=False)
        pipe = module.Pipe()
        pipe.valves.OWNER_KEY = "k" * 40
        pipe._client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                                 headers={"Authorization": "Bearer " + "k" * 40})
        glm.save_key(KEY)
        seen = []

        async def reply(messages, user):
            return [c async for c in pipe.pipe({"messages": messages, "stream": True}, __user__=user)]
        with patch.object(glm, "client", lambda: client_for(self.glm_stream(seen))):
            chunks = asyncio.run(reply([{"role": "user", "content": "hi"}], {"role": "admin"}))
            refused = asyncio.run(reply([{"role": "user", "content": "hi"}], {"role": "user"}))
            hidden = asyncio.run(reply([{"role": "user", "content": "/connect glm " + KEY}], {"role": "admin"}))
        self.assertTrue(chunks[0].startswith("data: ") and "reasoning_content" in chunks[0])
        self.assertEqual(chunks[1], "Hello from GLM")
        self.assertEqual(refused, ["Only the owner can use this model."])
        self.assertIn("Qwen Cowork", hidden[0])
        self.assertEqual(len(seen), 1)  # neither the stranger nor the key reached GLM
        history = [{"role": "user", "content": [{"type": "text", "text": "look"},
                                                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
                   {"role": "assistant", "content": "A cat."},
                   {"role": "user", "content": "/connect glm secret"}, {"role": "user", "content": "thanks"}]
        cleaned = module.clean_messages(history, 100000)
        self.assertEqual(cleaned[0]["content"], "look\n[1 image(s) shared earlier]")
        self.assertEqual(cleaned[2]["content"], "(connection command, hidden)")
        self.assertEqual(module.clean_messages(history[:1], 100000)[0]["content"][1]["type"], "image_url")
