"""Mid-task questions (ask_user) through the controller, the chat site and Telegram; the owner's MCP servers and APIs."""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "tests"))
import workspace as ws
import hub
import sandbox
import cowork
import escalate
import console
import asking
import connectors
import telegram_bot
import httpx2 as httpx
from openai import AsyncOpenAI
from fastapi.testclient import TestClient
from test_cowork import Base, sse, calls
from test_upgrades import AUTH


def run(coro):
    return asyncio.run(coro)


def running_job(thread="chat-q", project="default"):
    identity = ws.create_job(project, "CURRENT REQUEST:\nPlan my launch", "cowork", "fast", thread=thread)
    job = ws.claim_job()
    assert job["id"] == identity
    return job


class AskingTests(Base):
    def test_questions_answers_and_option_numbers(self):
        job = running_job()
        first = asking.ask(job["id"], "Which platform first?", ["TikTok", "Instagram", ""], wait_minutes=500)
        self.assertEqual((first["options"], first["wait_minutes"]), (["TikTok", "Instagram"], asking.MAX_WAIT_MINUTES))
        self.assertEqual(asking.pending_in_thread("default", "chat-q")[1]["id"], first["id"])
        second = asking.ask(job["id"], "Actually: budget?", ["$500", "$2,000"])
        self.assertEqual(asking.get(first["id"])["status"], "replaced")
        answered = asking.answer(job["id"], "2")
        self.assertEqual((answered["status"], answered["answer"]), ("answered", "$2,000"))
        self.assertIsNone(asking.pending(job["id"]))
        with self.assertRaises(ValueError):
            asking.answer(job["id"], "again")
        self.assertIn("2. $2,000", asking.show(second))
        kinds = [e["kind"] for e in ws.query("SELECT kind FROM events WHERE job_id=?", (job["id"],))]
        self.assertTrue({"question", "answer"} <= set(kinds))

    def test_unanswered_questions_expire(self):
        job = running_job()
        question = asking.ask(job["id"], "Anything?", wait_minutes=1)
        clock = iter([0, 30, 61, 61, 61])
        with patch.object(asking, "_clock", lambda: next(clock)):
            result = run(asking.wait(question["id"], lambda: None, poll=0))
        self.assertEqual(result["status"], "expired")

    def test_the_task_waits_for_the_answer_without_using_its_time_limit(self):
        script = [calls(("ask_user", {"question": "Which colour?", "options": ["Blue", "Red"]})),
                  {"role": "assistant", "content": "Made it blue."}]
        seen = []

        def handler(request):
            body = json.loads(request.content)
            step = sum(1 for m in body["messages"] if m["role"] == "assistant")
            seen.append([m["content"] for m in body["messages"] if m["role"] == "tool"])
            return sse(script[step], "tool_calls" if "tool_calls" in script[step] else "stop")

        async def scenario():
            client = AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1",
                                 http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            ws.create_job("default", "CURRENT REQUEST:\nMake a logo", "cowork", "fast", thread="chat-ask")
            job = ws.claim_job()

            async def user():
                while not asking.pending(job["id"]):
                    await asyncio.sleep(.05)
                await asyncio.sleep(2.5)  # longer than the whole task's 2-second limit
                asking.answer(job["id"], "1")

            replying = asyncio.create_task(user())
            with patch.dict(cowork.PROFILES["fast"], seconds=2), patch.object(asking, "wait", fast_wait), \
                    patch.object(hub, "async_client", return_value=client), patch.object(escalate, "available", return_value="off"):
                answer = await cowork.run_job(job)
            await replying
            return answer

        self.assertEqual(run(scenario()), "Made it blue.")
        self.assertEqual(seen[-1][-1], "The user answered: Blue")

    def test_cowork_offers_ask_user_to_the_lead_only(self):
        job = running_job("t")
        space = sandbox.Workspace("owner", "t").prepare()
        fake = AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1")
        with patch.object(escalate, "available", return_value="off"):
            agent = cowork.build(job, fake, asyncio.Semaphore(1), space)
        self.assertIn("ask_user", {t.name for t in agent.tools})
        self.assertIn("ASKING:", agent.instructions)


async def fast_wait(question_id, still_running, poll=2.0):
    return await ORIGINAL_WAIT(question_id, still_running, poll=.05)


ORIGINAL_WAIT = asking.wait


class ControllerAndPipeTests(Base):
    def test_console_reports_and_answers_questions(self):
        job = running_job("chat-c")
        client = TestClient(console.create_app("k" * 40, run_worker=False))
        asking.ask(job["id"], "Which audience?", ["Students", "Founders"])
        events = client.get(f"/api/jobs/{job['id']}/events", headers=AUTH).json()
        self.assertEqual(events["question"]["question"], "Which audience?")
        active = client.get("/api/thread/active", headers=AUTH, params={"project": "default", "thread": "chat-c"}).json()
        self.assertEqual(active["question"]["options"], ["Students", "Founders"])
        reply = client.post(f"/api/jobs/{job['id']}/answer", headers=AUTH, json={"answer": "2"})
        self.assertEqual(reply.json()["answer"], "Founders")
        self.assertEqual(client.post(f"/api/jobs/{job['id']}/answer", headers=AUTH, json={"answer": "x"}).status_code, 400)
        self.assertIsNone(client.get(f"/api/jobs/{job['id']}/events", headers=AUTH).json()["question"])

    def pipe(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cowork_pipe_ask", ROOT / "integrations" / "openwebui_cowork.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = console.create_app("k" * 40, run_worker=False)
        pipe = module.Pipe()
        pipe.valves.OWNER_KEY = "k" * 40
        pipe._client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                                 headers={"Authorization": "Bearer " + "k" * 40})
        return pipe

    def test_the_chat_shows_the_question_and_the_next_message_answers_it(self):
        pipe = self.pipe()
        user = {"role": "admin", "email": "me@example.com", "id": "u1"}

        async def say(text, during=None):
            chunks = []
            helper = asyncio.create_task(during()) if during else None
            async for chunk in pipe.pipe({"messages": [{"role": "user", "content": text}]}, __user__=user,
                                         __metadata__={"chat_id": "chat-p"}, __event_emitter__=lambda e: asyncio.sleep(0)):
                chunks.append(chunk)
            if helper:
                await helper
            return "".join(chunks)

        state = {}

        async def task_asks():
            for _ in range(200):
                await asyncio.sleep(.02)
                job = ws.claim_job()
                if job:
                    state["job"] = job
                    asking.ask(job["id"], "Launch on TikTok or Instagram?", ["TikTok", "Instagram"])
                    return

        async def task_finishes():
            for _ in range(200):
                await asyncio.sleep(.02)
                question = ws.query("SELECT * FROM questions WHERE job_id=?", (state["job"]["id"],))[0]
                if question["status"] == "answered":
                    ws.finish_job(state["job"]["id"], "completed", f"Plan done for {question['answer']}.")
                    return

        async def scenario():
            first = await say("Plan my launch", task_asks)
            second = await say("1", task_finishes)
            return first, second

        first, second = run(scenario())
        self.assertIn("Cowork has a question", first)
        self.assertIn("1. TikTok", first)
        self.assertIn("Plan done for TikTok.", second)
        self.assertEqual(ws.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 1)  # the answer didn't start a new task


class FakeTelegram:
    def __init__(self):
        self.calls = []

    async def __call__(self, token, method, data=None, files=None, timeout=40):
        self.calls.append((method, data or {}))
        return {"username": "hub_bot"} if method == "getMe" else True


class TelegramAskTests(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeTelegram()
        patcher = patch.object(telegram_bot, "api", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        state = run(telegram_bot.connect("123456789:" + "A" * 35))
        run(telegram_bot.handle(self.message(f"/start {state['pairing_code']}")))

    def message(self, text):
        return {"message_id": 1, "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text}

    def test_questions_go_to_telegram_and_the_reply_answers_them(self):
        job = running_job("tg-42-1")
        asking.ask(job["id"], "Which hook style?", ["Question", "Bold claim"])

        async def scenario():
            watcher = asyncio.create_task(telegram_bot.watch(job["id"], 42, every=.02))
            for _ in range(100):
                if any("Which hook style?" in d.get("text", "") for m, d in self.fake.calls if m == "sendMessage"):
                    break
                await asyncio.sleep(.02)
            await telegram_bot.handle(self.message("Bold claim"))
            self.assertEqual(asking.get(1)["answer"], "Bold claim")
            ws.finish_job(job["id"], "completed", "Here are 5 bold-claim hooks.")
            await asyncio.wait_for(watcher, 5)

        run(scenario())
        sent = [d for m, d in self.fake.calls if m == "sendMessage"]
        question = next(d for d in sent if "Which hook style?" in d["text"])
        self.assertEqual(question["reply_markup"]["keyboard"], [[{"text": "Question"}], [{"text": "Bold claim"}]])
        self.assertEqual(sum("Which hook style?" in d["text"] for d in sent), 1)  # asked once, not on every poll
        self.assertTrue(any(d.get("reply_markup") == {"remove_keyboard": True} for d in sent))
        self.assertIn("bold-claim hooks", sent[-1]["text"])
        self.assertEqual(ws.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 1)

    def test_connect_commands_stay_off_telegram(self):
        run(telegram_bot.handle(self.message("/connect api stripe https://api.stripe.com bearer=sk_live_x")))
        self.assertIn("from a Cowork chat on the web", self.fake.calls[-1][1]["text"])
        self.assertEqual(ws.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 0)


MCP_SERVER = """
try:
    from mcp.server.mcpserver import MCPServer as Server  # mcp 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
server = Server("calc")

@server.tool()
def add(a: int, b: int) -> int:
    \"\"\"Add two numbers.\"\"\"
    return a + b

@server.tool()
def shout(text: str) -> str:
    \"\"\"Upper-case some text.\"\"\"
    return text.upper()

server.run()
"""


class ConnectorTests(Base):
    def test_configuring_mcp_servers_and_apis(self):
        http = connectors.configure("mcp", 'notion https://mcp.notion.com/mcp bearer=ntn_secret123 header="X-Team: growth"')
        self.assertEqual((http["transport"], http["target"]), ("http", "mcp.notion.com"))
        sse_entry = connectors.configure("mcp", "old https://example.com/v1/sse")
        self.assertEqual(sse_entry["transport"], "sse")
        local = connectors.configure("mcp", "memory stdio npx -y @modelcontextprotocol/server-memory env:MEMORY_FILE=/tmp/m.json")
        self.assertEqual((local["transport"], local["target"]), ("stdio", "npx"))
        api = connectors.configure("api", 'stats https://api.example.com/v2/ bearer=sk_abc123 query:region=us methods=GET '
                                          'about="Shop sales by day"')
        self.assertEqual((api["host"], api["methods"], api["about"]), ("api.example.com", ["GET"], "Shop sales by day"))
        stored = connectors.load()
        self.assertEqual(stored["mcp"]["notion"]["headers"], {"Authorization": "Bearer ntn_secret123", "X-Team": "growth"})
        self.assertEqual(stored["mcp"]["memory"]["env"], {"MEMORY_FILE": "/tmp/m.json"})
        self.assertEqual(stored["api"]["stats"]["base_url"], "https://api.example.com/v2")
        self.assertNotIn("secret", json.dumps(connectors.summary()))
        self.assertEqual(oct(connectors._path().stat().st_mode & 0o777), "0o600")
        for kind, text in (("mcp", "x http://example.com/mcp"), ("mcp", "Bad!Name https://a.b/mcp"), ("api", "a https://a.b methods=GET,FLY"),
                           ("mcp", "x"), ("mcp", "x stdio")):
            with self.assertRaises(ValueError):
                connectors.configure(kind, text)
        self.assertTrue(connectors.configure("mcp", "old off")["removed"])
        self.assertNotIn("old", connectors.load()["mcp"])

    def test_call_api_adds_keys_stays_on_its_host_and_hides_secrets(self):
        connectors.configure("api", "shop https://api.shop.test/v1 bearer=sk_live_abcdef query:key=qk_123456 methods=GET,POST")
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/moved"):
                return httpx.Response(302, headers={"location": "https://evil.test/"})
            return httpx.Response(200, json={"echo": request.headers.get("authorization"), "items": [1, 2]})

        transport = httpx.MockTransport(handler)
        reply = run(connectors.call("shop", "GET", "/orders?status=open", {"limit": 5}, transport=transport))
        self.assertIn("HTTP 200", reply)
        self.assertIn('"items"', reply)
        self.assertNotIn("sk_live_abcdef", reply)
        sent = requests[0]
        self.assertEqual(str(sent.url.copy_with(query=None)), "https://api.shop.test/v1/orders")
        self.assertEqual(dict(sent.url.params), {"status": "open", "limit": "5", "key": "qk_123456"})
        self.assertEqual(sent.headers["authorization"], "Bearer sk_live_abcdef")
        self.assertIn("not followed", run(connectors.call("shop", "GET", "/moved", transport=transport)))
        self.assertIn("isn't allowed", run(connectors.call("shop", "DELETE", "/orders/1", transport=transport)))
        for path in ("https://evil.test/x", "/../admin", "orders", "//evil.test/x"):
            self.assertNotEqual(run(connectors.call("shop", "GET", path, transport=transport))[:8], "HTTP 200", path)
        self.assertEqual(len(requests), 2)
        self.assertIn("No API named", run(connectors.call("nope")))

    @unittest.skipIf(sandbox.IS_ROOT, "the sandbox user can't run this test's Python as root")
    def test_a_real_stdio_mcp_server_becomes_prefixed_tools(self):
        script = Path(self.temp.name) / "calc_server.py"
        script.write_text(MCP_SERVER)
        python_path = os.pathsep.join(p for p in sys.path if p)
        connectors.configure("mcp", f"calc stdio {sys.executable} {script} 'env:PYTHONPATH={python_path}'")
        space = sandbox.Workspace("owner", "mcp-test").prepare()
        events = []

        async def scenario():
            async with AsyncExitStack() as stack:
                tools, notes = await connectors.open_mcp(stack, space, lambda: None, lambda kind, detail: events.append(detail))
                from agents.tool_context import ToolContext
                add = next(t for t in tools if t.name == "calc__add")
                ctx = ToolContext(context=None, tool_name=add.name, tool_call_id="c1", tool_arguments='{"a": 2, "b": 3}')
                return [t.name for t in tools], notes, await add.on_invoke_tool(ctx, '{"a": 2, "b": 3}')

        names, notes, result = run(scenario())
        self.assertEqual(sorted(names), ["calc__add", "calc__shout"])
        self.assertIn("calc (MCP): 2 tools", notes[0])
        self.assertEqual(result.strip(), "5")
        self.assertIn("calc: add", events)

    def test_a_broken_server_is_reported_not_fatal_and_owner_tasks_get_connected_tools(self):
        connectors.configure("mcp", "broken stdio /nonexistent/mcp-server")
        connectors.configure("api", 'crm https://crm.test/api about="Leads and deals"')
        job = running_job("chat-tools")
        space = sandbox.Workspace("owner", "chat-tools").prepare()
        state = {}

        async def scenario():
            async with AsyncExitStack() as stack:
                await cowork.runner.open_connectors(job, space, state, stack)
                fake = AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1")
                with patch.object(escalate, "available", return_value="off"):
                    return cowork.build(job, fake, asyncio.Semaphore(1), space, state)

        agent = run(scenario())
        self.assertIn("call_api", {t.name for t in agent.tools})
        self.assertIn("CONNECTED TOOLS", agent.instructions)
        self.assertIn("broken (MCP): unavailable right now", agent.instructions)
        self.assertIn("crm (API, call_api): https://crm.test/api, methods GET/POST — Leads and deals", agent.instructions)

    def test_console_and_chat_commands(self):
        client = TestClient(console.create_app("k" * 40, run_worker=False))
        r = client.post("/api/connections/tools", headers=AUTH, json={"kind": "api", "text": "crm https://crm.test bearer=tok_123456"})
        self.assertEqual((r.status_code, r.json()["host"]), (200, "crm.test"))
        r = client.post("/api/connections/tools", headers=AUTH, json={"kind": "mcp", "text": "gone stdio /nonexistent/server"})
        self.assertIn("error", r.json()["check"]) if "error" in r.json()["check"] else self.assertEqual(r.json()["check"]["tools"], [])
        listing = client.get("/api/connections/tools", headers=AUTH).json()
        self.assertEqual(set(listing["api"]), {"crm"})
        self.assertNotIn("tok_123456", json.dumps(listing))
        self.assertEqual(client.post("/api/connections/tools", headers=AUTH, json={"kind": "api", "text": "crm http://crm.test"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
