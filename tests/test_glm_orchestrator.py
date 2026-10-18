"""GLM as the orchestrator: it plans and assigns, Qwen helpers on both GPUs do the work (agents/glm.py,
agents/cowork/orchestrator.py)."""
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
sys.path.insert(0, str(ROOT / "tests"))
import store
import workspace as ws
import hub
import sandbox
import cowork
import escalate
import glm
import jev
import console
from cowork import gpu, kickoff, orchestrator, tools as cowork_tools
from openai import AsyncOpenAI
import httpx2 as httpx
from fastapi.testclient import TestClient

KEY = "ak_" + "x" * 40


def sse(delta, finish, model="m", usage=None):
    chunk = lambda d, f=None: {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model,
                               "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
    pieces = [chunk(delta), chunk({}, finish),
              {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model, "choices": [],
               "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="".join("data: " + json.dumps(p) + "\n\n" for p in pieces) + "data: [DONE]\n\n")


def calls(*items):
    return {"role": "assistant", "tool_calls": [
        {"index": i, "id": f"call-{name}-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(items)]}


def client_for(handler):
    return AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1",
                       http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.chmod(self.temp.name, 0o755)
        import toolbox
        self.old = (store.DATA, sandbox.ROOT, dict(sandbox.USERS), toolbox.TOOLS_DIR)
        store.DATA = Path(self.temp.name) / "data"
        sandbox.ROOT = Path(self.temp.name) / "cowork"
        toolbox.TOOLS_DIR = Path(self.temp.name) / "tools"
        if sandbox.IS_ROOT:
            sandbox.USERS.update(owner="nobody", guest="nobody", friend="nobody")
        ws.init()
        gpu._leads.clear(); gpu._last.clear(); gpu._helpers.clear()

    def tearDown(self):
        import toolbox
        store.DATA, sandbox.ROOT, toolbox.TOOLS_DIR = self.old[0], self.old[1], self.old[3]
        sandbox.USERS.clear(); sandbox.USERS.update(self.old[2])
        self.temp.cleanup()


class MeterTests(Base):
    def test_key_budget_and_monthly_meter(self):
        self.assertIn("not connected", glm.available())
        with self.assertRaises(ValueError):
            glm.save_key("short")
        glm.save_key(KEY)
        self.assertEqual(oct((store.DATA / "glm-key").stat().st_mode & 0o777), "0o600")
        self.assertIsNone(glm.available())
        # 1M uncached input, 1M cached input, 1M output at $3 / $0.30 / $5.
        self.assertAlmostEqual(glm.record(2_000_000, 1_000_000, 1_000_000), 8.30, places=6)
        self.assertEqual(glm.status()["calls"], 1)
        glm.save_budget(8)
        self.assertIn("budget is used up", glm.available())
        glm.save_budget(50)
        self.assertIsNone(glm.available())
        with patch.dict(os.environ, {"COWORK_ORCHESTRATOR": "off"}):
            self.assertIn("switched off", glm.available())
        glm.save_key("off")
        self.assertFalse(glm.status()["connected"])

    def test_console_connect_command(self):
        client = TestClient(console.create_app("k" * 40, run_worker=False))
        auth = {"Authorization": "Bearer " + "k" * 40}
        r = client.post("/api/connections/glm", headers=auth, json={"token": KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["connected"])
        self.assertNotIn(KEY, r.text)
        r = client.post("/api/connections/glm", headers=auth, json={"token": "budget $25"})
        self.assertEqual(r.json()["budget_usd"], 25)
        self.assertEqual(client.post("/api/connections/glm", headers=auth, json={"token": "budget lots"}).status_code, 400)
        self.assertTrue(client.get("/api/connections/glm", headers=auth).json()["connected"])


class BuildTests(Base):
    job = {"id": "j" * 32, "project": "default", "profile": "fast", "allow_frontier": 1, "allow_images": 1, "thread": "t",
           "skill": "cowork", "task": "Build me a landing page"}

    def build(self, job=None, tier="owner"):
        job = job or self.job
        ws.create_job(job["project"], "x", job["skill"], thread="t")
        space = sandbox.Workspace(tier, "t").prepare()
        fake = AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1")
        with patch.object(escalate, "available", return_value=None), patch.object(jev, "available", return_value=None):
            state = {}
            agent = cowork.build(job, fake, asyncio.Semaphore(1), space, state)
        return agent, state

    def test_glm_leads_with_planning_tools_only(self):
        glm.save_key(KEY)
        agent, state = self.build()
        self.assertTrue(state["orchestrated"])
        self.assertEqual(state["lead"], "glm")
        self.assertIsInstance(agent.model, orchestrator.OrchestratorModel)
        names = {t.name for t in agent.tools}
        self.assertTrue({"update_plan", "start_helpers", "collect_helpers", "delegate_many", "read_file", "share_file",
                         "ask_user", "ask_claude", "ask_codex", "ask_jev"} <= names)
        self.assertFalse({"run_shell", "web_search", "read_webpage", "generate_image"} & names)
        self.assertTrue(agent.instructions.startswith("You are the lead of Qwen Cowork"))
        self.assertIn("up to 6 at once", agent.instructions)
        self.assertIn("Claude Code (ask_claude)", agent.instructions)

    def test_qwen_still_leads_without_glm_for_friends_and_in_chat_mode(self):
        agent, state = self.build()
        self.assertFalse(state["orchestrated"])
        self.assertIn(state["lead"], cowork.RESIDENTS)
        glm.save_key(KEY)
        _, state = self.build({**self.job, "project": "friends"}, tier="guest")
        self.assertFalse(state["orchestrated"])
        _, state = self.build({**self.job, "skill": "chat"})
        self.assertFalse(state["orchestrated"])
        _, state = self.build({**self.job, "allow_frontier": 0})
        self.assertFalse(state["orchestrated"])

    def test_helpers_spread_over_both_gpus(self):
        self.assertEqual(gpu.least_busy(4), ["qwen-1", "qwen-2", "qwen-1", "qwen-2"])
        gpu.helper_started("qwen-1")
        self.assertEqual(gpu.least_busy(1), ["qwen-2"])
        gpu.helper_finished("qwen-1")
        self.assertEqual(gpu.assign_gpus("a", orchestrated=True)[0], "glm")
        self.assertEqual(gpu._leads, {})  # GLM takes no GPU


class HandOffTests(Base):
    def test_a_helpers_failed_hand_off_is_reported_not_fatal(self):
        space = sandbox.Workspace("owner", "t").prepare()
        ctx = cowork.ToolContext(job={"id": "j" * 32, "project": "default", "profile": "fast"}, space=space, client=None,
                                 gate=None, budget=type("B", (), {"active": lambda self: None})(), state={}, images={},
                                 log=lambda *a: None, before=lambda *a: None, lead_model="glm", helper_model="qwen-1")

        async def failing(*args, **kwargs):
            return {"ok": False, "error": "Claude Code is not signed in"}
        with patch.object(escalate, "run", failing):
            report = asyncio.run(cowork_tools._hand_off(ctx, stop_on_failure=False)("claude", "build it"))
            self.assertNotIn("stop", ctx.state)
            self.assertIn("Do the part yourself", report)
            asyncio.run(cowork_tools._hand_off(ctx)("claude", "build it"))
            self.assertIn("stop", ctx.state)

    def test_the_clock_pauses_until_the_last_waiting_helper_returns(self):
        async def scenario():
            async with asyncio.timeout(100) as timer:
                ctx = type("C", (), {"state": {"timer": timer}})()
                first, second = cowork_tools.paused_clock(ctx), cowork_tools.paused_clock(ctx)
                await first.__aenter__()
                await second.__aenter__()
                await first.__aexit__(None, None, None)
                still_paused = timer.when() is None
                await second.__aexit__(None, None, None)
                return still_paused, timer.when() is not None
        self.assertEqual(asyncio.run(scenario()), (True, True))


class EndToEndTests(Base):
    def run_task(self, glm_handler, hub_handler):
        async def scenario():
            identity = ws.create_job("default", "Write two short notes", "cowork", "fast", allow_frontier=True,
                                     thread="chat-glm")
            job = ws.claim_job()

            async def no_questions(*args, **kwargs):
                return ""
            with patch.object(hub, "async_client", return_value=client_for(hub_handler)), \
                 patch.object(glm, "client", return_value=client_for(glm_handler)), \
                 patch.object(kickoff, "clarify", no_questions), \
                 patch.object(escalate, "available", return_value="off"):
                return identity, await cowork.run_job(job)
        return asyncio.run(scenario())

    def test_glm_plans_and_both_qwens_work_in_parallel(self):
        glm.save_key(KEY)
        lead_requests, helper_models = [], []
        script = [
            calls(("update_plan", {"steps": ["Note A", "Note B"], "active_index": 0, "completed_indices": []}),
                  ("start_helpers", {"briefs": ["Write a.txt saying alpha", "Write b.txt saying beta"]})),
            calls(("collect_helpers", {"wait_seconds": 60})),
            {"role": "assistant", "content": "Both notes are written."},
        ]

        def glm_handler(request):
            body = json.loads(request.content)
            lead_requests.append(body)
            step = sum(1 for m in body["messages"] if m["role"] == "assistant")
            delta = script[step]
            return sse(delta, "tool_calls" if "tool_calls" in delta else "stop", model=glm.MODEL,
                       usage={"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
                              "prompt_tokens_details": {"cached_tokens": 800}})

        def hub_handler(request):
            body = json.loads(request.content)
            system = body["messages"][0]["content"]
            if system.startswith("You are a helper agent"):
                helper_models.append(body["model"])
                self.assertIn("keep that final message under about 250 words", system)
                return sse({"role": "assistant", "content": f"Wrote the note on {body['model']}."}, "stop")
            return sse({"role": "assistant", "content": "unexpected"}, "stop")

        identity, output = self.run_task(glm_handler, hub_handler)
        self.assertEqual(output, "Both notes are written.")
        self.assertEqual(sorted(helper_models), ["qwen-1", "qwen-2"])
        self.assertEqual({r["model"] for r in lead_requests}, {glm.MODEL})
        # Planning steps think at "high"; right after a read-only look (collect is not one) still "high".
        self.assertEqual({r["reasoning_effort"] for r in lead_requests}, {"high"})
        self.assertTrue(all(r["max_tokens"] <= glm.MAX_TOKENS for r in lead_requests))
        lead_tools = {t["function"]["name"] for t in lead_requests[0]["tools"]}
        self.assertNotIn("run_shell", lead_tools)
        self.assertIn("Wrote the note on qwen-2", lead_requests[-1]["messages"][-1]["content"])
        month = glm.this_month()
        self.assertEqual((month["calls"], month["cached_tokens"]), (3, 2400))
        self.assertAlmostEqual(month["usd"], 3 * glm.cost(1000, 800, 100), places=6)
        notes = [e["detail"] for e in ws.query("SELECT detail FROM events WHERE job_id=? AND kind='tool'", (identity,))]
        self.assertTrue(any(n.startswith("GLM: 3 calls") for n in notes))
        self.assertEqual(gpu._helpers, {"qwen-1": 0, "qwen-2": 0})

    def test_glm_out_of_tokens_retries_with_less_thinking_then_hands_one_step_to_qwen(self):
        glm.save_key(KEY)
        efforts, qwen_calls = [], []

        def length_only(request, model):
            # Thinking used every token: no text, no tool call, finish_reason "length".
            chunk = lambda d, f=None: {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model,
                                       "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
            pieces = [chunk({"role": "assistant", "reasoning_content": "hmm " * 50}), chunk({}, "length"),
                      {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model, "choices": [],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 16000, "total_tokens": 16100}}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join("data: " + json.dumps(p) + "\n\n" for p in pieces) + "data: [DONE]\n\n")

        def glm_handler(request):
            body = json.loads(request.content)
            efforts.append(body.get("reasoning_effort"))
            if len(efforts) == 1:  # the first try runs out; the low-effort retry answers
                return length_only(request, body["model"])
            if len(efforts) == 2:
                return sse({"role": "assistant", "content": "Answered after the retry."}, "stop", model=glm.MODEL)
            return length_only(request, body["model"])

        def hub_handler(request):
            qwen_calls.append(json.loads(request.content)["model"])
            return sse({"role": "assistant", "content": "Qwen took the step."}, "stop")

        identity, output = self.run_task(glm_handler, hub_handler)
        self.assertEqual(output, "Answered after the retry.")
        self.assertEqual(efforts, ["high", "low"])
        self.assertEqual(qwen_calls, [])
        notes = [e["detail"] for e in ws.query("SELECT detail FROM events WHERE job_id=? AND kind='tool'", (identity,))]
        self.assertIn("GLM ran out of room while thinking; asking again with less thinking", notes)

    def test_when_glm_runs_out_twice_qwen_takes_that_step_and_glm_stays_the_lead(self):
        glm.save_key(KEY)
        efforts = []

        def length_only(request):
            efforts.append(json.loads(request.content).get("reasoning_effort"))
            chunk = lambda d, f=None: {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": glm.MODEL,
                                       "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
            pieces = [chunk({"role": "assistant", "reasoning_content": "hmm"}), chunk({}, "length")]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join("data: " + json.dumps(p) + "\n\n" for p in pieces) + "data: [DONE]\n\n")

        def hub_handler(request):
            return sse({"role": "assistant", "content": "Qwen took the step."}, "stop")

        identity, output = self.run_task(length_only, hub_handler)
        self.assertEqual(output, "Qwen took the step.")
        self.assertEqual(efforts, ["high", "low"])
        notes = [e["detail"] for e in ws.query("SELECT detail FROM events WHERE job_id=? AND kind='tool'", (identity,))]
        self.assertIn("GLM couldn't fit this step; Qwen takes this one step", notes)
        self.assertFalse(any(n.startswith("GLM is unavailable") for n in notes))

    def test_qwen_takes_over_when_glm_fails(self):
        glm.save_key(KEY)

        def glm_handler(request):
            return httpx.Response(500, json={"error": {"message": "upstream down"}})

        def hub_handler(request):
            body = json.loads(request.content)
            return sse({"role": "assistant", "content": f"Done by {body['model']}."}, "stop")

        identity, output = self.run_task(glm_handler, hub_handler)
        self.assertEqual(output, "Done by qwen.")
        notes = [e["detail"] for e in ws.query("SELECT detail FROM events WHERE job_id=? AND kind='tool'", (identity,))]
        self.assertTrue(any(n.startswith("GLM is unavailable") for n in notes))
        self.assertEqual(glm.this_month()["calls"], 0)


if __name__ == "__main__":
    unittest.main()
