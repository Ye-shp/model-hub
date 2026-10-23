"""Offline checks: evidence, scoped action review, duplicate prevention and bounded observation."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import social_missions as sm
import store


class SocialMissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = patch.object(store, "DATA", Path(self.temp.name))
        self.data.start()
        self.addCleanup(self.data.stop)
        self.addCleanup(self.temp.cleanup)
        self.stopped = []
        self.session = sm.Session("default", "Find Turkish pottery groups and join relevant ones.",
                                  None, None, self.stopped.append)
        self.screen = {"app": "com.facebook.katana", "elements": [], "jpeg": ""}

    def observe(self, speech=False):
        self.session.observation = {"evidence": {"subject": "A potter making a bowl", "speech_needed": speech},
                                    "screen": self.screen, "time": time.monotonic()}

    def test_no_comment_without_post_evidence_or_when_audio_is_missing(self):
        with self.assertRaisesRegex(ValueError, "actual post"):
            asyncio.run(self.session.prepare("comment", "Nice bowl", self.screen))
        self.observe(speech=True)
        with self.assertRaisesRegex(ValueError, "Speech"):
            asyncio.run(self.session.prepare("comment", "Nice bowl", self.screen))

    def test_irrelevant_comment_rejected_and_same_candidate_not_reanalyzed(self):
        self.observe()
        self.session.decide = AsyncMock(return_value={"allowed": False, "reason": "The post is about pottery, not cars"})
        result = asyncio.run(self.session.prepare("comment", "Great car!", self.screen))
        self.assertFalse(result["allowed"])
        self.assertIsNone(self.session.review)
        with self.assertRaisesRegex(ValueError, "already reviewed"):
            asyncio.run(self.session.prepare("comment", "Great car!", self.screen))
        self.assertEqual(self.session.decide.await_count, 1)

    def test_relevant_group_join_is_scoped_and_durably_fenced(self):
        self.session.decide = AsyncMock(side_effect=[
            {"allowed": True, "target": "Istanbul Pottery", "reason": "Matches mission and visible rules"},
            {"navigation_only": False, "matches_review": True},
        ])
        result = asyncio.run(self.session.prepare("join", "", self.screen))
        self.assertTrue(result["allowed"])
        self.assertTrue(asyncio.run(self.session.commit(self.screen, 20, 30, "phone")))
        self.assertIsNone(self.session.review)
        with self.assertRaisesRegex(PermissionError, "already attempted"):
            sm.reserve_action("default", "phone", "Istanbul Pottery", "join", "")

    def test_changed_target_or_text_cannot_submit(self):
        self.session.review = {"action": "reply", "text": "Nice bowl", "target": "potter/123",
                               "screen": self.screen, "time": time.monotonic()}
        self.session.decide = AsyncMock(return_value={"navigation_only": False, "matches_review": False,
                                                      "reason": "Different video"})
        with self.assertRaisesRegex(PermissionError, "Different video"):
            asyncio.run(self.session.commit(self.screen, 20, 30, "phone"))
        self.assertEqual(self.session.actions, 0)

    def test_navigation_does_not_require_action_approval(self):
        self.session.decide = AsyncMock(return_value={"navigation_only": True, "matches_review": False})
        self.assertFalse(asyncio.run(self.session.commit(self.screen, 20, 30, "phone")))
        self.assertEqual(self.session.actions, 0)

    def test_repeated_inspection_stops_and_navigation_invalidates_context(self):
        for _ in range(3):
            self.session.inspected()
        with self.assertRaisesRegex(RuntimeError, "analysis loop"):
            self.session.inspected()
        self.assertEqual(len(self.stopped), 1)
        self.observe()
        self.session.moved(changed_post=True)
        self.assertIsNone(self.session.observation)
        self.assertEqual(self.session.reads, 0)

    def test_persona_is_persistent_and_project_scoped(self):
        self.assertIn("No custom persona", sm.persona("default"))
        sm.persona("default", "A ceramics studio: warm, concise Turkish; no invented personal experiences.")
        self.assertIn("Turkish", sm.persona("default"))
        self.assertIn("No custom persona", sm.persona("another"))

    def test_malformed_review_fails_closed(self):
        self.session.decide = AsyncMock(return_value={"allowed": "yes", "target": "Group"})
        result = asyncio.run(self.session.prepare("join", "", self.screen))
        self.assertFalse(result["allowed"])

    def test_task_can_continue_past_five_actions_without_losing_duplicate_fence(self):
        self.session.actions = 5
        self.session.review = {"action": "join", "text": "", "target": "Group",
                               "screen": self.screen, "time": time.monotonic()}
        self.session.decide = AsyncMock(return_value={"navigation_only": False, "matches_review": True})
        self.assertTrue(asyncio.run(self.session.commit(self.screen, 20, 30, "phone")))
        self.assertEqual(self.session.actions, 6)
        self.assertFalse(self.stopped)
        with self.assertRaisesRegex(PermissionError, "already attempted"):
            sm.reserve_action("default", "phone", "Group", "join", "")

    def test_direct_post_requires_details_and_reviews_the_requested_media(self):
        self.session.request = "Publish my pottery clip to my Facebook page with caption New bowl."
        with self.assertRaisesRegex(ValueError, "exact operation"):
            asyncio.run(self.session.prepare("post", "New bowl", self.screen))
        self.session.decide = AsyncMock(return_value={"allowed": True, "target": "my page / pottery clip / New bowl"})
        result = asyncio.run(self.session.prepare("post", "New bowl", self.screen,
                                                "Selected pottery clip, my page, public audience"))
        self.assertTrue(result["allowed"])
        self.assertIn("pottery clip", self.session.review["details"])
        prompt = self.session.decide.call_args.args[0]
        self.assertIn("selected media", prompt)
        self.assertIn("Publish my pottery clip", prompt)

    def test_message_and_device_operation_require_a_scoped_review(self):
        for action in ("message", "device"):
            self.session.decide = AsyncMock(return_value={"allowed": False, "reason": "Outside the requested task"})
            result = asyncio.run(self.session.prepare(action, "", self.screen, "Unrelated operation"))
            self.assertFalse(result["allowed"])
            self.assertIsNone(self.session.review)

    def test_long_press_is_reviewed_as_a_distinct_gesture(self):
        self.session.decide = AsyncMock(return_value={"navigation_only": True})
        self.assertFalse(asyncio.run(self.session.commit(self.screen, 20, 30, "phone", "long_press")))
        self.assertIn('"gesture": "long_press"', self.session.decide.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
