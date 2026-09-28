"""Coordination and Open WebUI Pipe contract tests; no live providers or Open WebUI required."""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import store
import workspace as ws
import coordination
import console
import httpx2 as httpx

spec = importlib.util.spec_from_file_location("hub_pipe", ROOT / "integrations" / "openwebui_pipe.py")
pipe_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipe_module)


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = store.DATA
        store.DATA = Path(self.temp.name)
        ws.init()

    def tearDown(self):
        store.DATA = self.old_data
        self.temp.cleanup()

    def new_job(self, project="default"):
        return ws.create_job(project, "Compare the supplied sources", "research-brief")

    def test_plan_validates_indices_and_retains_progress_across_resume(self):
        identity = self.new_job()
        with self.assertRaises(ValueError):
            coordination.update_plan(identity, ["Research"], 0, [])
        ws.claim_job()
        coordination.update_plan(identity, ["Research", "Draft"], 1, [0])
        with self.assertRaises(ValueError):
            coordination.update_plan(identity, ["Research", "Draft"], 1, [1])
        ws.cancel_job(identity)
        ws.resume_job(identity)
        self.assertEqual(coordination.plan(identity)[0]["status"], "completed")

    def test_attention_prioritizes_stale_jobs_and_review_is_project_scoped(self):
        now = datetime.now(timezone.utc)
        stale = self.new_job(); ws.claim_job()
        with ws.connection() as db, db:
            db.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", ((now-timedelta(seconds=30)).isoformat(), stale))
        other = ws.create_project("Other")
        finished = self.new_job(other); ws.claim_job(); ws.finish_job(finished, "completed", "Done")
        items = coordination.attention("default", now)
        self.assertEqual([i["id"] for i in items], [stale])
        self.assertEqual(items[0]["priority"], 0)
        self.assertEqual(len(coordination.attention(other)), 1)
        coordination.review(finished)
        self.assertEqual(coordination.attention(other), [])
        with self.assertRaises(ValueError): coordination.review(stale)

    def test_handoff_preserves_decisions_sources_plan_and_originals(self):
        identity = self.new_job(); ws.claim_job()
        doc = ws.ingest("default", "Evidence", "24 GB memory", "verified source")
        ws.save_note("default", "Decision", "Keep two resident models", "decision", [doc["id"]])
        other = ws.create_project("Other")
        ws.save_note(other, "Secret", "Other project material")
        coordination.update_plan(identity, ["Compare", "Recommend"], 1, [0])
        result = coordination.handoff(identity)
        self.assertIn("Keep two resident models", result["markdown"])
        self.assertIn(doc["id"], result["markdown"])
        self.assertIn("[completed] Compare", result["markdown"])
        self.assertNotIn("Other project material", result["markdown"])
        self.assertTrue(ws.document_chunk("default", doc["id"], 1))
        self.assertEqual(len(ws.snapshot("default")["artifacts"]), 1)

    def pipe(self):
        pipe = pipe_module.Pipe()
        pipe.valves.OWNER_KEY = "k"*40
        pipe.valves.WAIT_SECONDS = 0
        self.project = ws.create_project("Friends")
        pipe.valves.PROJECT_ID = self.project
        app = console.create_app("k"*40, run_worker=False)
        pipe._client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                                headers={"Authorization": "Bearer " + "k"*40})
        return pipe

    def run_pipe(self, pipe, text, user=None, background=None):
        async def run():
            return "".join([part async for part in pipe.pipe(
                {"model": "model_hub.research-brief", "stream": True, "messages": [{"role": "user", "content": text}]},
                __user__=user or {"role": "admin"}, __task__=background)])
        return asyncio.run(run())

    def test_pipe_refuses_to_share_the_owners_default_project(self):
        pipe = self.pipe()
        for value in ("", "default"):
            pipe.valves.PROJECT_ID = value
            self.assertIn("PROJECT_ID", self.run_pipe(pipe, "Write a report"))
        self.assertEqual(ws.query("SELECT id FROM jobs"), [])

    def test_pipe_background_tasks_and_uninvited_users_never_create_jobs(self):
        pipe = self.pipe()
        self.run_pipe(pipe, "Write a report", background="title_generation")
        self.run_pipe(pipe, "Write a report", user={"role":"user", "email":"stranger@example.com"})
        self.assertEqual(ws.query("SELECT id FROM jobs"), [])

    def test_pipe_invited_user_can_start_check_cancel_resume_and_export(self):
        pipe = self.pipe(); pipe.valves.ALLOWED_EMAILS = "friend@example.com"
        output = self.run_pipe(pipe, "Compare these sources", user={"role":"user", "email":"FRIEND@example.com"})
        job = ws.query("SELECT * FROM jobs")[0]
        self.assertIn(job["id"], output)
        self.assertFalse(job["allow_frontier"])
        self.assertFalse(job["allow_images"])
        self.assertIn("Queued", self.run_pipe(pipe, "/hub status " + job["id"]))
        self.run_pipe(pipe, "/hub cancel " + job["id"])
        self.assertEqual(ws.query("SELECT status FROM jobs")[0]["status"], "cancelled")
        self.run_pipe(pipe, "/hub resume " + job["id"])
        self.assertEqual(ws.query("SELECT status FROM jobs")[0]["status"], "queued")
        self.assertIn("# Handoff", self.run_pipe(pipe, "/hub handoff " + job["id"]))

    def test_pipe_cannot_read_or_mutate_another_projects_job(self):
        pipe = self.pipe()
        other = ws.create_project("Other")
        identity = self.new_job(other)
        for command in ["status", "cancel", "resume", "handoff", "review"]:
            self.assertNotIn("Compare the supplied sources", self.run_pipe(pipe, f"/hub {command} {identity}"))
        self.assertEqual(ws.query("SELECT status FROM jobs WHERE id=?", (identity,))[0]["status"], "queued")
        self.assertEqual(ws.snapshot(other)["artifacts"], [])

    def test_pipe_returns_finished_answer_and_does_not_enqueue_again_on_status(self):
        pipe = self.pipe()
        identity = self.new_job(self.project); ws.claim_job(); ws.finish_job(identity, "completed", "Evidence-backed answer")
        self.assertIn("Evidence-backed answer", self.run_pipe(pipe, "/hub status " + identity))
        self.run_pipe(pipe, "/hub review " + identity)
        self.assertNotIn("Result ready for review", self.run_pipe(pipe, "/hub tasks"))
        self.assertEqual(len(ws.query("SELECT id FROM jobs")), 1)

    def test_pipe_waiting_stream_keeps_job_durable(self):
        pipe = self.pipe(); pipe.valves.WAIT_SECONDS = 1
        output = self.run_pipe(pipe, "A brief background task")
        self.assertIn('"delta": {}', output)
        self.assertIn("Working in the background", output)
        self.assertEqual(ws.query("SELECT status FROM jobs")[0]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
