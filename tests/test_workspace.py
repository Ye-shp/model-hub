"""Offline regressions. Temporary databases, mocked models, no paid API calls."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import store
import workspace as ws
import skills
import fleet
import hub
import crew
import worker
import console
from agents import Agent, ModelSettings, Runner, RunContextWrapper
from agents.tool_context import ToolContext
from openai import AsyncOpenAI
import httpx2 as httpx
from fastapi.testclient import TestClient


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = store.DATA
        store.DATA = Path(self.temp.name)
        ws.init()

    def tearDown(self):
        store.DATA = self.old_data
        self.temp.cleanup()

    def test_same_post_after_a_failed_swipe_is_not_saved_twice(self):
        db = store.connect()
        post = {"creator": "@a", "caption": "Same caption", "device_serial": "P1"}
        first = store.save_post(db, "tiktok", {**post, "capture_hash": "x1"}, "", "default")
        again = store.save_post(db, "tiktok", {**post, "capture_hash": "x2"}, "", "default")
        other_device = store.save_post(db, "tiktok", {**post, "device_serial": "P2", "capture_hash": "x3"}, "", "default")
        self.assertIsNotNone(first); self.assertIsNone(again); self.assertIsNotNone(other_device)
        db.close()

    def job(self, **kwargs):
        identity = ws.create_job("default", "Use the project evidence", "research-brief", **kwargs)
        return identity, ws.claim_job()

    def test_same_caption_different_posts_are_not_discarded(self):
        with ws.connection() as db:
            a = store.save_post(db, "tiktok", {"creator":"same", "caption":"#fyp", "capture_hash":"frame-A"}, "")
            b = store.save_post(db, "tiktok", {"creator":"same", "caption":"#fyp", "capture_hash":"frame-B"}, "")
            repeated = store.save_post(db, "tiktok", {"creator":"same", "caption":"changed", "capture_hash":"frame-A"}, "")
        self.assertNotEqual(a, b)
        self.assertIsNone(repeated)

    def test_absent_identity_preserves_both_observations(self):
        with ws.connection() as db:
            self.assertTrue(store.save_post(db, "instagram", {"caption":"same"}, ""))
            self.assertTrue(store.save_post(db, "instagram", {"caption":"same"}, ""))

    def test_explicit_source_id_deduplicates_across_changed_frames(self):
        with ws.connection() as db:
            self.assertTrue(store.save_post(db,"tiktok",{"source_id":"123", "capture_hash":"a"},""))
            self.assertIsNone(store.save_post(db,"tiktok",{"source_id":"123", "capture_hash":"b"},""))

    def test_migration_preserves_v2_rows(self):
        legacy = store.DATA / "legacy"
        legacy.mkdir()
        db = sqlite3.connect(legacy / "hub.db")
        db.executescript(store.SCHEMA)
        db.execute("INSERT INTO posts(platform,fingerprint,caption,collected_at) VALUES ('tiktok','legacy','keep me','2026-09-27T01:00:00+00:00')")
        db.commit(); db.close()
        with patch.object(store, "DATA", legacy):
            ws.init()
            row = ws.query("SELECT caption,project,source_url FROM posts")[0]
            self.assertEqual(row, {"caption":"keep me", "project":"default", "source_url":None})

    def test_timestamp_comparison_uses_actual_times(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        with ws.connection() as db, db:
            for age, title in [(2, "recent"), (30, "too old")]:
                db.execute("INSERT INTO posts(platform,fingerprint,caption,collected_at,project) VALUES ('tiktok',?,?,?,'default')",
                           (title, title, (now-timedelta(hours=age)).isoformat()))
        identity, job = self.job()
        team = crew.build_team(job, object(), asyncio.Semaphore(2))
        tool = next(t for t in team.tools if t.name == "recent_posts")
        arguments = '{"platform":"any","hours":24,"limit":20}'
        context = ToolContext(context=None, tool_name="recent_posts", tool_call_id="test", tool_arguments=arguments)
        value = asyncio.run(tool.on_invoke_tool(context, arguments))
        self.assertIn("recent", value)
        self.assertNotIn("too old", value)

    def test_counts_handle_unreadable_strings(self):
        self.assertEqual(store.count_text("12.3K"), 12300)
        self.assertIsNone(store.count_text("..."))
        self.assertIsNone(store.count_text(float("nan")))

    def test_search_is_scoped_and_duplicate_import_is_idempotent(self):
        other = ws.create_project("Other")
        first = ws.ingest("default", "GPU report", "The GPU has 24 GB of VRAM", "my source")
        repeated = ws.ingest("default", "GPU report", "The GPU has 24 GB of VRAM", "my source")
        ws.ingest(other, "Private report", "GPU private project secret", "private")
        self.assertEqual(first["id"], repeated["id"])
        self.assertTrue(repeated["duplicate"])
        results = ws.search("default", 'GPU OR " VRAM')
        self.assertEqual(len(results), 1)
        self.assertNotIn("secret", results[0]["content"])
        self.assertEqual(ws.document_chunk(other, first["id"], 1), [])

    def test_bounded_json_remains_valid_with_large_records(self):
        data = json.loads(ws.bounded_json([{"x":"a"},{"x":"b"*20000}], 500))
        self.assertEqual(data["items"], [{"x":"a"}])
        self.assertEqual(data["omitted"], 1)

    def test_memory_updates_only_its_own_project(self):
        other = ws.create_project("Other")
        ws.save_note("default", "Budget", "300", "preference")
        ws.save_note(other, "Budget", "900", "preference")
        ws.save_note("default", "Budget", "250", "preference")
        self.assertEqual(ws.memories("default")[0]["content"], "250")
        self.assertEqual(ws.memories(other)[0]["content"], "900")

    def test_claim_serializes_project_and_allows_independent_projects(self):
        other = ws.create_project("Other")
        a, claimed = self.job()
        b = ws.create_job("default", "second", "research-brief")
        self.assertIsNone(ws.claim_job())
        c = ws.create_job(other, "independent", "research-brief")
        self.assertEqual(ws.claim_job()["id"], c)
        ws.finish_job(a, "completed")
        self.assertEqual(ws.claim_job()["id"], b)

    def test_cancel_wins_over_completion_and_recovery_is_explicit(self):
        identity, job = self.job()
        self.assertTrue(ws.cancel_job(identity))
        self.assertFalse(ws.finish_job(identity, "completed", "late answer"))
        self.assertTrue(ws.resume_job(identity))
        ws.claim_job()
        self.assertEqual(ws.recover_jobs(), 1)
        self.assertEqual(ws.query("SELECT status FROM jobs WHERE id=?", (identity,))[0]["status"], "interrupted")

    def test_artifact_names_cannot_escape_project_directory(self):
        artifact = ws.write_artifact("default", None, "../../escape.md", b"report")
        path = Path(ws.query("SELECT path FROM artifacts WHERE id=?", (artifact["id"],))[0]["path"])
        self.assertTrue(path.is_relative_to(store.DATA / "artifacts" / "default"))

    def test_fleet_rejects_duplicate_devices(self):
        with self.assertRaises(ValueError):
            fleet.plan({"devices":[{"serial":"A"},{"serial":"A"}]})
        self.assertIn("--serial", fleet.plan({"devices":[{"serial":"A"}]})[0])

    def test_all_skills_are_loadable_and_traversal_is_rejected(self):
        self.assertEqual(len(skills.catalog()), 7)
        with self.assertRaises(ValueError):
            skills.load_skill("../../outside")

    def test_console_auth_host_origin_and_project_creation(self):
        app = console.create_app("k"*40, run_worker=False)
        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/api/state").status_code, 401)
            headers={"Authorization":"Bearer "+"k"*40}
            self.assertEqual(client.get("/api/state", headers=headers).status_code, 200)
            self.assertEqual(client.get("/api/state", headers={**headers,"Host":"evil.example"}).status_code, 403)
            self.assertEqual(client.post("/api/projects",headers={**headers,"Origin":"https://evil.example"},json={"name":"X"}).status_code,403)
            project=client.post("/api/projects",headers=headers,json={"name":"New","brief":"Keep it concise"}).json()["id"]
            self.assertTrue(ws.project_exists(project))
            queued=client.post("/api/jobs",headers=headers,json={"project":project,"task":"Compare my notes"})
            self.assertEqual(queued.status_code,201)
            self.assertEqual(client.post("/api/jobs",headers=headers,json={"task":"x","skill":"../../bad"}).status_code,400)

    def test_frontier_allowance_survives_resume(self):
        identity, job = self.job(allow_frontier=True)
        with patch.object(hub,"FRONTIER_MODEL","anthropic/test"):
            budget=crew.CallBudget(job)
            budget.before("anthropic/test"); budget.before("anthropic/test")
            with self.assertRaises(RuntimeError):
                crew.CallBudget(job).before("anthropic/test")

    def test_stream_adapter_preserves_tool_calls_and_usage(self):
        async def scenario():
            requests=[]
            def handler(request):
                body=json.loads(request.content);requests.append(body)
                has_tool=any(m["role"]=="tool" for m in body["messages"])
                delta={"role":"assistant","content":"Evidence saved."} if has_tool else {"role":"assistant","tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"remember","arguments":json.dumps({"title":"Test evidence","content":"The report says 24 GB","kind":"fact","sources":["document:test"]})}}]}
                def chunk(d, finish=None):
                    return {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"qwen-1","choices":[{"index":0,"delta":d,"finish_reason":finish}]}
                pieces=[chunk(delta),chunk({},"stop" if has_tool else "tool_calls"),{"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"qwen-1","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}]
                text=': connected\n\n'+''.join('data: '+json.dumps(x)+'\n\n' for x in pieces)+'data: [DONE]\n\n'
                return httpx.Response(200,headers={"content-type":"text/event-stream"},content=text)
            client=AsyncOpenAI(api_key="test",base_url="https://fake.invalid/v1",http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            identity,job=self.job()
            with patch.object(hub,"async_client",return_value=client):
                output=await crew.run_job(job)
            self.assertEqual(output,"Evidence saved.")
            self.assertEqual(len(requests),2)
            self.assertTrue(all(r["stream"] for r in requests))
            self.assertEqual(ws.memories("default")[0]["title"],"Test evidence")
            self.assertTrue((store.DATA/'sessions.db').exists())
        asyncio.run(scenario())

    def test_worker_finishes_with_artifact_and_persistent_checkpoint(self):
        async def scenario():
            identity,job=self.job()
            async def runner(job,gate):return "A verified result"
            await worker.execute(job,asyncio.Semaphore(2),runner)
            self.assertEqual(ws.query("SELECT status FROM jobs WHERE id=?",(identity,))[0]["status"],"completed")
            self.assertTrue(ws.snapshot("default")["artifacts"])
            self.assertTrue(ws.memories("default"))
        asyncio.run(scenario())

    def test_worker_cancellation_stops_inflight_work(self):
        async def scenario():
            identity,job=self.job(); stopped=asyncio.Event()
            async def runner(job,gate):
                try:await asyncio.sleep(20)
                finally:stopped.set()
            task=asyncio.create_task(worker.execute(job,asyncio.Semaphore(2),runner))
            await asyncio.sleep(.05);ws.cancel_job(identity)
            await asyncio.wait_for(task,3)
            self.assertTrue(stopped.is_set())
            self.assertEqual(ws.query("SELECT status FROM jobs WHERE id=?",(identity,))[0]["status"],"cancelled")
        asyncio.run(scenario())

    def test_image_adapter_uses_auth_and_returns_openai_shape(self):
        spec=importlib.util.spec_from_file_location('image_server',ROOT/'services'/'image_server.py')
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        from PIL import Image
        calls=[]
        def render(prompt,w,h,**options):
            calls.append((prompt,w,h,options)); return Image.new('RGB',(8,8),'green')
        app=module.create_app(render,"k"*40)
        auth={"Authorization":"Bearer "+"k"*40}
        with TestClient(app) as client:
            self.assertEqual(client.post('/v1/images/generations',json={"prompt":"test"}).status_code,401)
            response=client.post('/v1/images/generations',headers=auth,json={"prompt":"test"})
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.json()['data'][0]['b64_json'])
            tall=client.post('/v1/images/generations',headers=auth,json={"prompt":"reel cover","size":"1152x2048","steps":20,"seed":7})
            self.assertEqual(tall.status_code,200)
            self.assertEqual(calls[-1][1:3],(1152,2048)); self.assertEqual(calls[-1][3]["steps"],20); self.assertEqual(calls[-1][3]["seed"],7)
            self.assertEqual(client.post('/v1/images/generations',headers=auth,json={"prompt":"x","size":"999x999"}).status_code,400)
            self.assertEqual(client.post('/v1/images/generations',headers=auth,json={"prompt":"x","steps":500}).status_code,422)
        loading=module.create_app(None,"k"*40)
        with TestClient(loading) as client:
            self.assertEqual(client.get('/health').json()["phase"],"starting")
            self.assertEqual(client.post('/v1/images/generations',headers=auth,json={"prompt":"x"}).status_code,503)


if __name__ == "__main__":
    unittest.main()
