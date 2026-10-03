"""Offline API/tool boundaries for audience feedback; no platform requests, posting or training."""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))

from agents.tool_context import ToolContext as InvocationContext
from fastapi.testclient import TestClient
import audience
import console
import research_tools
import store
import toolbox
import workspace as ws
import httpx2 as httpx

AUTH = {"Authorization": "Bearer " + "k" * 40}


class AudienceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = store.DATA
        store.DATA = Path(self.temp.name) / "data"
        ws.init()
        audience.init()
        research_tools.init()
        self.other = ws.create_project("Separate account")
        self.root = Path(self.temp.name) / "chat"
        self.root.mkdir()
        self.client = TestClient(console.create_app("k" * 40, run_worker=False))

    def tearDown(self):
        store.DATA = self.old_data
        self.temp.cleanup()

    def tools(self, project="default", owner=True, request_text=""):
        space = SimpleNamespace(is_owner=owner, dir=self.root, resolve=lambda p: self.root / p,
                                write_text=lambda p, text: (self.root / p).write_text(text, encoding="utf-8"))
        job = {"id": "offline-job", "project": project}
        with patch.object(toolbox, "ready", return_value=True):
            tools, _ = research_tools.build_tools(job, space, None, asyncio.Semaphore(1), lambda *args: None,
                                                 SimpleNamespace(active=lambda: None), request_text, "qwen-2")
        return {tool.name: tool for tool in tools}

    def invoke(self, tool, arguments):
        text = json.dumps(arguments)
        ctx = InvocationContext(context=None, tool_name=tool.name, tool_call_id="offline", tool_arguments=text)
        return asyncio.run(tool.on_invoke_tool(ctx, text))

    def post(self, project="default", status="draft"):
        with ws.connection() as db, db:
            return db.execute("""INSERT INTO social_posts(project,platform,kind,caption,media,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""", (project, "x", "text", "private caption", "[]", status,
                                                store.now(), store.now())).lastrowid

    def test_audience_routes_require_owner_auth_and_same_origin(self):
        for path in ("experiments", "performance", "preferences"):
            response = self.client.get("/api/audience/" + path)
            self.assertEqual(response.status_code, 401)
        with patch.object(audience, "create_experiment") as create:
            response = self.client.post("/api/audience/experiments", headers={**AUTH, "Origin": "https://untrusted.example"},
                                        json={"name": "Test", "brief": "Make a video"})
            self.assertEqual(response.status_code, 403)
            create.assert_not_called()

    def test_api_preserves_project_and_cannot_spoof_collector_source(self):
        with patch.object(audience, "create_experiment", return_value={"id": "exp-a"}) as create:
            response = self.client.post("/api/audience/experiments", headers=AUTH,
                                        json={"project": self.other, "name": "Hook", "brief": "Exact original brief",
                                              "context": "Similar audience", "split": "eval"})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(create.call_args.kwargs["project"], self.other)
            self.assertEqual(create.call_args.kwargs["split"], "eval")
        with patch.object(audience, "record_snapshot", return_value={"id": "snap"}) as record:
            response = self.client.post("/api/audience/variants/1/metrics", headers=AUTH,
                                        json={"project": self.other, "metrics": {"views": 1200, "shares": 18},
                                              "source": "tiktok_api", "observed_at": "2026-10-03T08:00:00+00:00"})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(record.call_args.kwargs["project"], self.other)
            self.assertEqual(record.call_args.kwargs["variant_id"], 1)
            self.assertEqual(record.call_args.kwargs["source"], "manual")

    def test_api_gets_keep_project_scoped_and_export_does_not_train(self):
        with patch.object(audience, "list_experiments", return_value=[{"id": "exp"}]) as listing:
            response = self.client.get("/api/audience/experiments", headers=AUTH, params={"project": self.other})
            self.assertEqual(response.json(), {"experiments": [{"id": "exp"}]})
            listing.assert_called_once_with(self.other)
        expected = {"records": [], "comparisons": [], "skipped": ["Too early"]}
        with patch.object(audience, "export_preferences", return_value=expected) as export:
            response = self.client.get("/api/audience/preferences", headers=AUTH,
                                       params={"project": self.other, "horizon_hours": 24, "min_views": 1000})
            self.assertEqual(response.json(), expected)
            export.assert_called_once_with(self.other, 24, 1000, 0.15)

    def test_api_lifecycle_accepts_numeric_ids_null_unknowns_and_blocks_other_projects(self):
        response = self.client.post("/api/audience/experiments", headers=AUTH,
                                    json={"name": "Compare hooks", "brief": "One controlled product video", "account": "open-id"})
        self.assertEqual(response.status_code, 201, response.text)
        experiment = response.json()["id"]
        response = self.client.post("/api/audience/variants", headers=AUTH,
                                    json={"experiment_id": experiment, "label": "Result first", "response": "Exact generated script"})
        self.assertEqual(response.status_code, 201, response.text)
        variant = response.json()["id"]
        published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        response = self.client.post(f"/api/audience/variants/{variant}/publication", headers=AUTH,
                                    json={"remote_id": "123456789", "url": "https://www.tiktok.com/@creator/video/123456789",
                                          "published_at": published, "account": "open-id"})
        self.assertEqual(response.status_code, 200, response.text)
        metrics = {"views": 1000, "shares": 20, "likes": None}
        response = self.client.post(f"/api/audience/variants/{variant}/metrics", headers=AUTH, json={"metrics": metrics})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["source"], "manual")
        self.assertIsNone(response.json()["metrics"]["likes"])
        forbidden = self.client.post(f"/api/audience/variants/{variant}/metrics", headers=AUTH,
                                     json={"project": self.other, "metrics": metrics})
        self.assertEqual(forbidden.status_code, 400)
        forbidden = self.client.get(f"/api/audience/experiments/{experiment}", headers=AUTH, params={"project": self.other})
        self.assertEqual(forbidden.status_code, 400)
        detail = self.client.get(f"/api/audience/experiments/{experiment}", headers=AUTH).json()
        self.assertEqual(len(detail["variants"]), 1)
        self.assertEqual(len(detail["checkpoints"]), 3)
        self.assertFalse(any("lease_token" in row for row in detail["checkpoints"]))
        empty = self.client.get("/api/audience/performance", headers=AUTH, params={"project": self.other}).json()
        self.assertEqual(empty["results"], [])

    def test_metrics_api_rejects_boolean_and_string_counts_before_recording(self):
        for views in (True, "1000"):
            with patch.object(audience, "record_snapshot") as record:
                response = self.client.post("/api/audience/variants/1/metrics", headers=AUTH, json={"metrics": {"views": views}})
                self.assertEqual(response.status_code, 422)
                record.assert_not_called()

    def test_performance_output_keeps_complete_records_when_responses_are_large(self):
        rows = [{"id": i, "label": "Variant " + str(i), "response": "script" * 10000,
                 "reward": {"score": 0.4}, "snapshots": [{"metrics": {"views": 1000}}]} for i in range(200)]
        view = json.loads(research_tools.performance_json({"results": rows, "summary": {"variants": len(rows)}}))
        self.assertTrue(view["results"])
        self.assertGreater(view["omitted"]["results"], 0)
        self.assertNotIn("response", view["results"][0])
        self.assertEqual(view["results"][0]["reward"]["score"], 0.4)

    def test_social_draft_reads_and_publication_cannot_cross_projects(self):
        private = self.post(self.other)
        public = self.post()
        tools = self.tools(request_text=f"approve post {private}")
        rows = json.loads(self.invoke(tools["list_posts"], {}))
        self.assertEqual([row["id"] for row in rows], [public])
        with patch.object(toolbox, "run_social", new_callable=AsyncMock) as publish:
            result = self.invoke(tools["publish_post"], {"post_id": private})
            self.assertIn("No draft", result)
            publish.assert_not_awaited()
        posts = self.client.get("/api/posts", headers=AUTH, params={"project": "default"}).json()["posts"]
        self.assertEqual([row["id"] for row in posts], [public])

    def test_tracking_and_confirmation_are_evidence_only(self):
        tools = self.tools()
        private = self.post(self.other)
        with patch.object(audience, "register_variant") as register:
            result = self.invoke(tools["track_content_variant"],
                                 {"experiment_id": 1, "label": "A", "response": "Script", "post_id": private})
            self.assertIn("Draft not found", result)
            register.assert_not_called()
        with patch.object(audience, "confirm_publication", return_value={"id": "v"}) as confirm, \
                patch.object(toolbox, "run_social", new_callable=AsyncMock) as publish:
            self.invoke(tools["confirm_post_published"], {"variant_id": 1, "remote_id": "123", "url": "https://www.tiktok.com/@me/video/123",
                                                        "published_at": "2026-10-03T00:00:00+00:00", "account": "me"})
            confirm.assert_called_once_with("default", 1, "123", "https://www.tiktok.com/@me/video/123",
                                            "2026-10-03T00:00:00+00:00", "me", "organic")
            publish.assert_not_awaited()

    def test_media_hashes_preserve_final_file_and_job_lineage(self):
        import hashlib
        media = self.root / "final.mp4"
        media.write_bytes(b"finished edit")
        tools = self.tools()
        with patch.object(audience, "register_variant", return_value={"id": "v"}) as register:
            self.invoke(tools["track_content_variant"], {"experiment_id": 1, "label": "Result first", "response": "Exact script",
                                                        "media_paths": ["final.mp4"], "model": "reviewed-version"})
            self.assertEqual(register.call_args.args[7], [hashlib.sha256(b"finished edit").hexdigest()])
            self.assertEqual(register.call_args.args[8], "offline-job")
            self.assertEqual(register.call_args.args[3], "Exact script")

    def test_guest_gets_only_project_performance_and_helpers_exclude_mutators(self):
        guest = self.tools(project=self.other, owner=False)
        self.assertIn("content_performance", guest)
        self.assertFalse(research_tools.OWNER_ONLY_TOOLS & set(guest))
        from cowork import tools as cowork_tools
        space = SimpleNamespace(is_owner=True, dir=self.root, resolve=lambda p: self.root / p)
        job = {"id": "offline-job", "project": "default", "profile": "fast", "allow_images": False,
               "allow_frontier": False, "task": "Test", "thread": "test"}
        ctx = cowork_tools.ToolContext(job, space, None, asyncio.Semaphore(1), SimpleNamespace(active=lambda: None),
                                       {"delegations": 0}, {"count": 0}, lambda *args: None, lambda *args: None,
                                       "qwen-1", "qwen-2")
        async def helper(*args, **kwargs):
            return "Offline helper"
        captured = []
        def helper_factory(context, helper_tools):
            captured.extend(t.name for t in helper_tools)
            return helper
        with patch.object(cowork_tools, "_run_helper", side_effect=helper_factory), \
                patch("phone_link.connected", return_value=False), patch.object(toolbox, "ready", return_value=True):
            cowork_tools.build_tools(ctx)
        self.assertIn("content_performance", captured)
        self.assertFalse(research_tools.OWNER_ONLY_TOOLS & set(captured))

    def test_preferences_write_complete_jsonl_and_audit_without_training(self):
        tools = self.tools()
        exported = {"records": [{"prompt": "Brief", "chosen": "Winner", "rejected": "Loser"}],
                    "comparisons": [{"experiment": "exp"}], "skipped": []}
        with patch.object(audience, "export_preferences", return_value=exported):
            result = json.loads(self.invoke(tools["export_content_preferences"], {}))
        self.assertFalse(result["training_started"])
        self.assertEqual(json.loads((self.root / result["path"]).read_text()), exported["records"][0])
        self.assertEqual(json.loads((self.root / result["audit"]).read_text()), exported)

    def test_tiktok_connection_keeps_token_private_and_identifies_open_id(self):
        values = toolbox.parse_connect("tiktok", ["oauth-open-id", "private-token"])
        self.assertEqual(values, {"account_id": "oauth-open-id", "access_token": "private-token"})
        connected = toolbox.save_credentials("tiktok", values)
        self.assertTrue(connected["tiktok"]["connected"])
        self.assertNotIn("private-token", json.dumps(connected))
        self.assertEqual(toolbox.credentials()["tiktok"]["account_id"], "oauth-open-id")

    def test_optional_oauth_credentials_parse_persist_and_remain_private(self):
        words = ["open-id", "@access-token", "refresh_token=@refresh-token", "client_key=application-key", "client_secret=@private-secret"]
        values = toolbox.parse_connect("tiktok", words)
        expected = {"account_id": "open-id", "access_token": "@access-token", "refresh_token": "@refresh-token",
                    "client_key": "application-key", "client_secret": "@private-secret"}
        self.assertEqual(values, expected)
        state = toolbox.save_credentials("tiktok", values)
        self.assertEqual({k: toolbox.credentials()["tiktok"][k] for k in expected}, expected)
        for secret in ("@access-token", "@refresh-token", "@private-secret", "application-key"):
            self.assertNotIn(secret, json.dumps(state))
        with self.assertRaisesRegex(ValueError, "together"):
            toolbox.save_credentials("tiktok", {"account_id": "open-id", "access_token": "new", "refresh_token": "partial"})
        self.assertEqual(toolbox.credentials()["tiktok"]["access_token"], "@access-token")
        # A manual reconnect deliberately removes automatic-renewal configuration.
        toolbox.save_credentials("tiktok", {"account_id": "open-id", "access_token": "manual-token"})
        self.assertNotIn("refresh_token", toolbox.credentials()["tiktok"])

    def test_rotation_is_atomic_preserves_other_accounts_and_respects_reconnects(self):
        values = {"account_id": "open-id", "access_token": "old-access", "refresh_token": "old-refresh",
                  "client_key": "app-key", "client_secret": "app-secret"}
        toolbox.save_credentials("tiktok", values)
        toolbox.save_credentials("github", {"token": "github-private"})
        expected = toolbox.credentials()["tiktok"]
        self.assertTrue(toolbox.rotate_tiktok_tokens(expected, "rotated-access", "rotated-refresh", "2026-10-04T12:00:00Z"))
        current = toolbox.credentials()
        self.assertEqual(current["tiktok"]["saved_at"], expected["saved_at"])
        self.assertEqual(current["tiktok"]["access_token"], "rotated-access")
        self.assertEqual(current["tiktok"]["expires_at"], "2026-10-04T12:00:00+00:00")
        self.assertEqual(current["github"]["token"], "github-private")
        self.assertFalse(toolbox.rotate_tiktok_tokens(expected, "stale-access", "stale-refresh"))
        before_reconnect = current["tiktok"]
        toolbox.save_credentials("tiktok", {"account_id": "new-account", "access_token": "reconnected-token"})
        self.assertFalse(toolbox.rotate_tiktok_tokens(before_reconnect, "stale-access", "stale-refresh"))
        self.assertEqual(toolbox.credentials()["tiktok"]["access_token"], "reconnected-token")
        toolbox.forget("tiktok")
        self.assertFalse(toolbox.rotate_tiktok_tokens(before_reconnect, "stale-access", "stale-refresh"))
        self.assertNotIn("tiktok", toolbox.credentials())
        self.assertEqual(toolbox.credentials()["github"]["token"], "github-private")

    def test_credential_write_failure_keeps_previous_file_and_removes_temporary(self):
        toolbox.save_credentials("tiktok", {"account_id": "open-id", "access_token": "old"})
        previous = toolbox.credentials()
        with patch.object(toolbox.os, "replace", side_effect=OSError("Storage failure")):
            with self.assertRaises(OSError):
                toolbox.save_credentials("github", {"token": "private-new-token"})
        self.assertEqual(toolbox.credentials(), previous)
        self.assertEqual(list(store.DATA.glob(".social-*.tmp")), [])

    def test_reconnect_resumes_only_matching_credential_failures_inside_window(self):
        now = datetime.now(timezone.utc)
        checkpoints = {}
        cases = [("retry", "tiktok", "open-id", 25, "credentials_expired"),
                 ("failed", "tiktok", "open-id", 25, "invalid_credentials"),
                 ("retry", "tiktok", "different-account", 25, "credentials_expired"),
                 ("retry", "instagram", "open-id", 25, "credentials_expired"),
                 ("retry", "tiktok", "open-id", 40, "credentials_expired"),
                 ("retry", "tiktok", "open-id", 25, "network_error"),
                 ("done", "tiktok", "open-id", 25, "credentials_expired"),
                 ("leased", "tiktok", "open-id", 25, "credentials_expired"),
                 ("missed", "tiktok", "open-id", 25, "credentials_expired")]
        for index, (status, platform, account, age, error) in enumerate(cases):
            experiment = audience.create_experiment("default", f"Case {index}", "Controlled brief", platform=platform, account=account)
            variant = audience.register_variant("default", experiment["id"], "A", "Script")
            remote_id = str(90000 + index)
            url = f"https://www.tiktok.com/@creator/video/{remote_id}" if platform == "tiktok" else f"https://www.instagram.com/reel/{remote_id}/"
            audience.confirm_publication("default", variant["id"], remote_id, url, (now - timedelta(hours=age)).isoformat(), account)
            checkpoint = ws.query("SELECT id FROM audience_checkpoints WHERE variant_id=? AND horizon_hours=24", (variant["id"],))[0]["id"]
            with ws.connection() as db, db:
                db.execute("UPDATE audience_checkpoints SET status=?,last_error=?,attempts=8,next_attempt_at=? WHERE id=?",
                           (status, error + ": safe message", (now + timedelta(hours=6)).isoformat(), checkpoint))
            checkpoints[index] = checkpoint
        with patch.object(audience, "_clock", return_value=now):
            self.assertEqual(audience.resume_credentials("tiktok", "open-id"), 2)
        for index, checkpoint in checkpoints.items():
            row = ws.query("SELECT * FROM audience_checkpoints WHERE id=?", (checkpoint,))[0]
            self.assertEqual(row["status"], "queued" if index < 2 else cases[index][0])
            self.assertEqual(row["attempts"], 0 if index < 2 else 8)

    def test_queue_failure_does_not_fail_a_successful_connection_or_echo_secrets(self):
        with patch.object(audience, "resume_credentials", side_effect=RuntimeError("fake-private-token")):
            response = self.client.post("/api/connections/social", headers=AUTH,
                                        json={"service": "tiktok", "words": ["open-id", "fake-private-token"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["accounts"]["tiktok"]["connected"])
        self.assertNotIn("fake-private-token", response.text)
        self.assertIn("audience_retry_warning", response.json())

    def pipe(self):
        spec = importlib.util.spec_from_file_location("audience_pipe_test", ROOT / "integrations" / "openwebui_cowork.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pipe = module.Pipe()
        pipe.valves.OWNER_KEY = "k" * 40
        pipe.valves.ALLOWED_EMAILS = "friend@example.com"
        pipe._client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=self.client.app), base_url="http://testserver",
                                                headers=AUTH)
        return pipe

    def pipe_reply(self, pipe, text, user=None):
        async def collect():
            chunks = []
            async for chunk in pipe.pipe({"messages": [{"role": "user", "content": text}]},
                                         __user__=user or {"role": "admin"}, __metadata__={"chat_id": "offline-test"}):
                chunks.append(chunk)
            return "".join(chunks)
        return asyncio.run(collect())

    def test_pipe_routes_tiktok_connection_to_owner_api_without_jobs_and_hides_credentials(self):
        pipe = self.pipe()
        text = "/connect tiktok open-id private-access refresh_token=private-refresh client_key=app-key client_secret=private-secret"
        with patch.object(pipe, "_call", wraps=pipe._call) as call:
            reply = self.pipe_reply(pipe, text)
        self.assertIn("tiktok", reply)
        self.assertIn("connected", reply)
        self.assertEqual([c.args[2] for c in call.await_args_list], ["/api/connections/social"])
        self.assertEqual(toolbox.credentials()["tiktok"]["refresh_token"], "private-refresh")
        self.assertEqual(ws.query("SELECT id FROM jobs"), [])
        for secret in ("private-access", "private-refresh", "private-secret", "app-key"):
            self.assertNotIn(secret, reply)
        with patch.object(pipe, "_call", wraps=pipe._call) as call:
            reply = self.pipe_reply(pipe, text, {"role": "user", "email": "friend@example.com"})
        self.assertIn("Only the owner", reply)
        call.assert_not_awaited()

    def test_pipe_connections_lists_tiktok_and_refresh_setup_without_model_work(self):
        pipe = self.pipe()
        toolbox.save_credentials("tiktok", {"account_id": "open-id", "access_token": "private-access"})
        with patch.object(pipe, "_call", wraps=pipe._call) as call:
            reply = self.pipe_reply(pipe, "/connections")
        self.assertIn("TikTok (analytics)", reply)
        self.assertIn("unattended checkpoints", reply)
        self.assertIn("refresh_token=", reply)
        self.assertNotIn("private-access", reply)
        self.assertNotIn("/api/jobs", [c.args[2] for c in call.await_args_list])
        self.assertEqual(ws.query("SELECT id FROM jobs"), [])

    def test_pipe_redacts_credentials_in_connection_validation_errors(self):
        pipe = self.pipe()
        text = "/connect tiktok open-id private-access refresh_token=private-refresh client_key=app-key client_secret=private-secret"
        with patch.object(pipe, "_call", new_callable=AsyncMock,
                          side_effect=ValueError("Controller rejected private-access and private-secret and private-refresh")):
            reply = self.pipe_reply(pipe, text)
        self.assertIn("Not saved", reply)
        self.assertIn("[redacted]", reply)
        for secret in ("private-access", "private-refresh", "private-secret"):
            self.assertNotIn(secret, reply)

    def test_metrics_worker_starts_and_stops_with_controller_lifespan(self):
        import audience_worker
        import telegram_bot
        started, stopped = threading.Event(), threading.Event()
        async def idle(*args, **kwargs):
            await asyncio.Event().wait()
        async def collector():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        with patch.object(toolbox, "install_in_background"), patch.object(console, "serve", side_effect=idle), \
                patch.object(telegram_bot, "serve", side_effect=idle), patch.object(audience_worker, "serve", side_effect=collector):
            with TestClient(console.create_app("k" * 40, run_worker=True)) as client:
                self.assertTrue(started.wait(2))
                self.assertEqual(client.get("/api/audience/performance", headers=AUTH).status_code, 200)
            self.assertTrue(stopped.wait(2))


if __name__ == "__main__":
    unittest.main()
