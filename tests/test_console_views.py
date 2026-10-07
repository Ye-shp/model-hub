"""What the console shows: per-task GPUs, questions and hand-offs, Jev decisions, the knowledge base, per-chat
memory, the bundled fonts, and the image-box check's user agent."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "tests"))
import asking
import console
import workspace as ws
from fastapi.testclient import TestClient
from test_cowork import Base

AUTH = {"Authorization": "Bearer " + "k" * 40}


class ConsoleViewTests(Base):
    def client(self):
        return TestClient(console.create_app("k" * 40, run_worker=False))

    def test_activity_shows_gpus_current_step_question_and_hand_offs(self):
        ws.create_job("default", "CONVERSATION SO FAR:\nUSER: hi\n\nCURRENT REQUEST:\nWrite hooks", "cowork", thread="chat-a",
                      requested_by="me@example.com")
        job = ws.claim_job()
        ws.event(job["id"], "tool", "Lead on qwen-2, helpers on qwen-1")
        ws.event(job["id"], "escalation", "jev: ask first?")
        ws.event(job["id"], "escalation-done", "jev: ask first? failed")
        ws.event(job["id"], "escalation", "claude: write the scraper")
        asking.ask(job["id"], "Which platform?", ["TikTok", "Reels"], 30)
        ws.create_job("default", "AUTOMATIC CONTINUATION 1 of up to 4\n\nCURRENT REQUEST:\nWrite hooks", "cowork",
                      thread="chat-a", parent=job["id"])
        client = self.client()
        rows = {r["id"]: r for r in client.get("/api/activity", headers=AUTH).json()["jobs"]}
        mine = rows[job["id"]]
        self.assertEqual((mine["gpus"], mine["question"], mine["jev_calls"], mine["claude_calls"]),
                         ("qwen-2, helpers on qwen-1", "Which platform?", 1, 1))
        self.assertEqual(mine["step"], "claude: write the scraper")
        self.assertTrue(mine["task"].startswith("Write hooks"))
        follow = next(r for r in rows.values() if r["id"] != job["id"])
        self.assertEqual((follow["automatic"], follow["parent"]), (1, job["id"]))

        decisions = client.get("/api/overview", headers=AUTH).json()["jev_decisions"]
        self.assertEqual(decisions, [{"purpose": "ask first?", "asked": 1, "failed": 1}])

    def test_knowledge_base_and_per_chat_memory(self):
        ws.ingest("default", "UGC: Pattern interrupt", "# UGC: Pattern interrupt\n\n## Summary\nOpen on motion.",
                  "https://www.tiktok.com/@a/video/1")
        ws.ingest("default", "Uploaded notes", "not a studied link", "upload:notes.md")
        client = self.client()
        listed = client.get("/api/knowledge", headers=AUTH).json()
        self.assertEqual((listed["total"], [d["title"] for d in listed["documents"]]), (1, ["UGC: Pattern interrupt"]))
        self.assertEqual(client.get("/api/knowledge?search=nothing", headers=AUTH).json()["documents"], [])
        doc = client.get(f"/api/knowledge/{listed['documents'][0]['id']}", headers=AUTH).json()
        self.assertIn("Open on motion.", doc["text"])
        self.assertEqual(client.get("/api/knowledge/missing", headers=AUTH).status_code, 404)
        self.assertEqual(client.get("/api/knowledge").status_code, 401)

        ws.create_job("default", "CURRENT REQUEST:\nPlan my launch", "cowork", thread="chat-m")
        ws.save_note("default", "Launch date", "March 3", "fact", [], thread="chat-m")
        ws.save_note("default", "Old note", "from before", "fact", [])
        notes = {n["title"]: n for n in client.get("/api/memories", headers=AUTH).json()["memories"]}
        self.assertEqual(notes["Launch date"]["chat_request"].strip(), "Plan my launch")
        self.assertEqual((notes["Old note"]["thread"], notes["Old note"]["chat_request"]), ("", None))

    def test_fonts_are_served_locally_and_nothing_else_is(self):
        client = self.client()
        for name in ("fraunces.woff2", "instrument-sans.woff2"):
            response = client.get("/fonts/" + name)
            self.assertEqual((response.status_code, response.headers["content-type"]), (200, "font/woff2"))
            self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(client.get("/fonts/..%2Fapp.js").status_code, 404)
        self.assertEqual(client.get("/fonts/OFL-Fraunces.txt").status_code, 404)

    def test_image_box_check_sends_a_user_agent(self):
        seen = {}

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"models": []}'

        def opener(request, timeout=None):
            if "img.example" in request.full_url:
                seen["agent"] = request.get_header("User-agent")
            return Response()

        import sandbox
        with patch.dict("os.environ", {"MODEL3_URL": "https://img.example/v1"}), patch("urllib.request.urlopen", opener):
            report = console.system_health(sandbox)
        self.assertEqual(report["image_box"], {"ok": True})
        self.assertTrue(seen["agent"].startswith("model-hub-console"))
