"""A new conversation request must not silently resume a different stopped task."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "tests"))

import sandbox
import workspace as ws
from cowork.continuity import current_request, read_plan, recap
from cowork.intent import wants_claude
from test_cowork import Base


class RequestBoundaryTests(unittest.TestCase):
    def test_the_appended_request_wins_over_a_header_quoted_in_history(self):
        task = ("CONVERSATION SO FAR (earlier turns of this chat):\nUSER: earlier example"
                "\n\nCURRENT REQUEST:\nUse Claude to build the old app"
                "\n\nASSISTANT: That was finished.\n\nCURRENT REQUEST:\nExplain what a comet is")
        self.assertEqual(current_request(task), "Explain what a comet is")
        self.assertFalse(wants_claude(task))

    def test_a_real_claude_request_after_history_is_preserved(self):
        task = "CONVERSATION SO FAR:\nUSER: earlier\n\nCURRENT REQUEST:\nUse Claude to fix this app"
        self.assertEqual(current_request(task), "Use Claude to fix this app")
        self.assertTrue(wants_claude(task))

    def test_unwrapped_text_and_inline_marker_are_preserved(self):
        for task in ("Explain comets", "Explain this text: CURRENT REQUEST:\nUse Claude", "Resume my draft"):
            self.assertEqual(current_request(task), task)

    def test_a_starting_header_and_windows_line_endings_work(self):
        self.assertEqual(current_request("CURRENT REQUEST:\ncontinue"), "continue")
        self.assertEqual(current_request("History\r\n\r\nCURRENT REQUEST:\r\nresume"), "resume")


class ChatContinuityTests(Base):
    def stopped(self, request="Build the ALPHA shopping app", thread="chat-a", status="interrupted"):
        identity = ws.create_job("default", "CURRENT REQUEST:\n" + request, "cowork", thread=thread)
        job = ws.claim_job()
        self.assertEqual(job["id"], identity)
        ws.event(identity, "tool", "Saved ALPHA's scaffold")
        if status == "cancelled":
            ws.cancel_job(identity)
        else:
            ws.finish_job(identity, status, error="Work stopped")
        return identity

    def next_job(self, request, skill="chat", thread="chat-a", parent=None):
        identity = ws.create_job("default", request, skill, thread=thread, parent=parent)
        job = ws.claim_job()
        self.assertEqual(job["id"], identity)
        return job

    def test_unrelated_chat_and_cowork_requests_do_not_resume_stopped_work(self):
        for skill, status in (("chat", "interrupted"), ("cowork", "cancelled")):
            with self.subTest(skill=skill, status=status):
                self.stopped(status=status)
                job = self.next_job("CURRENT REQUEST:\nExplain what a comet is", skill=skill)
                self.assertEqual(recap(job), "")
                ws.finish_job(job["id"], "completed", "A comet is an icy body.")

    def test_an_unrelated_partial_completion_does_not_force_continuation(self):
        previous = self.stopped(status="failed")
        ws.event(previous, "partial", "Reached the time limit")
        job = self.next_job("CURRENT REQUEST:\nCalculate 12 times 17")
        self.assertEqual(recap(job), "")

    def test_explicit_resume_commands_preserve_the_stopped_task_evidence(self):
        for request in ("continue", "Please resume the app", "keep going", "carry on from where you stopped",
                        "pick up where you left off"):
            with self.subTest(request=request):
                self.stopped()
                job = self.next_job("CURRENT REQUEST:\n" + request)
                text = recap(job)
                self.assertIn("Build the ALPHA shopping app", text)
                self.assertIn("Saved ALPHA's scaffold", text)
                self.assertIn("Continue from where that work stopped", text)
                ws.finish_job(job["id"], "completed", "Done")

    def test_mentioning_resume_or_a_historical_command_is_not_current_resume_intent(self):
        self.stopped()
        task = ("CONVERSATION SO FAR:\nUSER: continue\n\nCURRENT REQUEST:\n"
                "What does the word resume mean?")
        self.assertEqual(recap(self.next_job(task)), "")

    def test_continue_with_a_different_subject_does_not_resume_the_old_app(self):
        self.stopped()
        self.assertEqual(recap(self.next_job("CURRENT REQUEST:\ncontinue explaining comets")), "")

    def test_continue_with_the_same_subject_preserves_the_previous_work(self):
        self.stopped("Explain comets")
        text = recap(self.next_job("CURRENT REQUEST:\ncontinue explaining comets"))
        self.assertIn("Request: Explain comets", text)

    def test_a_resume_in_a_different_chat_does_not_read_other_chat_work(self):
        self.stopped()
        self.assertEqual(recap(self.next_job("CURRENT REQUEST:\ncontinue", thread="chat-b")), "")

    def test_an_automatic_continuation_uses_its_parent_despite_an_intervening_request(self):
        parent = self.stopped()
        intervening = self.stopped("Write unrelated BETA poetry")
        job = self.next_job("AUTOMATIC CONTINUATION 1\n\nCURRENT REQUEST:\nBuild the ALPHA shopping app",
                            skill="cowork", parent=parent)
        text = recap(job)
        self.assertIn("Build the ALPHA shopping app", text)
        self.assertNotIn("BETA", text)
        self.assertNotEqual(parent, intervening)

    def test_completed_parent_phase_is_evidence_for_its_automatic_next_phase(self):
        parent = ws.create_job("default", "CURRENT REQUEST:\nBuild the scaffold", "cowork", thread="chat-a")
        ws.claim_job()
        ws.finish_job(parent, "completed", "Scaffold saved in app/")
        job = self.next_job("AUTOMATIC NEXT PHASE\n\nCURRENT REQUEST:\nBuild the pages", skill="cowork", parent=parent)
        text = recap(job)
        self.assertIn("THE PREVIOUS PHASE OF THIS SAME WORK", text)
        self.assertIn("Scaffold saved in app/", text)
        self.assertNotIn("STOPPED BEFORE FINISHING", text)

    def test_automatic_parent_must_belong_to_the_same_chat(self):
        parent = self.stopped()
        job = self.next_job("AUTOMATIC CONTINUATION 1\n\nCURRENT REQUEST:\nBuild the app", skill="cowork",
                            thread="chat-b", parent=parent)
        self.assertEqual(recap(job), "")

    def test_retry_of_the_same_job_keeps_its_own_earlier_attempt(self):
        identity = ws.create_job("default", "CURRENT REQUEST:\nBuild the ALPHA app", "cowork", thread="chat-a")
        ws.claim_job()
        ws.event(identity, "started", "Attempt 1")
        ws.event(identity, "tool", "Saved ALPHA scaffold")
        ws.finish_job(identity, "interrupted", error="Restarted")
        self.assertTrue(ws.resume_job(identity))
        job = ws.claim_job()
        ws.event(identity, "started", "Attempt 2")
        text = recap(job)
        self.assertIn("AN EARLIER ATTEMPT OF THIS SAME TASK", text)
        self.assertIn("Saved ALPHA scaffold", text)

    def test_omitting_stale_resume_context_keeps_files_and_plan_available(self):
        self.stopped()
        space = sandbox.Workspace("owner", "chat-a").prepare()
        space.write_text("draft.md", "Existing draft")
        space.write_text("plan.md", "# Existing project plan")
        job = self.next_job("CURRENT REQUEST:\nExplain comets")
        self.assertEqual(recap(job), "")
        self.assertIn("Existing draft", space.read_text("draft.md"))
        self.assertEqual(read_plan(space), "# Existing project plan")


if __name__ == "__main__":
    unittest.main()
