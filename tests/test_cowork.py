"""Cowork: sandboxed workspace, per-chat concurrency and a scripted end-to-end run with a fake model."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import store
import workspace as ws
import hub
import sandbox
import cowork
import escalate
import console
from openai import AsyncOpenAI
import httpx2 as httpx
from fastapi.testclient import TestClient


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.chmod(self.temp.name, 0o755)  # the sandbox user must be able to reach its folder
        self.old = (store.DATA, sandbox.ROOT, dict(sandbox.USERS))
        store.DATA = Path(self.temp.name) / "data"
        sandbox.ROOT = Path(self.temp.name) / "cowork"
        if sandbox.IS_ROOT:
            sandbox.USERS.update(owner="nobody", guest="nobody", friend="nobody")
        ws.init()
        cowork._leads.clear()

    def tearDown(self):
        store.DATA, sandbox.ROOT = self.old[0], self.old[1]
        sandbox.USERS.clear(); sandbox.USERS.update(self.old[2])
        self.temp.cleanup()


class SandboxTests(Base):
    def test_paths_cannot_leave_the_chat_folder(self):
        space = sandbox.Workspace("owner", "chat-1").prepare()
        for bad in ("../chat-2/x", "/etc/passwd", "../../guest/home/x"):
            with self.assertRaises(ValueError):
                space.resolve(bad)
        (space.dir / "link").symlink_to("/etc")
        with self.assertRaises(ValueError):
            space.read_text("link/passwd")
        self.assertIn("Wrote", space.write_text("out/report.md", "# Hi\nline two"))
        self.assertIn("line two", space.read_text("out/report.md"))
        self.assertIn("Edited", space.edit("out/report.md", "Hi", "Hello"))
        with self.assertRaises(ValueError):
            space.edit("out/report.md", "missing text", "x")
        self.assertIn("report.md", space.listing("."))
        with self.assertRaises(ValueError):
            sandbox.clean_thread("../..")  # becomes empty after cleaning

    def test_commands_run_unprivileged_with_output_and_timeout(self):
        space = sandbox.Workspace("guest", "chat-2").prepare()
        result = asyncio.run(space.run("id -u; pwd; echo $HOME; echo oops >&2; exit 3"))
        lines = result["output"].split()
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("oops", result["output"])
        self.assertEqual(Path(lines[1]).resolve(), space.dir.resolve())
        if sandbox.IS_ROOT:
            self.assertNotEqual(lines[0], "0")
            store.DATA.chmod(0o700)  # as on the box (the supervisor locks down /workspace/data)
            denied = asyncio.run(space.run(f"cat {store.DATA}/hub.db >/dev/null && echo READ || echo DENIED"))
            self.assertIn("DENIED", denied["output"])
        slow = asyncio.run(space.run("sleep 30 & sleep 30", timeout=1))
        self.assertTrue(slow["timed_out"])
        self.assertIn("omitted", sandbox.trim("x" * 50000))


class ConcurrencyTests(Base):
    def test_different_chats_run_together_but_one_chat_runs_in_order(self):
        a1 = ws.create_job("default", "first", "cowork", thread="chat-a")
        a2 = ws.create_job("default", "second", "cowork", thread="chat-a")
        b1 = ws.create_job("default", "other chat", "cowork", thread="chat-b")
        team = ws.create_job("default", "team job", "research-brief")
        claimed = [ws.claim_job()["id"] for _ in range(3)]
        self.assertEqual(set(claimed), {a1, b1, team})
        self.assertIsNone(ws.claim_job())  # a2 waits for a1
        ws.finish_job(a1, "completed", "ok")
        self.assertEqual(ws.claim_job()["id"], a2)
        with self.assertRaises(ValueError):
            ws.create_job("default", "x", "cowork", thread="../bad")

    def test_console_upload_events_and_connections(self):
        app = console.create_app("k" * 40, run_worker=False)
        client = TestClient(app)
        auth = {"Authorization": "Bearer " + "k" * 40}
        import base64
        r = client.post("/api/workspace/upload", headers=auth, json={"project": "friends", "thread": "chat-9", "name": "../../notes.txt",
                                                                    "content_b64": base64.b64encode(b"hello").decode()})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["path"], "uploads/notes.txt")
        self.assertEqual((sandbox.ROOT / "guest" / "threads" / "chat-9" / "uploads" / "notes.txt").read_bytes(), b"hello")
        job = client.post("/api/jobs", headers=auth, json={"project": "default", "task": "hi", "skill": "cowork", "thread": "chat-9",
                                                           "allow_frontier": True, "requested_by": "me@example.com"}).json()["id"]
        events = client.get(f"/api/jobs/{job}/events", headers=auth, params={"after": 0}).json()
        self.assertEqual(events["status"], "queued")
        self.assertTrue(events["events"])
        last = events["events"][-1]["id"]
        self.assertEqual(client.get(f"/api/jobs/{job}/events", headers=auth, params={"after": last}).json()["events"], [])
        self.assertEqual(ws.query("SELECT requested_by FROM jobs WHERE id=?", (job,))[0]["requested_by"], "me@example.com")
        self.assertIn("claude", client.get("/api/connections", headers=auth).json())
        r = client.post("/api/connections/claude", headers=auth, json={"token": "sk-ant-oat01-" + "b" * 40})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["signed_in"])
        self.assertEqual(client.post("/api/admin/code", headers=auth, json={"ref": "nothex"}).status_code, 400 if False else 422)
        import code_update
        async def fake_stage(ref): return {"staged": ref, "files": 1}
        with patch.object(code_update, "stage", fake_stage):
            r = client.post("/api/admin/code", headers=auth, json={"ref": "a" * 40})
        self.assertEqual(r.json()["staged"], "a" * 40, r.text)


def sse(delta, finish):
    chunk = lambda d, f=None: {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "qwen-1",
                               "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
    pieces = [chunk(delta), chunk({}, finish),
              {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "qwen-1", "choices": [],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="".join("data: " + json.dumps(p) + "\n\n" for p in pieces) + "data: [DONE]\n\n")


def calls(*items):
    return {"role": "assistant", "tool_calls": [
        {"index": i, "id": f"call-{name}-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(items)]}


class EndToEndTests(Base):
    def test_lead_plans_uses_shell_delegates_and_shares_a_file(self):
        script = [
            calls(("update_plan", {"steps": ["Write notes", "Check them", "Share"], "active_index": 0, "completed_indices": []}),
                  ("write_file", {"path": "notes.txt", "content": "alpha\nbeta\n"})),
            calls(("run_shell", {"command": "wc -l notes.txt", "timeout_seconds": 30})),
            calls(("delegate", {"brief": "Summarize notes.txt"})),
            calls(("share_file", {"path": "notes.txt"}),
                  ("update_plan", {"steps": ["Write notes", "Check them", "Share"], "active_index": -1, "completed_indices": [0, 1, 2]})),
            {"role": "assistant", "content": "Done: notes.txt has 2 lines."},
        ]
        seen = []

        def handler(request):
            body = json.loads(request.content)
            system = body["messages"][0]["content"]
            if system.startswith("You are a helper agent"):
                seen.append(("helper", body["model"]))
                return sse({"role": "assistant", "content": "The notes list alpha and beta."}, "stop")
            step = sum(1 for m in body["messages"] if m["role"] == "assistant")
            seen.append(("lead", body["model"], [m["content"] for m in body["messages"] if m["role"] == "tool"][-1:]))
            delta = script[step]
            return sse(delta, "tool_calls" if "tool_calls" in delta else "stop")

        async def scenario():
            client = AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1",
                                 http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            identity = ws.create_job("default", "Make notes", "cowork", "fast", thread="chat-e2e")
            job = ws.claim_job()
            with patch.object(hub, "async_client", return_value=client), patch.object(escalate, "available", return_value="off"):
                return identity, await cowork.run_job(job)

        identity, output = asyncio.run(scenario())
        self.assertEqual(output, "Done: notes.txt has 2 lines.")
        self.assertEqual([s[0] for s in seen].count("helper"), 1)
        self.assertEqual({s[1] for s in seen if s[0] == "lead"}, {"qwen-1"})
        shell_result = seen[2][2][0]
        self.assertIn("2 notes.txt", shell_result)
        artifacts = ws.query("SELECT name,media_type FROM artifacts WHERE job_id=?", (identity,))
        self.assertEqual(artifacts, [{"name": "notes.txt", "media_type": "text/plain"}])
        kinds = [e["kind"] for e in ws.query("SELECT kind FROM events WHERE job_id=? ORDER BY id", (identity,))]
        for kind in ("plan", "tool", "delegate", "delegate-done", "artifact", "usage"):
            self.assertIn(kind, kinds)
        self.assertEqual((sandbox.ROOT / "owner" / "threads" / "chat-e2e" / "notes.txt").read_text(), "alpha\nbeta\n")

    def test_guests_never_get_escalation_tools(self):
        job = {"id": "j" * 32, "project": "friends", "profile": "fast", "allow_frontier": 1, "allow_images": 1, "thread": "t"}
        ws.create_job("friends", "x", "cowork", thread="t")
        space = sandbox.Workspace("guest", "t").prepare()
        fake = AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1")
        with patch.object(escalate, "available", return_value=None):
            agent = cowork.build(job, fake, asyncio.Semaphore(1), space)
        names = {t.name for t in agent.tools}
        self.assertNotIn("ask_claude", names)
        self.assertIn("generate_image", names)
        owner = sandbox.Workspace("owner", "t").prepare()
        with patch.object(escalate, "available", return_value=None):
            names = {t.name for t in cowork.build({**job, "project": "default"}, fake, asyncio.Semaphore(1), owner).tools}
        self.assertTrue({"ask_claude", "ask_codex"} <= names)


if __name__ == "__main__":
    unittest.main()


class PipeTests(Base):
    def setUp(self):
        super().setUp()
        import importlib.util
        spec = importlib.util.spec_from_file_location("cowork_pipe", ROOT / "integrations" / "openwebui_cowork.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = console.create_app("k" * 40, run_worker=False)
        self.pipe = module.Pipe()
        self.pipe.valves.OWNER_KEY = "k" * 40
        self.pipe._client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                                      headers={"Authorization": "Bearer " + "k" * 40})

    def collect(self, body, user, **extra):
        async def run():
            statuses, chunks = [], []
            async def emit(event): statuses.append(event)
            async def finisher():
                for _ in range(100):
                    await asyncio.sleep(.05)
                    job = ws.claim_job()
                    if job:
                        coordination_plan(job["id"])
                        ws.event(job["id"], "tool", "Running: python make.py")
                        ws.write_artifact(job["project"], job["id"], "out.csv", b"a,b\n", "text/csv")
                        ws.finish_job(job["id"], "completed", "Here is your table.")
                        return job
            import coordination
            def coordination_plan(identity):
                coordination.update_plan(identity, ["Make table"], -1, [0])
            worker = asyncio.create_task(finisher())
            async for chunk in self.pipe.pipe(body, __user__=user, __metadata__={"chat_id": "chat-p"}, __event_emitter__=emit, **extra):
                chunks.append(chunk)
            return "".join(chunks), statuses, await worker
        return asyncio.run(run())

    def test_owner_task_streams_status_and_returns_result_with_plan(self):
        body = {"messages": [{"role": "user", "content": "earlier question"}, {"role": "assistant", "content": "earlier answer"},
                             {"role": "user", "content": "Make me a table"}]}
        reply, statuses, job = self.collect(body, {"role": "admin", "email": "me@example.com", "id": "u1"})
        self.assertIn("Here is your table.", reply)
        self.assertIn("Task list (1/1 done)", reply)
        self.assertIn("out.csv", reply)
        self.assertIn("Running: python make.py", [s["data"]["description"] for s in statuses])
        self.assertEqual((job["project"], job["thread"], job["allow_frontier"], job["requested_by"]), ("default", "chat-p", 1, "me@example.com"))
        self.assertIn("earlier answer", job["task"])
        self.assertTrue(job["task"].rstrip().endswith("Make me a table"))

    def test_friends_use_their_own_project_and_strangers_are_refused(self):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        reply, _, _ = self.collect(body, {"role": "user", "email": "stranger@example.com"})
        self.assertIn("invited", reply)
        self.pipe.valves.ALLOWED_EMAILS = "friend@example.com"
        reply, _, job = self.collect(body, {"role": "user", "email": "Friend@example.com"})
        # Each friend has their own project and sandbox account.
        self.assertEqual((job["project"], job["allow_frontier"]), (sandbox.friend_account("friend@example.com"), 0))
        self.assertEqual(cowork.tier_for(job), job["project"])
        reply, _, _ = self.collect({"messages": [{"role": "user", "content": "/connect codex"}]}, {"role": "user", "email": "friend@example.com"})
        self.assertIn("Only the owner", reply)


class ConnectionTests(Base):
    def test_claude_token_from_chat_is_validated_and_stored_privately(self):
        with self.assertRaises(ValueError):
            escalate.save_claude_token("not-a-token")
        escalate.save_claude_token("sk-ant-oat01-" + "a" * 40)
        self.assertEqual(oct(escalate.token_file().stat().st_mode & 0o777), "0o600")
        self.assertEqual(escalate.claude_env()["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-" + "a" * 40)


class OutOfTurnsTests(Base):
    def test_helper_that_runs_out_of_steps_still_reports_back(self):
        def handler(request):
            body = json.loads(request.content)
            system = body["messages"][0]["content"]
            last = body["messages"][-1]
            if last["role"] == "user" and last["content"] == cowork.WRAP_UP:
                return sse({"role": "assistant", "content": "Partial report: found A and B."}, "stop")
            if system.startswith("You are a helper agent"):
                return sse(calls(("list_files", {"path": "."})), "tool_calls")
            tools = [m for m in body["messages"] if m["role"] == "tool"]
            if not tools:
                return sse(calls(("delegate", {"brief": "Research forever"})), "tool_calls")
            return sse({"role": "assistant", "content": "Lead got: " + tools[-1]["content"]}, "stop")

        async def scenario():
            client = AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1",
                                 http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            ws.create_job("default", "Research", "cowork", "fast", thread="chat-turns")
            job = ws.claim_job()
            with patch.object(hub, "async_client", return_value=client), patch.object(escalate, "available", return_value="off"):
                return await cowork.run_job(job)
        self.assertEqual(asyncio.run(scenario()), "Lead got: Partial report: found A and B.")


class CodeUpdateTests(unittest.TestCase):
    def test_archive_extraction_keeps_only_app_folders_and_refuses_escapes(self):
        import io, tarfile, code_update
        def archive(entries):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz") as tar:
                for name, content in entries:
                    info = tarfile.TarInfo(name); info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
            return data.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            count = code_update.extract(archive([("repo-abc/agents/console.py", b"x"), ("repo-abc/gateway/app.js", b"y"),
                                                 ("repo-abc/README.md", b"z")]), Path(folder))
            self.assertEqual(count, 1)
            self.assertTrue((Path(folder) / "agents" / "console.py").is_file())
            self.assertFalse((Path(folder) / "gateway").exists())
            with self.assertRaises(ValueError):
                code_update.extract(archive([("repo-abc/agents/../../../etc/x", b"bad")]), Path(folder))


class ConsoleOperationsTests(Base):
    def test_owner_signed_in_through_access_needs_no_key_and_sees_everything(self):
        import access, base64 as b64, json as js, time as tm
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = key.public_key().public_numbers()
        enc = lambda raw: b64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        jwk = {"kid": "k1", "kty": "RSA", "n": enc(numbers.n.to_bytes(256, "big")), "e": enc(numbers.e.to_bytes(3, "big"))}
        def token(email, aud="aud-console", exp=None):
            head = enc(js.dumps({"alg": "RS256", "kid": "k1"}).encode())
            body = enc(js.dumps({"aud": [aud], "email": email, "iss": "https://team.cloudflareaccess.com",
                                 "iat": int(tm.time()), "exp": exp or int(tm.time()) + 600}).encode())
            sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
            return f"{head}.{body}.{enc(sig)}"
        with patch.multiple(access, TEAM="team", AUDIENCE="aud-console", OWNER="me@example.com"), \
             patch.object(access, "_public_keys", return_value={"k1": jwk}):
            self.assertIsNotNone(access.verify(token("Me@example.com")))
            self.assertIsNone(access.verify(token("friend@example.com")))
            self.assertIsNone(access.verify(token("me@example.com", aud="other-app")))
            self.assertIsNone(access.verify(token("me@example.com", exp=int(tm.time()) - 5)))
            forged = token("friend@example.com").split(".")
            forged[1] = token("me@example.com").split(".")[1]
            self.assertIsNone(access.verify(".".join(forged)))
            client = TestClient(console.create_app("k" * 40, run_worker=False))
            self.assertEqual(client.get("/api/overview").status_code, 401)
            ok = client.get("/api/overview", headers={"Cf-Access-Jwt-Assertion": token("me@example.com")})
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(client.get("/api/overview", headers={"Cf-Access-Jwt-Assertion": token("friend@example.com")}).status_code, 401)
        auth = {"Authorization": "Bearer " + "k" * 40}
        mine = ws.create_job("default", "my task", "cowork", thread="chat-1", requested_by="me@example.com")
        theirs = ws.create_job("friends", "friend task", "cowork", thread="chat-2", requested_by="friend@example.com")
        ws.event(theirs, "tool", "Running: ls")
        ws.event(theirs, "usage", json.dumps({"requests": 3, "input_tokens": 100, "output_tokens": 20}))
        ws.save_note("friends", "Likes short hooks", "Under 3 seconds", "preference")
        space = sandbox.Workspace("guest", "chat-2").prepare()
        space.write_text("report.md", "# hi")
        activity = client.get("/api/activity", headers=auth).json()["jobs"]
        self.assertEqual({j["id"] for j in activity}, {mine, theirs})
        people = {p["who"]: p for p in client.get("/api/overview", headers=auth).json()["people"]}
        self.assertEqual((people["friend@example.com"]["tasks"], people["friend@example.com"]["input_tokens"]), (1, 100))
        timeline = client.get(f"/api/jobs/{theirs}/timeline", headers=auth).json()
        self.assertIn("tool", [e["kind"] for e in timeline["events"]])
        files = client.get("/api/workspace/files", headers=auth, params={"project": "friends", "thread": "chat-2"}).json()["files"]
        self.assertEqual(files, [{"path": "report.md", "bytes": 4}])
        self.assertEqual(client.get("/api/workspace/file", headers=auth, params={"project": "friends", "thread": "chat-2", "path": "report.md"}).content, b"# hi")
        self.assertEqual(client.get("/api/workspace/file", headers=auth, params={"project": "friends", "thread": "chat-2", "path": "../../../x"}).status_code, 400)
        memories = client.get("/api/memories", headers=auth).json()["memories"]
        self.assertEqual(memories[0]["project_name"], "Shared with invited friends")
        self.assertTrue(client.post(f"/api/memories/{memories[0]['id']}/delete", headers=auth).json()["deleted"])
