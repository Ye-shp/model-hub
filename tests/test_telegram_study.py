"""Telegram bridge, link detection and studying posts into the knowledge base (all offline: Telegram, the model and the
social scrapers are faked)."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "tests"))
import workspace as ws
import sandbox
import cowork
import escalate
import console
import links
import study
import telegram_bot
from fastapi.testclient import TestClient
from test_cowork import Base
from test_upgrades import fake_client, AUTH

ANSWER = """CATEGORY: ugc
USEFUL: yes
TITLE: Pay creators per video, not per view, for first UGC tests

## Summary
How to run a first UGC test with 5 creators.
## Tactics and steps
- Pay $150 per video; brief with 3 hooks: "I didn't expect this to work…"
## What the comments add
- @maya (2,100 likes): $80 works on Fiverr.
## How to apply it
- Works for DTC under $50."""

THREAD = {"ok": True, "platform": "x", "url": "https://x.com/growthguy/status/111", "author": "growthguy",
          "author_followers": 52000, "has_video": False,
          "thread": [{"text": "How I run UGC tests on $1k (thread)", "likes": 900, "reposts": 120, "views": 80000},
                     {"text": "1/ Find 5 creators on TikTok Creator Marketplace", "likes": 300}],
          "comments": [{"user": "maya", "text": "$80 works on Fiverr", "likes": 2100}]}


def run(coro):
    return asyncio.run(coro)


class LinkTests(unittest.TestCase):
    def test_platforms(self):
        cases = {
            "https://www.tiktok.com/@creator/video/7412345678901234567": "TikTok video",
            "https://vm.tiktok.com/ZMabc123/": "TikTok video",
            "https://www.instagram.com/reel/C9xYz/?igsh=abc": "Instagram Reel",
            "https://instagram.com/p/C9xYz/": "Instagram post",
            "https://x.com/growthguy/status/1840000000000000000": "X post",
            "https://twitter.com/a/status/1": "X post",
            "https://www.reddit.com/r/marketing/comments/1abcde/how_we_got/": "Reddit thread",
            "https://www.reddit.com/r/marketing/s/AbCdEf": "Reddit thread",
            "https://redd.it/1abcde": "Reddit thread",
            "https://www.youtube.com/shorts/abcDEF123": "YouTube Short",
            "https://youtu.be/abcDEF123": "YouTube video",
            "https://x.com/growthguy": "X profile or page",
            "https://example.com/blog/ugc-guide": "web page",
        }
        for url, label in cases.items():
            self.assertEqual(links.platform_of(url)[1], label, url)

    def test_finding_links_in_a_message(self):
        text = "look at this https://x.com/a/status/1). and (https://vm.tiktok.com/ZMabc/), again https://x.com/a/status/1"
        self.assertEqual(links.find_urls(text), ["https://x.com/a/status/1", "https://vm.tiktok.com/ZMabc/"])
        self.assertEqual(links.describe(["https://x.com/a/status/1", "https://redd.it/x"]), "2 links: X post, Reddit thread")


class StudyTests(Base):
    def fakes(self, answer=ANSWER, thread=THREAD):
        calls = {"social": [], "ask": [], "probe": []}

        async def social(command, args):
            calls["social"].append((command, args))
            if command == "x_thread":
                return thread
            if command == "post_details":
                return {"ok": True, "post": {"uploader": "creator", "like_count": 5000, "description": "UGC hooks that convert"},
                        "comments": [{"user": "sam", "text": "tried hook 2, CTR doubled", "likes": 340}], "comments_note": None}
            return {"ok": False, "error": "unexpected"}

        async def ask(content):
            calls["ask"].append(content)
            return answer

        async def probe(source):
            calls["probe"].append(source)
            return {"video": {"duration": 30.0, "width": 1080, "height": 1920, "fps": 30, "has_audio": True},
                    "frames": [], "shots": {}, "transcript": {"segments": [{"start": 0, "end": 3, "text": "Here's my UGC brief"}],
                                                              "language": "en", "words": 4, "speech_seconds": 3}}, None

        import research_tools
        kwargs = dict(probe=probe, social=social, ask=ask, small_jpeg=lambda p: "data:,", facts=research_tools.facts,
                      log=lambda kind, detail: calls.setdefault("log", []).append((kind, detail)), x_signed_in=True)
        return calls, kwargs

    def test_an_x_thread_is_studied_saved_and_found_later(self):
        calls, kwargs = self.fakes()
        url = "https://x.com/growthguy/status/111"
        result = run(study.study(url, "default", **kwargs))
        self.assertEqual(result["platform"], "X post")
        self.assertTrue(result["saved"])
        self.assertEqual(result["category"], "ugc")
        prompt = calls["ask"][0][0]["text"]
        for expected in ("How I run UGC tests", "1/ Find 5 creators", "@maya", "2100 likes", "52000 followers"):
            self.assertIn(expected, prompt)
        # Any later chat finds it.
        hits = ws.search("default", "creators per video UGC")
        self.assertTrue(hits and hits[0]["source"] == url)
        self.assertIn("Fiverr", hits[0]["content"])
        self.assertEqual(study.index("default", "ugc")[0]["source"], url)
        # Sending it again doesn't redo the work; save='always' replaces the old copy.
        again = run(study.study(url, "default", **kwargs))
        self.assertTrue(again["already"])
        self.assertEqual(len(calls["ask"]), 1)
        self.assertIn("Already in the knowledge base", study.reply(again))
        run(study.study(url, "default", save="always", **kwargs))
        self.assertEqual(len(study.saved("default", url)), 1)
        self.assertEqual(len(calls["ask"]), 2)

    def test_not_useful_posts_are_not_saved_and_never_means_never(self):
        _, kwargs = self.fakes(answer="CATEGORY: other\nUSEFUL: no\nTITLE: A cat video\n\n## Summary\nA cat.")
        result = run(study.study("https://x.com/a/status/222", "default", **kwargs))
        self.assertFalse(result["saved"])
        self.assertIn("Not saved", study.reply(result))
        _, kwargs = self.fakes()
        result = run(study.study("https://x.com/a/status/333", "default", save="never", **kwargs))
        self.assertFalse(result["saved"])
        self.assertEqual(study.saved("default", "https://x.com/a/status/333"), [])

    def test_videos_use_the_transcript_and_the_comments(self):
        calls, kwargs = self.fakes()
        url = "https://www.tiktok.com/@creator/video/7412345678901234567"
        result = run(study.study(url, "default", **kwargs))
        self.assertEqual(result["platform"], "TikTok video")
        self.assertEqual(calls["probe"], [url])
        prompt = calls["ask"][0][0]["text"]
        self.assertIn("Here's my UGC brief", prompt)
        self.assertIn("CTR doubled", prompt)
        self.assertEqual(result["comments_read"], 1)

    def test_x_without_a_sign_in_falls_back_to_the_public_post(self):
        calls, kwargs = self.fakes()
        kwargs["x_signed_in"] = False
        page = {"content": json.dumps({"tweet": {"text": "Public post about GTM", "likes": 10, "author": {"screen_name": "a"},
                                                 "url": "https://x.com/a/status/444", "media": {}}})}

        async def fetch(url, offset=0, max_chars=12000):
            self.assertIn("api.fxtwitter.com/status/444", url)
            return page
        with patch.object(study.web, "fetch", fetch):
            result = run(study.study("https://x.com/a/status/444", "default", **kwargs))
        self.assertNotIn("x_thread", [c[0] for c in calls["social"]])
        self.assertIn("Public post about GTM", calls["ask"][0][0]["text"])
        self.assertIn("Replies need the owner's X sign-in", study.reply(result))

    def test_parse_tolerates_formatting_slips(self):
        found = study.parse("<think>hmm</think>**CATEGORY:** GTM.\n**USEFUL**: Yes\nTitle: \"Launch on Product Hunt\"\n\n## Summary\nx")
        self.assertEqual((found["category"], found["useful"], found["title"]), ("gtm", True, "Launch on Product Hunt"))
        self.assertTrue(found["body"].startswith("## Summary"))
        self.assertEqual(study.parse("no header at all")["category"], "other")

    def test_cowork_has_the_tools_and_knows_about_the_knowledge_base(self):
        job = ws.claim_job() or ws.query("SELECT * FROM jobs WHERE id=?", (ws.create_job(
            "default", "CURRENT REQUEST:\nhi", "cowork", thread="tg-5-1"),))[0]
        space = sandbox.Workspace("owner", "tg-5-1").prepare()
        with patch.object(escalate, "available", return_value="off"):
            agent = cowork.build(job, fake_client(lambda r: None), asyncio.Semaphore(1), space)
        names = {t.name for t in agent.tools}
        self.assertTrue({"study_link", "list_knowledge", "search_knowledge"} <= names)
        self.assertIn("KNOWLEDGE BASE", agent.instructions)
        self.assertIn("TELEGRAM", agent.instructions)


class FakeTelegram:
    """Records Bot API calls and answers them like Telegram would."""
    def __init__(self):
        self.calls = []

    async def __call__(self, token, method, data=None, files=None, timeout=40):
        self.calls.append((method, data or {}, files))
        if method == "getMe":
            return {"username": "hub_bot"}
        if method == "getFile":
            return {"file_path": "videos/file_1.mp4"}
        return True

    def sent(self):
        return [d["text"] for m, d, _ in self.calls if m == "sendMessage"]


class TelegramTests(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeTelegram()
        patcher = patch.object(telegram_bot, "api", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def message(self, text, chat=42, user=42, **extra):
        return {"message_id": 1, "chat": {"id": chat, "type": "private"}, "from": {"id": user}, "text": text, **extra}

    def connect_and_pair(self):
        state = run(telegram_bot.connect("123456789:" + "A" * 35))
        self.assertEqual(state["bot"], "hub_bot")
        run(telegram_bot.handle(self.message(f"/start {state['pairing_code']}")))
        return state

    def test_pairing_is_needed_and_strangers_are_ignored(self):
        state = run(telegram_bot.connect("123456789:" + "A" * 35))
        self.assertFalse(state["paired"])
        run(telegram_bot.handle(self.message("/start 000000")))
        self.assertIn("private bot", self.fake.sent()[-1])
        run(telegram_bot.handle(self.message(f"/start {state['pairing_code']}")))
        self.assertIn("Paired", self.fake.sent()[-1])
        self.assertTrue(telegram_bot.status()["paired"])
        before = len(self.fake.calls)
        run(telegram_bot.handle(self.message("hello", chat=99, user=99)))
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(oct(telegram_bot._path().stat().st_mode & 0o777), "0o600")
        with self.assertRaises(ValueError):
            run(telegram_bot.connect("not-a-token"))

    def test_a_link_gets_its_platform_then_a_task_then_the_reply_and_files(self):
        self.connect_and_pair()

        async def scenario():
            with patch.object(telegram_bot, "watch", lambda job, chat: asyncio.sleep(0)):
                await telegram_bot.handle(self.message("what do you think https://vm.tiktok.com/ZMabc123/"))
            self.assertIn("TikTok video", self.fake.sent()[-1])
            job = ws.query("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1")[0]
            self.assertEqual((job["project"], job["thread"], job["requested_by"]), ("default", "tg-42-1", "telegram"))
            self.assertIn("CURRENT REQUEST:\nwhat do you think https://vm.tiktok.com/ZMabc123/", job["task"])
            self.assertIn(job["id"], telegram_bot.load()["pending"])
            # The task finishes with a reply and a shared file.
            self.assertEqual(ws.claim_job()["id"], job["id"])
            ws.write_artifact("default", job["id"], "playbook.md", b"# notes", "text/markdown")
            ws.finish_job(job["id"], "completed", result="**TikTok video**: saved to the knowledge base")
            await telegram_bot.watch(job["id"], 42, every=0.01)
            self.assertIn("<b>TikTok video</b>", self.fake.sent()[-1])
            self.assertEqual([m for m, _, _ in self.fake.calls].count("sendDocument"), 1)
            self.assertNotIn(job["id"], telegram_bot.load()["pending"])
        run(scenario())

    def test_new_conversations_history_and_files(self):
        self.connect_and_pair()
        run(telegram_bot.handle(self.message("/new")))
        self.assertEqual(telegram_bot.thread_for(telegram_bot.load(), 42), "tg-42-2")
        first = ws.create_job("default", "CURRENT REQUEST:\nfirst question", "cowork", thread="tg-42-2")
        self.assertEqual(ws.claim_job()["id"], first)
        self.assertTrue(ws.finish_job(first, "completed", result="first answer"))

        async def download(token, file_id):
            return b"video bytes"

        async def scenario():
            with patch.object(telegram_bot, "watch", lambda job, chat: asyncio.sleep(0)), \
                    patch.object(telegram_bot, "download", download):
                await telegram_bot.handle(self.message("", video={"file_id": "f1", "file_size": 11, "file_name": "clip.mp4"}))
        run(scenario())
        job = ws.query("SELECT * FROM jobs WHERE thread='tg-42-2' ORDER BY rowid DESC LIMIT 1")[0]
        self.assertIn("USER: first question", job["task"])
        self.assertIn("ASSISTANT: first answer", job["task"])
        self.assertIn("uploads/clip.mp4", job["task"])
        self.assertEqual((sandbox.Workspace("owner", "tg-42-2").dir / "uploads" / "clip.mp4").read_bytes(), b"video bytes")
        self.assertIn("your file", self.fake.sent()[-1])

    def test_markdown_becomes_telegram_html_and_long_replies_split(self):
        html = telegram_bot.to_html("## Hook\n**Bold** and *it* `x<y` [link](https://a.b/c?d=1&e=2)\n- one\n```\na<b\n```")
        self.assertIn("<b>Hook</b>", html)
        self.assertIn("<b>Bold</b> and <i>it</i> <code>x&lt;y</code>", html)
        self.assertIn('<a href="https://a.b/c?d=1&amp;e=2">link</a>', html)
        self.assertIn("• one", html)
        self.assertIn("<pre>a&lt;b</pre>", html)
        parts = telegram_bot.pieces(("paragraph " * 50 + "\n\n") * 30)
        self.assertTrue(len(parts) > 1 and all(len(p) <= telegram_bot.CHUNK for p in parts))

    def test_console_connects_and_disconnects(self):
        client = TestClient(console.create_app("k" * 40, run_worker=False))
        r = client.post("/api/connections/telegram", headers=AUTH, json={"token": "123456789:" + "B" * 35})
        self.assertEqual((r.status_code, r.json()["bot"], r.json()["paired"]), (200, "hub_bot", False))
        self.assertRegex(r.json()["pairing_code"], r"^[0-9a-f]{8}$")
        self.assertTrue(client.get("/api/connections/telegram", headers=AUTH).json()["connected"])
        self.assertFalse(client.post("/api/connections/telegram", headers=AUTH, json={"token": "off"}).json()["connected"])


if __name__ == "__main__":
    unittest.main()
