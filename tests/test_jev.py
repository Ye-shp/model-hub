"""Jev (TypeSafe) for Cowork: the key, the request, the tool, the prompt, and the Claude Code plugin install."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))



import cowork
import escalate
import jev
httpx = jev.httpx
import sandbox
import store
import workspace as ws
from crew import CallBudget
from openai import AsyncOpenAI

KEY = "apikey_" + "a1" * 20
QUESTIONS = {"positive": {"type": "noul", "instructions": "Is the comment positive?"},
             "intent": {"type": "choice", "instructions": "What does the commenter want?",
                        "criteria": {"more": "More videos", "other": "Anything else"}},
             "hype": {"type": "score", "instructions": "How excited?", "criteria": ["Calm", "Interested", "Very excited"]}}
ANSWER = {"model": "jev-1.13.0", "answers": {"positive": {"type": "noul", "noul": 0.97}}, "usage": {"input_tokens": 9}}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.chmod(self.temp.name, 0o755)
        self.old = (store.DATA, sandbox.ROOT, dict(sandbox.USERS))
        store.DATA = Path(self.temp.name) / "data"
        sandbox.ROOT = Path(self.temp.name) / "cowork"
        if sandbox.IS_ROOT:
            sandbox.USERS.update(owner="nobody", guest="nobody", friend="nobody")
        ws.init()
        cowork._leads.clear()
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("TYPESAFE_API_KEY", None)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.env.stop()
        store.DATA, sandbox.ROOT = self.old[0], self.old[1]
        sandbox.USERS.clear()
        sandbox.USERS.update(self.old[2])
        cowork._leads.clear()
        self.temp.cleanup()


class KeyAndRequestTests(Base):
    def test_key_is_validated_stored_privately_and_removable(self):
        with self.assertRaises(ValueError):
            jev.save_key("sk-not-a-typesafe-key")
        self.assertIsNotNone(jev.available())
        jev.save_key(KEY)
        self.assertEqual(oct(jev.key_file().stat().st_mode & 0o777), "0o600")
        self.assertEqual(jev.api_key(), KEY)
        self.assertIsNone(jev.available())
        self.assertTrue(escalate.status()["jev"]["signed_in"])
        jev.save_key("off")
        self.assertFalse(jev.status()["signed_in"])

    def test_bad_questions_are_explained(self):
        for bad, message in (({}, "object"), ({"x": {"type": "guess", "instructions": "?"}}, "type"),
                             ({"x": {"type": "choice", "instructions": "?", "criteria": {"a": "only one"}}}, "at least 2"),
                             ({"x": {"type": "score", "instructions": "?", "criteria": ["one"]}}, "2-10"),
                             ({"bad id!": {"type": "noul", "instructions": "?"}}, "letters")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                jev.check_questions(bad)
        self.assertEqual(jev.check_questions(QUESTIONS), QUESTIONS)

    def test_request_shape_retry_and_key_never_echoed(self):
        jev.save_key(KEY)
        seen, replies = [], [httpx.Response(529, text="busy"), httpx.Response(200, json=ANSWER)]

        def handler(request):
            seen.append(request)
            return replies.pop(0)

        real_sleep = asyncio.sleep

        async def no_wait(seconds):
            await real_sleep(0)

        with patch.object(jev.asyncio, "sleep", no_wait):
            result = asyncio.run(jev.ask({"comment": "need part 2"}, QUESTIONS, transport=httpx.MockTransport(handler)))
        self.assertEqual(result["answers"]["positive"]["noul"], 0.97)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].headers["authorization"], f"Bearer {KEY}")
        body = json.loads(seen[0].content)
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["questions"], QUESTIONS)

        refused = asyncio.run(jev.ask("x", QUESTIONS, transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text=f"bad key {KEY}"))))
        self.assertIn("401", refused["error"])
        self.assertNotIn(KEY, refused["error"])

    def test_oversized_state_is_not_sent(self):
        jev.save_key(KEY)
        transport = httpx.MockTransport(lambda request: self.fail("must not be sent"))
        result = asyncio.run(jev.ask("x" * (jev.MAX_STATE_CHARS + 10), QUESTIONS, transport=transport))
        self.assertIn("too large", result["error"])


class CoworkTests(Base):
    def build(self, tier, allow_frontier=1, task="", job_id="j" * 32):
        from cowork.tools import ToolContext, build_tools
        job = {"id": job_id, "project": "default" if tier == "owner" else "friends", "profile": "fast",
               "allow_frontier": allow_frontier, "allow_images": 0, "thread": "t", "task": task}
        space = sandbox.Workspace(tier, "t").prepare()
        client = AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1")
        ctx = ToolContext(job=job, space=space, client=client, gate=asyncio.Semaphore(1), budget=CallBudget(job, limit=10),
                          state={"delegations": 0}, images={"count": 0}, log=lambda kind, detail: None,
                          before=lambda name: None, lead_model="qwen-1", helper_model="qwen-2")
        with patch.object(escalate, "available", return_value=None):
            tools = build_tools(ctx)
        return {t.name: t for t in tools}, ctx

    def test_owner_gets_ask_jev_only_when_connected(self):
        tools, ctx = self.build("owner")
        self.assertNotIn("ask_jev", tools)
        jev.save_key(KEY)
        tools, ctx = self.build("owner")
        self.assertIn("ask_jev", tools)
        self.assertTrue(ctx.state["jev"])
        self.assertNotIn("ask_jev", self.build("owner", allow_frontier=0)[0])
        self.assertNotIn("ask_jev", self.build("guest", allow_frontier=0)[0])

    def test_ask_jev_sends_parsed_state_and_logs_the_hand_off(self):
        jev.save_key(KEY)
        ws.create_job("default", "x", "cowork", thread="t")
        job_id = ws.query("SELECT id FROM jobs LIMIT 1")[0]["id"]
        with ws.connection() as db, db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        tools, ctx = self.build("owner", job_id=job_id)
        from agents.tool_context import ToolContext as Invocation
        sent = {}

        async def fake_ask(state, questions):
            sent.update(state=state, questions=questions)
            return ANSWER

        args = json.dumps({"state_json": json.dumps({"comment": "part 2 pls"}), "questions_json": json.dumps(QUESTIONS)})
        with patch.object(jev, "ask", fake_ask):
            reply = asyncio.run(tools["ask_jev"].on_invoke_tool(
                Invocation(context=None, tool_name="ask_jev", tool_call_id="c", tool_arguments=args), args))
        self.assertTrue(reply.startswith("{"), reply)
        self.assertEqual(json.loads(reply)["answers"]["positive"]["noul"], 0.97)
        self.assertEqual(sent["state"], {"comment": "part 2 pls"})
        self.assertEqual(jev.used_today(), 1)
        bad = json.dumps({"state_json": "x", "questions_json": json.dumps({"q": {"type": "maybe", "instructions": "?"}})})
        refused = asyncio.run(tools["ask_jev"].on_invoke_tool(
            Invocation(context=None, tool_name="ask_jev", tool_call_id="c", tool_arguments=bad), bad))
        self.assertIn("Not sent to Jev", refused)

    def test_prompt_explains_when_to_use_jev_and_loads_the_skill_for_builds(self):
        from cowork.prompt import instructions
        space = sandbox.Workspace("owner", "t").prepare()
        job = {"id": "j" * 32, "project": "default", "profile": "fast", "allow_images": 0, "allow_frontier": 1, "thread": "t",
               "task": "rank these comments"}
        self.assertNotIn("JEV (ask_jev", instructions(job, space))
        text = instructions(job, space, jev=True)
        self.assertIn("JEV (ask_jev", text)
        self.assertNotIn("BUILDING WITH TYPESAFE", text)
        build = instructions({**job, "task": "build a ticket router with TypeSafe"}, space, jev=True)
        self.assertIn("BUILDING WITH TYPESAFE", build)
        self.assertIn("docs.typesafe.ai", build)
        self.assertIn("JEV (ask_jev", instructions(job, space, helper=True, jev=True))


class ClaudePluginTests(Base):
    def test_plugins_install_once_and_retry_after_failure(self):
        space = sandbox.Workspace("owner", "connections").prepare()
        calls, outcome = [], {"code": 1}

        async def fake_run(command, timeout=120, extra_env=None, cwd=None):
            calls.append(command[1:])
            return {"exit_code": outcome["code"], "timed_out": False, "output": "error: offline"}

        with patch.object(escalate, "binary", return_value="/usr/bin/claude"), patch.object(space, "run", fake_run):
            self.assertEqual(asyncio.run(escalate.ensure_claude_plugins(space)), [])
            self.assertEqual(calls, [["plugin", "marketplace", "add", "typesafe-ai/skills"]])
            outcome["code"] = 0
            self.assertEqual(asyncio.run(escalate.ensure_claude_plugins(space)), ["typesafe@typesafe-ai"])
            self.assertEqual(calls[-1], ["plugin", "install", "typesafe@typesafe-ai"])
            count = len(calls)
            self.assertEqual(asyncio.run(escalate.ensure_claude_plugins(space)), [])
            self.assertEqual(len(calls), count)

    def test_claude_gets_the_typesafe_key(self):
        self.assertNotIn("TYPESAFE_API_KEY", escalate.claude_env())
        jev.save_key(KEY)
        self.assertEqual(escalate.claude_env()["TYPESAFE_API_KEY"], KEY)


if __name__ == "__main__":
    unittest.main()
