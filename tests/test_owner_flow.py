"""Kickoff questions, direct hand-off when the user names Claude, background helpers and GPU alternation."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "tests"))
import asking
import workspace as ws
import hub
import cowork
import escalate
from cowork import kickoff
from test_cowork import Base, sse, calls
from test_upgrades import fake_client


class IntentTests(unittest.TestCase):
    def test_claude_is_detected_only_when_asked_to_do_the_work(self):
        for text in ("use claude to build the landing page", "have Claude Code fix the bug", "give this to claude",
                     "Claude should write the script", "do it with claude", "claude, build me a site", "let claude handle it",
                     "CONVERSATION SO FAR:\nUSER: hi\n\nCURRENT REQUEST:\nask Claude to review app.py"):
            self.assertTrue(cowork.wants_claude(text), text)
        for text in ("write a post about Claude", "compare ChatGPT vs Claude", "is gpt better than claude",
                     "give me tips on claude prompting", "don't use claude for this", "what is claude",
                     "CONVERSATION SO FAR:\nUSER: use claude for it\n\nCURRENT REQUEST:\nnow make it blue"):
            self.assertFalse(cowork.wants_claude(text), text)

    def test_kickoff_questions_skip_continuations_and_just_do_it(self):
        self.assertTrue(cowork.may_ask_first("CURRENT REQUEST:\nmake me a landing page for my app"))
        for text in ("CURRENT REQUEST:\ncontinue", "CURRENT REQUEST:\nkeep going", "CURRENT REQUEST:\njust do it: make a logo",
                     "AUTOMATIC NEXT PHASE (queued)\n\nCURRENT REQUEST:\nphase 2", "AUTOMATIC CONTINUATION 1 of up to 4"):
            self.assertFalse(cowork.may_ask_first(text), text)

    def test_parsing_and_message(self):
        found = kickoff._parse('<think>hmm</think>{"questions": [{"question": "Which platform?", "options": ["TikTok", "Reels"]},'
                               ' "How long?", {"question": ""}, {"question": "Tone?"}, {"question": "Extra?"}]}')
        self.assertEqual([q["question"] for q in found], ["Which platform?", "How long?", "Tone?"])
        text, options = kickoff.message(found)
        self.assertIn("1. Which platform? (TikTok / Reels)", text)
        self.assertEqual(options, [])
        self.assertEqual(kickoff.message(found[:1]), ("Before I start: Which platform?", ["TikTok", "Reels"]))
        self.assertEqual(kickoff._parse("no json here"), [])


class GpuTests(Base):
    def test_single_tasks_alternate_between_the_gpus(self):
        first = cowork.assign_gpus("a")
        cowork.release_gpus("a")
        second = cowork.assign_gpus("b")
        self.assertEqual((first, second), (("qwen-1", "qwen-2"), ("qwen-2", "qwen-1")))
        third = cowork.assign_gpus("c")  # while b leads on qwen-2, a second task leads on qwen-1
        self.assertEqual(third[0], "qwen-1")


class KickoffFlowTests(Base):
    def test_questions_are_asked_before_starting_and_the_answer_reaches_the_lead(self):
        lead_inputs, kickoff_models = [], []

        def handler(request):
            body = json.loads(request.content)
            system = body["messages"][0]["content"]
            if system.startswith("You decide whether"):
                kickoff_models.append(body["model"])
                return sse({"role": "assistant", "content": json.dumps({"questions": [
                    {"question": "Which platform is it for?", "options": ["TikTok", "Instagram"]}]})}, "stop")
            lead_inputs.append(body["messages"][1]["content"])
            return sse({"role": "assistant", "content": "Here are the hooks."}, "stop")

        async def answer_when_asked(identity):
            for _ in range(200):
                await asyncio.sleep(0.05)
                question = asking.pending(identity)
                if question:
                    asking.answer(identity, "2")  # picks the second option
                    return question

        async def scenario():
            identity = ws.create_job("default", "CURRENT REQUEST:\nWrite 10 hooks for my new product video", "cowork", "fast",
                                     thread="chat-k")
            job = ws.claim_job()
            answering = asyncio.create_task(answer_when_asked(identity))
            with patch.object(hub, "async_client", return_value=fake_client(handler)), \
                 patch.object(escalate, "available", return_value="off"):
                output = await cowork.run_job(job)
            return output, await answering

        output, question = asyncio.run(scenario())
        self.assertEqual(output, "Here are the hooks.")
        self.assertEqual(question["question"], "Before I start: Which platform is it for?")
        self.assertEqual(kickoff_models, ["qwen-2"])  # decided on the helper GPU
        self.assertIn("THEIR ANSWER:\nInstagram", lead_inputs[0])

    def test_short_or_specific_requests_go_straight_to_work(self):
        def handler(request):
            body = json.loads(request.content)
            if body["messages"][0]["content"].startswith("You decide whether"):
                return sse({"role": "assistant", "content": '{"questions": []}'}, "stop")
            return sse({"role": "assistant", "content": "Done."}, "stop")

        async def scenario():
            ws.create_job("default", "CURRENT REQUEST:\nConvert uploads/a.csv to xlsx", "cowork", "fast", thread="chat-k2")
            job = ws.claim_job()
            with patch.object(hub, "async_client", return_value=fake_client(handler)), \
                 patch.object(escalate, "available", return_value="off"):
                return job, await cowork.run_job(job)
        job, output = asyncio.run(scenario())
        self.assertEqual(output, "Done.")
        self.assertFalse(ws.query("SELECT 1 FROM questions WHERE job_id=?", (job["id"],)))


class DirectClaudeTests(Base):
    def run_with(self, request, handoff, handler):
        async def scenario():
            ws.create_job("default", "CONVERSATION SO FAR:\nUSER: earlier\n\nCURRENT REQUEST:\n" + request, "cowork", "fast",
                          allow_frontier=True, thread="chat-c")
            job = ws.claim_job()
            with patch.object(hub, "async_client", return_value=fake_client(handler)), \
                 patch.object(escalate, "available", return_value=None), \
                 patch.object(escalate, "run", side_effect=handoff) as run, \
                 patch.object(cowork.kickoff, "questions_for", return_value=[]):
                return job, await cowork.run_job(job), run
        return asyncio.run(scenario())

    def test_naming_claude_hands_the_request_over_first(self):
        lead_inputs = []

        def handler(request):
            body = json.loads(request.content)
            lead_inputs.append((body["messages"][0]["content"], body["messages"][1]["content"]))
            return sse({"role": "assistant", "content": "Claude built site/index.html; it works."}, "stop")

        async def handoff(kind, space, job_id, task, timeout=None):
            return {"ok": True, "agent": kind, "summary": "Built site/index.html"}

        job, output, run = self.run_with("use claude to build me a landing page", handoff, handler)
        self.assertEqual(output, "Claude built site/index.html; it works.")
        kind, _, _, brief = run.call_args.args
        self.assertEqual(kind, "claude")
        self.assertIn("USER: earlier", brief)
        self.assertIn("use claude to build me a landing page", brief)
        system, first = lead_inputs[0]
        self.assertIn("CLAUDE CODE HAS ALREADY WORKED ON THIS REQUEST", first)
        self.assertIn("Built site/index.html", first)
        self.assertNotIn("THE USER ASKED FOR CLAUDE IN THIS REQUEST", system)  # already handed over

    def test_a_failed_hand_off_is_reported_not_silently_done_by_qwen(self):
        def handler(request):
            raise AssertionError("Qwen must not take over the work")

        async def handoff(kind, space, job_id, task, timeout=None):
            return {"ok": False, "error": "claude daily limit reached (30 tasks per 24 hours)"}

        job, output, _ = self.run_with("have claude write the scraper", handoff, handler)
        self.assertIn("Claude Code couldn't do this: claude daily limit reached", output)

    def test_long_chats_go_to_claude_in_a_file(self):
        job = {"id": "x" * 32, "task": "CONVERSATION SO FAR:\n" + "é" * 70000 + "\n\nCURRENT REQUEST:\nuse claude to fix it",
               "project": "default", "thread": None}
        space = cowork.runner.sandbox.Workspace("owner", "brief").prepare()
        brief = cowork.runner.handoff_brief(job, space, "")
        self.assertLess(len(brief.encode()), 30000)
        self.assertIn(".claude-brief.md", brief)
        self.assertIn("use claude to fix it", brief)
        self.assertIn("é" * 100, (space.dir / ".claude-brief.md").read_text())


class BackgroundHelperTests(Base):
    def test_helpers_work_in_the_background_on_the_other_gpu_while_the_lead_continues(self):
        order, script = [], [
            calls(("start_helpers", {"briefs": ["Research apples into a.md", "Research pears into b.md"]})),
            calls(("write_file", {"path": "intro.md", "content": "intro"})),
            calls(("collect_helpers", {"wait_seconds": 30})),
            {"role": "assistant", "content": "Report ready."},
        ]

        async def handler(request):
            body = json.loads(request.content)
            if body["messages"][0]["content"].startswith("You are a helper agent"):
                order.append(("helper-start", body["model"]))
                await asyncio.sleep(0.3)
                order.append(("helper-end", body["model"]))
                return sse({"role": "assistant", "content": "found " + body["messages"][-1]["content"][9:15]}, "stop")
            step = sum(1 for m in body["messages"] if m["role"] == "assistant")
            order.append(("lead", step))
            if step == 3:
                tool_results = [m["content"] for m in body["messages"] if m["role"] == "tool"]
                order.append(("collected", tool_results[-1]))
            delta = script[step]
            return sse(delta, "tool_calls" if "tool_calls" in delta else "stop")

        async def scenario():
            ws.create_job("default", "CURRENT REQUEST:\nfruit", "cowork", "fast", thread="chat-b")
            job = ws.claim_job()
            with patch.object(hub, "async_client", return_value=fake_client(handler)), \
                 patch.object(escalate, "available", return_value="off"):
                return await cowork.run_job(job, asyncio.Semaphore(6))

        self.assertEqual(asyncio.run(scenario()), "Report ready.", order)
        # The lead's next call happened while the helpers were still working.
        self.assertLess(order.index(("lead", 1)), order.index(("helper-end", "qwen-2")))
        self.assertEqual({m for kind, m in order if kind == "helper-start"}, {"qwen-2"})
        collected = dict(o for o in order if o[0] == "collected")["collected"]
        self.assertIn("### h1: Research apples", collected)
        self.assertIn("### h2: Research pears", collected)


if __name__ == "__main__":
    unittest.main()
