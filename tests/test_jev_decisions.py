"""Decisions the controller hands to Jev: kickoff, Claude routing, reply check, continuation, study pre-screen,
approvals and trend reranking. Every one must fall back to the old behaviour when Jev isn't usable."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.append(str(ROOT / "agents" / "vendor" / "last30days" / "scripts"))  # last: it has its own store.py
import workspace as ws
import hub
import cowork
import escalate
import jev
import research_tools
import study
from cowork import judge, kickoff
from test_cowork import Base, sse, calls
from test_upgrades import fake_client


def noul(p):
    return {"type": "noul", "noul": p}


def score(value, confidence=0.9):
    return {"type": "score", "score": value, "confidence": confidence}


class FakeJev:
    """Answers each question id from a table; records what was asked."""
    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    async def __call__(self, state, questions, transport=None, attempts=3):
        jev.check_questions(questions)
        self.calls.append((state, questions))
        return {"model": "jev-test", "answers": {k: self.answers[k] for k in questions if k in self.answers}, "usage": {}}


def connected(fake):
    """Jev connected and answering from `fake`."""
    return patch.multiple(jev, available=lambda: None, ask=fake)


OWNER = {"id": "j" * 32, "project": "default", "thread": "t", "allow_frontier": 1, "skill": "cowork"}


class GateTests(Base):
    def test_only_the_owners_frontier_tasks_use_jev_and_failures_fall_back(self):
        fake = FakeJev(ask=noul(0.9))
        with connected(fake):
            self.assertTrue(judge.enabled(OWNER))
            self.assertFalse(judge.enabled({**OWNER, "allow_frontier": 0}))
            self.assertFalse(judge.enabled({**OWNER, "project": "friend-abc"}))
        self.assertFalse(judge.enabled(OWNER))  # not connected
        self.assertIsNone(asyncio.run(judge.needs_questions({**OWNER, "task": "CURRENT REQUEST:\nmake a logo"})))

        async def broken(*args, **kwargs):
            return {"error": "TypeSafe returned HTTP 500"}
        with patch.multiple(jev, available=lambda: None, ask=broken):
            ws.create_job("default", "x", "cowork", thread="t")
            job = {**OWNER, "id": ws.claim_job()["id"], "task": "CURRENT REQUEST:\nmake a logo"}
            self.assertIsNone(asyncio.run(judge.needs_questions(job)))
            # Every request is logged as a Jev hand-off, so it counts toward the daily limit.
            self.assertEqual(ws.query("SELECT detail FROM events WHERE kind='escalation' AND job_id=?", (job["id"],)),
                             [{"detail": "jev: ask first?"}])


class KickoffTests(Base):
    def test_jev_skips_the_qwen_question_writer_when_nothing_needs_asking(self):
        def handler(request):
            raise AssertionError("no model call when Jev says no questions are needed")

        fake = FakeJev(ask=noul(0.08))
        with connected(fake):
            found = asyncio.run(kickoff.questions_for({**OWNER, "task": "CURRENT REQUEST:\nConvert a.csv to xlsx"},
                                                      fake_client(handler), asyncio.Semaphore(1), "qwen-2"))
        self.assertEqual(found, [])

    def test_when_jev_says_ask_qwen_writes_the_questions(self):
        def handler(request):
            return sse({"role": "assistant", "content": '{"questions": [{"question": "Which platform?"}]}'}, "stop")

        with connected(FakeJev(ask=noul(0.9))):
            found = asyncio.run(kickoff.questions_for({**OWNER, "task": "CURRENT REQUEST:\nMake me a promo video script"},
                                                      fake_client(handler), asyncio.Semaphore(1), "qwen-2"))
        self.assertEqual([q["question"] for q in found], ["Which platform?"])


class RoutingTests(Base):
    def route(self, request, **answers):
        with connected(FakeJev(**answers)):
            return asyncio.run(judge.claude_route({**OWNER, "task": "CURRENT REQUEST:\n" + request}))

    def test_jev_reads_whether_claude_is_asked_to_do_the_work(self):
        # The regex would miss this phrasing; Jev catches it.
        self.assertEqual(self.route("this is a job for claude honestly", asked=noul(0.9), code=noul(0.1)), ("asked", True))
        # The regex would take this as a request; Jev sees Claude is the topic.
        self.assertEqual(self.route("ask me anything you'd ask claude", asked=noul(0.05), code=noul(0.1)), (None, True))

    def test_substantial_coding_goes_to_claude_without_naming_it(self):
        self.assertEqual(self.route("build me a flask app with login and a dashboard", code=noul(0.93)), ("code", True))
        self.assertEqual(self.route("what does this regex do: ^a+$", code=noul(0.2)), (None, True))
        with patch.dict(os.environ, {"COWORK_AUTO_CLAUDE": "off"}):
            self.assertEqual(self.route("build me a flask app", code=noul(0.99)), (None, False))

    def test_without_jev_the_regex_decides(self):
        job = {**OWNER, "task": "CURRENT REQUEST:\nuse claude to build it"}
        self.assertEqual(asyncio.run(judge.claude_route(job)), ("asked", False))

    def test_a_coding_job_is_handed_to_claude_with_a_coding_brief(self):
        seen = {}

        def handler(request):
            body = json.loads(request.content)
            seen.setdefault("lead", body["messages"][1]["content"])
            return sse({"role": "assistant", "content": "Claude built app.py and the tests pass."}, "stop")

        async def handoff(kind, space, job_id, task, timeout=None):
            seen["brief"] = task
            return {"ok": True, "agent": kind, "summary": "Built app.py"}

        fake = FakeJev(ask=noul(0.05), code=noul(0.95), complete=score(4), promises=noul(0.05))

        async def scenario():
            ws.create_job("default", "CURRENT REQUEST:\nbuild me a flask todo app", "cowork", "fast", allow_frontier=True,
                          thread="chat-code")
            job = ws.claim_job()
            with connected(fake), patch.object(hub, "async_client", return_value=fake_client(handler)), \
                 patch.object(escalate, "available", return_value=None), patch.object(escalate, "run", side_effect=handoff):
                return await cowork.run_job(job)

        self.assertEqual(asyncio.run(scenario()), "Claude built app.py and the tests pass.")
        self.assertIn("mainly a software job", seen["brief"])
        self.assertIn("it's mainly a coding job, so it was handed over first", seen["lead"])


class ReplyCheckTests(Base):
    def test_an_incomplete_reply_gets_one_fix_round(self):
        prompts = []

        def handler(request):
            body = json.loads(request.content)
            users = [m["content"] for m in body["messages"] if m["role"] == "user"]
            prompts.append(users[-1])
            if "quality check" in users[-1]:
                return sse({"role": "assistant", "content": "Here are all 10 hooks: …"}, "stop")
            return sse({"role": "assistant", "content": "I'll write the hooks next."}, "stop")

        fake = FakeJev(ask=noul(0.05), code=noul(0.0), complete=score(0.4), promises=noul(0.95))

        async def scenario():
            ws.create_job("default", "CURRENT REQUEST:\nWrite 10 hooks for my gym video", "cowork", "fast",
                          allow_frontier=True, thread="chat-q")
            job = ws.claim_job()
            with connected(fake), patch.object(hub, "async_client", return_value=fake_client(handler)), \
                 patch.object(escalate, "available", return_value="off"):
                return job, await cowork.run_job(job)

        job, answer = asyncio.run(scenario())
        self.assertEqual(answer, "Here are all 10 hooks: …")
        self.assertIn("promises or hands back work", prompts[-1])
        checks = [s for s, q in fake.calls if "complete" in q]
        self.assertEqual(len(checks), 1)  # one fix round, never a loop
        self.assertEqual(checks[0]["reply"], "I'll write the hooks next.")

    def test_a_complete_or_uncertain_reply_is_left_alone(self):
        for answers in ({"complete": score(3.6), "promises": noul(0.1)}, {"complete": score(2.0, 0.9), "promises": noul(0.1)}):
            with connected(FakeJev(**answers)):
                gap = asyncio.run(judge.reply_gap({**OWNER, "task": "CURRENT REQUEST:\nx"}, "done", []))
            self.assertEqual(gap is None, answers["complete"]["score"] >= 2.5)


class ContinuationTests(Base):
    def test_a_finished_report_isnt_continued(self):
        def handler(request):
            body = json.loads(request.content)
            last = body["messages"][-1]
            if last["role"] == "user" and "has to stop now" in str(last["content"]):
                return sse({"role": "assistant", "content": "Report: everything was finished."}, "stop")
            return sse(calls(("run_shell", {"command": "sleep 20", "timeout_seconds": 60})), "tool_calls")

        async def scenario():
            ws.create_job("default", "CURRENT REQUEST:\nLong job", "cowork", "fast", allow_frontier=True, thread="chat-f")
            job = ws.claim_job()
            with connected(FakeJev(ask=noul(0.05), code=noul(0.0), done=noul(0.95))), \
                 patch.dict(cowork.PROFILES["fast"], seconds=2), \
                 patch.object(hub, "async_client", return_value=fake_client(handler)), \
                 patch.object(escalate, "available", return_value="off"):
                return job, await cowork.run_job(job)

        job, answer = asyncio.run(scenario())
        self.assertIn("Stopped at the 0-minute time limit", answer)
        self.assertIn("Report: everything was finished.", answer)
        self.assertFalse(ws.query("SELECT 1 FROM jobs WHERE parent=?", (job["id"],)))


class StudyScreenTests(unittest.TestCase):
    def run_study(self, chance):
        asked = []

        async def ask(content, max_tokens=6000):
            asked.append((content[0]["text"], max_tokens))
            return "CATEGORY: other\nUSEFUL: no\nTITLE: A dance trend\n\n## Summary\nA dance."

        async def screen(material):
            return chance

        async def gather(*args, **kwargs):
            return {"text": "Dancing to a song", "comments": [], "comments_note": None, "frames": [], "author": "",
                    "stats": "", "gaps": []}

        with patch.object(study, "gather", gather), patch.object(study, "saved", return_value=[]):
            asyncio.run(study.study("https://www.tiktok.com/@a/video/1", "default", probe=None, social=None, ask=ask,
                                    small_jpeg=None, facts=None, log=lambda *a: None, x_signed_in=False, screen=screen))
        return asked[0]

    def test_a_post_with_no_tactics_gets_a_short_summary(self):
        text, tokens = self.run_study(0.04)
        self.assertEqual(tokens, 1200)
        self.assertIn("pre-screen found no reusable know-how", text)

    def test_useful_or_unscreened_posts_get_the_full_extraction(self):
        for chance in (0.7, None):
            text, tokens = self.run_study(chance)
            self.assertEqual(tokens, 6000)
            self.assertNotIn("pre-screen", text)


class ApprovalTests(Base):
    def test_jev_withdraws_a_negated_approval_the_regex_accepted(self):
        message = "don't publish post 7 yet, I want to change the caption"
        self.assertTrue(research_tools.approved_post(message, 7))  # the regex alone would publish
        with connected(FakeJev(approves=noul(0.04))):
            self.assertFalse(asyncio.run(judge.confirms_approval(OWNER, message, "publish draft post #7")))
        with connected(FakeJev(approves=noul(0.97))):
            self.assertTrue(asyncio.run(judge.confirms_approval(OWNER, "approve post 7", "publish draft post #7")))
        self.assertIsNone(asyncio.run(judge.confirms_approval(OWNER, message, "publish")))  # no Jev: regex stands

    def test_phone_enter_stays_blocked_when_the_approval_is_negated(self):
        import phone_link
        job = {**OWNER, "task": "CURRENT REQUEST:\ndon't post it, just open the drafts"}
        with connected(FakeJev(approves=noul(0.03))):
            tools = {t.name: t for t in phone_link.agent_tools(job, lambda *a: None, type("B", (), {"active": lambda s: None})(),
                                                               "don't post it, just open the drafts", None, None)}
            self.assertTrue(phone_link.approved("don't post it, just open the drafts"))
            with patch.object(phone_link.BRIDGE, "last_screen", {"elements": []}):
                from agents.tool_context import ToolContext as Invocation
                arguments = json.dumps({"key": "enter"})
                context = Invocation(context=None, tool_name="phone_key", tool_call_id="t", tool_arguments=arguments)
                result = asyncio.run(tools["phone_key"].on_invoke_tool(context, arguments))
        self.assertIn("Enter is only allowed in search fields", str(result))


class TrendRerankTests(unittest.TestCase):
    def test_jev_scores_relevance_in_batches_and_feeds_the_reranker(self):
        from lib import jev_rerank, rerank, schema
        plan = schema.QueryPlan(intent="opinion", freshness_mode="balanced_recent", cluster_mode="none", raw_topic="creatine",
                                subqueries=[schema.SubQuery(label="primary", search_query="creatine",
                                                            ranking_query="What are people saying about creatine?",
                                                            sources=["reddit"])], source_weights={})
        candidates = [schema.Candidate(candidate_id=f"id{n}", item_id=f"i{n}", source="reddit", title=f"Post {n}",
                                       url=f"https://r/{n}", snippet="creatine" if n % 2 else "something else",
                                       subquery_labels=["primary"], native_ranks={"reddit": n}, local_relevance=0.5,
                                       freshness=50, engagement=10, source_quality=0.5, rrf_score=0.01)
                      for n in range(45)]
        bodies = []

        def post(url, body, headers=None, **kwargs):
            bodies.append(body)
            questions = body["questions"]
            assert len(questions) <= 40
            ids = list(body["state"]["candidates"])
            return {"answers": {q: {"type": "score", "score": 4.0 if "creatine" in body["state"]["candidates"][q]["snippet"]
                                    else 0.0, "confidence": 1.0} for q in ids}}

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "apikey_" + "x" * 30}), patch.object(jev_rerank.http, "post", post):
            ranked = rerank.rerank_candidates(topic="creatine", plan=plan, candidates=candidates, provider=None,
                                              model="local-score", shortlist_size=45)
        self.assertEqual([len(b["questions"]) for b in bodies], [40, 5])
        self.assertTrue(all(c.snippet == "creatine" for c in ranked[:22]))
        self.assertTrue(ranked[0].explanation.startswith("jev relevance 4.00/4"))
        # Mock runs (model None) and runs without the key keep the local scoring.
        bodies.clear()
        with patch.object(jev_rerank.http, "post", post):
            rerank.rerank_candidates(topic="creatine", plan=plan, candidates=candidates, provider=None, model="local-score",
                                     shortlist_size=45)
        self.assertEqual(bodies, [])


if __name__ == "__main__":
    unittest.main()
