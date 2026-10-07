"""Cowork boundaries for the persistent native-phone publisher; no device/network activity."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))

from agents.tool_context import ToolContext
import research_tools
import social_automation
import store
import toolbox
import workspace as ws


class PhoneAutomationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = store.DATA
        store.DATA = Path(self.temp.name) / "data"
        self.chat = Path(self.temp.name) / "chat"
        self.chat.mkdir()
        ws.init()
        research_tools.init()
        self.job_id = ws.create_job("default", "Prepare a post", "cowork", thread="phone-test")
        with ws.connection() as db, db:
            self.post_id = db.execute("""INSERT INTO social_posts
                (project,platform,kind,caption,media,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""", ("default", "tiktok", "video", "Original caption", "[]", "draft",
                                               store.now(), store.now())).lastrowid

    def tearDown(self):
        store.DATA = self.old_data
        self.temp.cleanup()

    def tools(self, request="", owner=True):
        space = SimpleNamespace(is_owner=owner, dir=self.chat, resolve=lambda p: self.chat / p,
                                write_text=lambda p, text: (self.chat / p).write_text(text, encoding="utf-8"))
        with patch.object(toolbox, "ready", return_value=True):
            tools, _ = research_tools.build_tools({"id": self.job_id, "project": "default"}, space, None,
                                                 asyncio.Semaphore(1), lambda *a: None,
                                                 SimpleNamespace(active=lambda: None), request, "qwen-2")
        return {t.name: t for t in tools}

    def invoke(self, tool, args):
        text = json.dumps(args)
        context = ToolContext(context=None, tool_name=tool.name, tool_call_id="offline", tool_arguments=text)
        return asyncio.run(tool.on_invoke_tool(context, text))

    def test_native_lifecycle_tools_are_owner_only_and_excluded_from_helpers(self):
        names = {"configure_phone_automation", "phone_automation_status", "schedule_post",
                 "cancel_scheduled_post", "confirm_scheduled_post"}
        self.assertTrue(names <= set(self.tools()))
        self.assertFalse(names & set(self.tools(owner=False)))
        self.assertTrue(names <= research_tools.OWNER_ONLY_TOOLS)

    def test_schedule_requires_current_specific_draft_approval(self):
        args = {"post_id": self.post_id, "account_id": "tiktok", "run_at": "2026-10-20T12:00:00-04:00"}
        with patch.object(social_automation, "schedule") as schedule:
            reply = self.invoke(self.tools("Make a posting plan")["schedule_post"], args)
        self.assertIn("Not approved", reply)
        schedule.assert_not_called()

    def test_negated_approval_cannot_queue_and_explicit_approval_records_provenance(self):
        args = {"post_id": self.post_id, "account_id": "tiktok", "run_at": "2026-10-20T12:00:00-04:00"}
        with patch("cowork.judge.confirms_approval", new_callable=AsyncMock, return_value=False), \
                patch.object(social_automation, "schedule") as schedule:
            reply = self.invoke(self.tools(f"Do not approve post {self.post_id}")["schedule_post"], args)
        self.assertIn("Not approved", reply)
        schedule.assert_not_called()
        with patch("cowork.judge.confirms_approval", new_callable=AsyncMock, return_value=True), \
                patch.object(social_automation, "schedule", return_value={"status": "queued"}) as schedule:
            reply = self.invoke(self.tools(f"approve post {self.post_id} for that time")["schedule_post"], args)
        self.assertEqual(json.loads(reply)["status"], "queued")
        schedule.assert_called_once_with("default", self.post_id, "tiktok", args["run_at"], self.job_id)

    def test_unavailable_semantic_checker_never_accepts_unsafe_approval(self):
        args = {"post_id": self.post_id, "account_id": "tiktok", "run_at": "2026-10-20T12:00:00-04:00"}
        messages = (f"Do not approve post {self.post_id}", f"Should I approve post {self.post_id}?",
                    f"approve post {self.post_id} if the caption is changed", f'Example: "approve post {self.post_id}"',
                    f"> approve post {self.post_id}", f"She said approve post {self.post_id}",
                    f"`approve post {self.post_id}`", f"I won't publish post {self.post_id}")
        with patch("cowork.judge.confirms_approval", new_callable=AsyncMock, return_value=None), \
                patch.object(social_automation, "schedule") as schedule:
            for message in messages:
                self.assertIn("Not approved", self.invoke(self.tools(message)["schedule_post"], args), message)
            schedule.assert_not_called()
        with patch("cowork.judge.confirms_approval", new_callable=AsyncMock, return_value=None), \
                patch.object(social_automation, "schedule", return_value={"status": "queued"}) as schedule:
            self.assertEqual(json.loads(self.invoke(self.tools(f"Please approve post {self.post_id}")["schedule_post"], args))["status"], "queued")
            schedule.assert_called_once()

    def test_configured_tiktok_publish_uses_queue_without_legacy_bridge_or_api(self):
        with patch("cowork.judge.confirms_approval", new_callable=AsyncMock, return_value=True), \
                patch.object(social_automation, "configured_account", return_value={"id": "tiktok"}), \
                patch.object(social_automation, "schedule", return_value={"status": "queued"}) as schedule, \
                patch.object(toolbox, "run_social", new_callable=AsyncMock) as api, \
                patch("phone_link.push_file", new_callable=AsyncMock) as transfer:
            reply = self.invoke(self.tools(f"approve post {self.post_id}")["publish_post"], {"post_id": self.post_id})
        self.assertEqual(json.loads(reply)["status"], "queued")
        self.assertEqual(schedule.call_args.args[:3], ("default", self.post_id, "tiktok"))
        api.assert_not_awaited()
        transfer.assert_not_awaited()

    def test_manual_confirmation_requires_the_owners_actual_link(self):
        args = {"post_id": self.post_id, "remote_id": "12345", "url": "https://www.tiktok.com/@owner/video/12345",
                "published_at": "2026-10-07T12:00:00+00:00"}
        with patch.object(social_automation, "confirm", return_value={"status": "published"}) as confirm:
            reply = self.invoke(self.tools("Did it work?")["confirm_scheduled_post"], args)
            self.assertIn("actual published post link", reply)
            confirm.assert_not_called()
            with patch("cowork.judge.confirms_approval", new_callable=AsyncMock, return_value=None):
                reply = self.invoke(self.tools(f"Post {self.post_id} was published at {args['published_at']}: {args['url']}")["confirm_scheduled_post"], args)
        self.assertEqual(json.loads(reply)["status"], "published")
        confirm.assert_called_once_with("default", self.post_id, args["remote_id"], args["url"], args["published_at"])

    def test_unrelated_link_or_invented_timestamp_cannot_reconcile_a_publication(self):
        args = {"post_id": self.post_id, "remote_id": "12345", "url": "https://www.tiktok.com/@owner/video/12345",
                "published_at": "2026-10-07T12:00:00+00:00"}
        messages = (f"Study the editing in this video: {args['url']}",
                    f"Post {self.post_id} was published here: {args['url']}",
                    f"Post {self.post_id} was not published at {args['published_at']}: {args['url']}",
                    f"Was post {self.post_id} published at {args['published_at']}? {args['url']}",
                    f'Example: "Post {self.post_id} was published at {args["published_at"]}: {args["url"]}"')
        with patch("cowork.judge.confirms_approval", new_callable=AsyncMock, return_value=None), \
                patch.object(social_automation, "confirm") as confirm:
            for message in messages:
                self.assertIn("owner must confirm", self.invoke(self.tools(message)["confirm_scheduled_post"], args), message)
            confirm.assert_not_called()

    def test_abandonment_requires_verified_no_post_and_discarded_composer(self):
        args = {"post_id": self.post_id, "verified_not_published": True}
        with patch.object(social_automation, "cancel") as cancel:
            for text in (f"Post {self.post_id} did not publish", f"Discarded the draft for post {self.post_id}",
                         f"If post {self.post_id} did not publish, I discarded the composer",
                         f"Did post {self.post_id} not publish? I cleared its composer",
                         f"Post {self.post_id} did not publish. I have NOT discarded its composer.",
                         f'Example: "Post {self.post_id} did not publish. I discarded its composer."'):
                reply = self.invoke(self.tools(text)["cancel_scheduled_post"], args)
                self.assertIn("owner must explicitly confirm", reply)
            cancel.assert_not_called()
        with patch("cowork.judge.confirms_approval", new_callable=AsyncMock, return_value=True), \
                patch.object(social_automation, "cancel", return_value={"status": "abandoned"}) as cancel:
            reply = self.invoke(self.tools(f"Post {self.post_id} did not publish. I checked the phone and discarded its composer.")["cancel_scheduled_post"], args)
        self.assertEqual(json.loads(reply)["status"], "abandoned")
        cancel.assert_called_once_with("default", self.post_id, verified_not_published=True)
        with patch("cowork.judge.confirms_approval", new_callable=AsyncMock, return_value=None), \
                patch.object(social_automation, "cancel", return_value={"status": "abandoned"}) as cancel:
            reply = self.invoke(self.tools(f"Post {self.post_id} did not publish and I discarded its composer.")["cancel_scheduled_post"], args)
        self.assertEqual(json.loads(reply)["status"], "abandoned")
        cancel.assert_called_once()

    def test_ordinary_cancel_does_not_claim_verified_abandonment(self):
        with patch.object(social_automation, "cancel", return_value={"status": "canceled"}) as cancel:
            reply = self.invoke(self.tools(f"Cancel scheduled post {self.post_id}")["cancel_scheduled_post"],
                                {"post_id": self.post_id})
        self.assertEqual(json.loads(reply)["status"], "canceled")
        cancel.assert_called_once_with("default", self.post_id, verified_not_published=False)

    def test_new_skill_dispatches_through_cowork_and_loads_guidance(self):
        import skills
        from cowork.prompt import instructions
        self.assertIn("social-automation", ws.AGENT_SKILLS)
        self.assertIn("social-automation", {s["id"] for s in skills.catalog()})
        space = SimpleNamespace(is_owner=True, dir=self.chat)
        job = {"skill": "social-automation", "profile": "fast", "allow_images": False, "task": "Post my video"}
        prompt = instructions(job, space, research="Tools installed")
        self.assertIn("NATIVE PHONE POSTING", prompt)
        self.assertIn("Never repeat an uncertain submission", prompt)


if __name__ == "__main__":
    unittest.main()
