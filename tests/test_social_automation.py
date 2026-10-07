"""Durable phone queue regressions; temporary data, fake publisher, no device effects."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import social_automation as automation
import store
import workspace as ws


class SimulatedCrash(BaseException):
    pass


class SocialAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = patch.object(store, "DATA", Path(self.temporary.name))
        self.data.start()
        self.now = datetime(2026, 10, 7, 16, 10, tzinfo=timezone.utc)
        self.clock = patch.object(automation, "_clock", side_effect=lambda: self.now)
        self.clock.start()
        self.ready = patch.object(automation, "_readiness", return_value={"ready": True, "missing": [], "adb": "fake-adb"})
        self.readiness = self.ready.start()
        ws.init()
        automation.init()
        self.other = ws.create_project("Other project")
        self.job_id = self.job("default")
        self.other_job = self.job(self.other)

    def tearDown(self):
        self.ready.stop()
        self.clock.stop()
        self.data.stop()
        self.temporary.cleanup()

    def job(self, project):
        ident = "approval-" + project
        with ws.connection() as db, db:
            db.execute("INSERT INTO jobs(id,project,task,skill,profile,status,created_at) VALUES (?,?,?,'cowork','balanced','completed',?)",
                       (ident, project, "Approve a specific draft", automation._iso(self.now)))
        return ident

    def configure(self, platform="tiktok", **kwargs):
        values = dict(device_serial="100.101.102.103:5555", platform=platform, username="owner",
                      timezone_name="America/New_York", country="US")
        values.update(kwargs)
        return automation.configure(**values)

    def post(self, project="default", platform="tiktok", caption="Exactly approved caption"):
        with ws.connection() as db, db:
            ident = db.execute("INSERT INTO social_posts(job_id,project,platform,kind,caption,media,status,created_at,updated_at) "
                               "VALUES (?,?,?,?,?,'[]','draft',?,?)", (self.job_id, project, platform,
                               "reel" if platform == "instagram" else "", caption, automation._iso(self.now), automation._iso(self.now))).lastrowid
        folder = store.DATA / "posts" / str(ident)
        folder.mkdir(parents=True)
        media = folder / "approved.mp4"
        media.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"fake video payload" * 8)
        with ws.connection() as db, db:
            db.execute("UPDATE social_posts SET media=? WHERE id=?", (json.dumps([str(media.resolve())]), ident))
        return ident, media

    def schedule(self, post_id, *, project="default", account="tiktok", when=None, job=None):
        return automation.schedule(project, post_id, account, automation._iso(when or self.now), job or self.job_id)

    def queue(self, post_id):
        return ws.query("SELECT * FROM automation_queue WHERE post_id=?", (post_id,))[0]

    def manual(self, post, account, settings, on_submit):
        return {"ok": True, "status": "awaiting_manual_publish", "submitted": False, "reason": "Review the native composer"}

    def submit(self, post, account, settings, on_submit):
        on_submit()
        return {"ok": True, "status": "needs_confirmation", "submitted": True, "needs_confirmation": True, "reason": "Verify the post"}

    def confirm(self, post_id, remote_id="123456789", **kwargs):
        values = dict(project="default", post_id=post_id, remote_id=remote_id,
                      url=f"https://www.tiktok.com/@owner/video/{remote_id}", published_at=automation._iso(self.now))
        values.update(kwargs)
        return automation.confirm(**values)

    def test_unconfigured_startup_is_disabled_and_has_no_device_or_config_side_effect(self):
        with patch.object(automation, "_publisher") as publisher:
            self.assertIsNone(automation.run_once())
            publisher.assert_not_called()
        result = automation.status("default")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["posts"], [])
        self.assertFalse((store.DATA / "automation" / "config.json").exists())

    def test_config_and_queue_survive_reinitialization_in_same_persistent_data(self):
        self.configure()
        self.configure("instagram")
        post_id, media = self.post()
        queued = self.schedule(post_id, when=self.now + timedelta(hours=2))
        before = media.read_bytes()
        automation.init()
        self.assertEqual(automation.status("default")["posts"][0]["snapshot_hash"], queued["snapshot_hash"])
        self.assertEqual(automation.configured_account("tiktok")["username"], "owner")
        config = json.loads((store.DATA / "automation" / "config.json").read_text())
        self.assertEqual(set(config["accounts"]), {"tiktok", "instagram"})
        self.assertEqual(media.read_bytes(), before)

    def test_approval_snapshot_is_exact_and_append_only_while_memory_is_unchanged(self):
        self.configure()
        post_id, media = self.post(caption="Caption\nwith exact spacing  ")
        with ws.connection() as db, db:
            db.execute("INSERT INTO notes(project,title,content,kind,sources,updated_at,thread) VALUES ('default','Remember','Keep all my memory','fact','[]',?,'chat-one')",
                       (automation._iso(self.now),))
        notes = ws.query("SELECT * FROM notes")
        video_bytes = media.read_bytes()
        first = self.schedule(post_id)
        self.assertEqual(self.schedule(post_id), first)
        saved = ws.query("SELECT * FROM automation_approvals")[0]
        snapshot = json.loads(saved["snapshot"])
        self.assertEqual(snapshot["post"]["caption"], "Caption\nwith exact spacing  ")
        self.assertEqual(snapshot["post"]["media_hashes"], [hashlib.sha256(media.read_bytes()).hexdigest()])
        self.assertEqual(snapshot["approved_by_job"], self.job_id)
        automation.cancel("default", post_id)
        with ws.connection() as db, db:
            db.execute("UPDATE social_posts SET caption='New approved caption' WHERE id=?", (post_id,))
        self.schedule(post_id, when=self.now + timedelta(minutes=30))
        self.assertEqual(ws.query("SELECT * FROM automation_approvals WHERE id=?", (saved["id"],))[0], saved)
        self.assertEqual(len(ws.query("SELECT id FROM automation_approvals")), 2)
        self.assertEqual(ws.query("SELECT * FROM notes"), notes)
        self.assertEqual(media.read_bytes(), video_bytes)

    def test_project_and_approval_job_ownership_are_enforced(self):
        self.configure()
        post_id, _ = self.post()
        with self.assertRaises(ValueError):
            self.schedule(post_id, project=self.other, job=self.other_job)
        with self.assertRaises(ValueError):
            self.schedule(post_id, job=self.other_job)
        self.schedule(post_id)
        for operation in (lambda: automation.cancel(self.other, post_id), lambda: self.confirm(post_id, project=self.other)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(automation.status(self.other)["posts"], [])

    def test_only_private_network_adb_serials_and_supported_accounts_are_accepted(self):
        for serial in ("1.2.3.4:5555", "serial-usb", "100.101.102.103", "user@localhost:5555", "localhost:5555/a"):
            with self.assertRaises(ValueError):
                self.configure(device_serial=serial)
        self.configure(device_serial="localhost:15555")
        self.assertEqual(automation.configured_account("tiktok")["id"], "tiktok")
        self.configure(device_serial="[fd7a:115c:a1e0::1234]:5555")

    def test_video_scope_and_media_folder_boundary(self):
        self.configure()
        for extension, content in ((".jpg", b"image bytes"), (".mp4", b"text pretending to be a video")):
            post_id, media = self.post()
            target = media.with_suffix(extension)
            target.write_bytes(content)
            with ws.connection() as db, db:
                db.execute("UPDATE social_posts SET media=? WHERE id=?", (json.dumps([str(target)]), post_id))
            with self.assertRaises(ValueError):
                self.schedule(post_id)
        post_id, _ = self.post()
        outside = store.DATA / "outside.mp4"
        outside.write_bytes(b"\0\0\0\x18ftypisom" + b"0" * 50)
        with ws.connection() as db, db:
            db.execute("UPDATE social_posts SET media=? WHERE id=?", (json.dumps([str(outside)]), post_id))
        with self.assertRaises(ValueError):
            self.schedule(post_id)
        self.assertEqual(ws.query("SELECT * FROM automation_approvals"), [])

    def test_caption_or_video_changed_after_approval_never_reaches_publisher(self):
        self.configure()
        for change in ("caption", "media"):
            post_id, media = self.post()
            self.schedule(post_id)
            if change == "caption":
                with ws.connection() as db, db:
                    db.execute("UPDATE social_posts SET caption='Unapproved change' WHERE id=?", (post_id,))
            else:
                media.write_bytes(media.read_bytes() + b"new unapproved bytes")
            with patch.object(automation, "_publisher") as publisher:
                result = automation.run_once()
                publisher.assert_not_called()
            self.assertEqual(result["status"], "failed")
            self.assertIn("changed after approval", result["error"])

    def test_changed_media_after_native_preparation_fences_final_submit(self):
        self.configure()
        post_id, media = self.post()
        self.schedule(post_id)
        submits = []
        def publisher(post, account, settings, on_submit):
            media.write_bytes(media.read_bytes() + b"changed while preparing")
            on_submit()
            submits.append(post_id)
        with patch.object(automation, "_publisher", side_effect=publisher):
            self.assertEqual(automation.run_once()["status"], "awaiting_manual_publish")
        self.assertEqual(submits, [])
        self.assertIsNone(self.queue(post_id)["submitted_at"])

    def test_durable_submit_checkpoint_precedes_press_and_survives_crash_without_replay(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        def crash(post, account, settings, on_submit):
            self.assertFalse(settings["dry_run"])
            self.assertEqual(settings["transport"], "direct_adb")
            self.assertEqual(post["kind"], "video")
            on_submit()
            self.assertEqual(self.queue(post_id)["status"], "needs_confirmation")
            self.assertEqual(ws.query("SELECT status FROM social_posts WHERE id=?", (post_id,))[0]["status"], "needs_confirmation")
            raise SimulatedCrash()
        with patch.object(automation, "_publisher", side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                automation.run_once()
        automation.init()
        with patch.object(automation, "_publisher") as publisher:
            self.assertEqual(automation.run_once()["status"], "blocked")
            publisher.assert_not_called()
        with self.assertRaises(ValueError):
            self.schedule(post_id)
        self.assertEqual(self.queue(post_id)["attempts"], 1)

    def test_crash_before_submit_can_recover_same_immutable_approval(self):
        self.configure()
        post_id, _ = self.post()
        original = self.schedule(post_id)["snapshot_hash"]
        with patch.object(automation, "_publisher", side_effect=SimulatedCrash()):
            with self.assertRaises(SimulatedCrash):
                automation.run_once()
        self.assertEqual(self.queue(post_id)["status"], "processing")
        with patch.object(automation, "_publisher", side_effect=self.submit) as publisher:
            result = automation.run_once()
            publisher.assert_called_once()
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["snapshot_hash"], original)
        self.assertEqual(result["attempts"], 2)

    def test_two_workers_cannot_claim_or_use_the_same_phone_concurrently(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        started, release = threading.Event(), threading.Event()
        def publisher(*args):
            started.set()
            self.assertTrue(release.wait(4))
            return self.submit(*args)
        with patch.object(automation, "_publisher", side_effect=publisher) as publishing, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(automation.run_once)
            self.assertTrue(started.wait(4))
            self.assertEqual(pool.submit(automation.run_once).result(4)["status"], "busy")
            release.set()
            self.assertEqual(first.result(4)["status"], "needs_confirmation")
            publishing.assert_called_once()

    def test_processing_cancellation_is_rejected_until_native_thread_finishes(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        started, release = threading.Event(), threading.Event()
        submits = []
        def publisher(post, account, settings, on_submit):
            started.set()
            self.assertTrue(release.wait(4))
            on_submit()
            submits.append(post_id)
        with patch.object(automation, "_publisher", side_effect=publisher), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(automation.run_once)
            self.assertTrue(started.wait(4))
            with self.assertRaises(ValueError):
                automation.cancel("default", post_id)
            release.set()
            self.assertEqual(pending.result(4)["status"], "needs_confirmation")
        self.assertEqual(submits, [post_id])
        self.assertIsNotNone(self.queue(post_id)["submitted_at"])

    def test_cancel_queued_work_never_reaches_native_preparation(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        result = automation.cancel("default", post_id)
        self.assertEqual(result["status"], "canceled")
        with patch.object(automation, "_publisher") as publisher:
            self.assertIsNone(automation.run_once())
            publisher.assert_not_called()

    def test_manual_hold_blocks_other_posts_and_requires_explicit_abandon_receipt(self):
        self.configure()
        first, _ = self.post()
        second, _ = self.post()
        self.schedule(first)
        self.schedule(second, when=self.now + timedelta(minutes=30))
        with patch.object(automation, "_publisher", side_effect=self.manual):
            self.assertEqual(automation.run_once()["status"], "awaiting_manual_publish")
        self.now += timedelta(minutes=30)
        with patch.object(automation, "_publisher") as publisher:
            self.assertEqual(automation.run_once()["status"], "blocked")
            publisher.assert_not_called()
        self.assertEqual(automation.status("default")["blocking_post_id"], first)
        with self.assertRaises(ValueError):
            automation.cancel("default", first)
        abandoned = automation.cancel("default", first, verified_not_published=True)
        self.assertEqual(abandoned["status"], "abandoned")
        self.assertIn("Owner verified not published", abandoned["error"])
        with self.assertRaises(ValueError):
            self.schedule(first)
        with patch.object(automation, "_publisher", side_effect=self.submit):
            self.assertEqual(automation.run_once()["post_id"], second)

    def test_owner_cannot_abandon_while_submit_thread_is_still_running(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        started, release = threading.Event(), threading.Event()
        def publisher(post, account, settings, on_submit):
            on_submit()
            started.set()
            self.assertTrue(release.wait(4))
            return {"status": "needs_confirmation", "submitted": True}
        with patch.object(automation, "_publisher", side_effect=publisher), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(automation.run_once)
            self.assertTrue(started.wait(4))
            with self.assertRaises(ValueError):
                automation.cancel("default", post_id, verified_not_published=True)
            release.set()
            pending.result(4)
        abandoned = automation.cancel("default", post_id, verified_not_published=True)
        self.assertEqual(abandoned["status"], "abandoned")
        self.assertFalse(abandoned["result"]["needs_confirmation"])
        self.assertIsNotNone(self.queue(post_id)["submitted_at"])

    def test_confirm_is_validated_idempotent_and_cannot_reassign_publication(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        with patch.object(automation, "_publisher", side_effect=self.submit):
            automation.run_once()
        invalid = ("http://www.tiktok.com/@owner/video/123456789", "https://evil.example/@owner/video/123456789",
                   "https://www.tiktok.com:9000/@owner/video/123456789", "https://user@www.tiktok.com/@owner/video/123456789",
                   "https://www.tiktok.com/@someoneelse/video/123456789", "https://www.tiktok.com/@owner/video/999")
        for url in invalid:
            with self.assertRaises(ValueError):
                self.confirm(post_id, url=url)
        with self.assertRaises(ValueError):
            self.confirm(post_id, published_at="2026-10-07T12:00:00")
        confirmed = self.confirm(post_id)
        self.assertEqual(self.confirm(post_id), confirmed)
        self.assertEqual(confirmed["status"], "published")
        self.assertEqual(confirmed["result"]["confirmation"], "owner_recorded")
        self.assertFalse(confirmed["result"]["needs_confirmation"])
        self.assertNotIn("reason", confirmed["result"])
        self.assertNotIn("error", confirmed["result"])
        self.assertEqual(confirmed["result"]["submission_receipt"]["reason"], "Verify the post")
        with self.assertRaises(ValueError):
            self.confirm(post_id, remote_id="999")
        with self.assertRaises(ValueError):
            automation.cancel("default", post_id, verified_not_published=True)

    def test_instagram_native_confirmation_uses_actual_permalink_shortcode(self):
        self.configure("instagram")
        post_id, _ = self.post(platform="instagram")
        self.schedule(post_id, account="instagram")
        with patch.object(automation, "_publisher", side_effect=self.manual):
            automation.run_once()
        with self.assertRaises(ValueError):
            self.confirm(post_id, remote_id="999", url="https://www.instagram.com/reel/RealCode_12/")
        confirmed = self.confirm(post_id, remote_id="RealCode_12", url="https://www.instagram.com/reel/RealCode_12/")
        self.assertEqual(confirmed["remote_id"], "RealCode_12")

    def test_publisher_claiming_submission_without_callback_is_held_never_retried(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        with patch.object(automation, "_publisher", return_value={"submitted": True, "ok": False, "retryable": True}):
            result = automation.run_once()
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertIsNotNone(result["submitted_at"])
        with patch.object(automation, "_publisher") as publisher:
            automation.run_once()
            publisher.assert_not_called()

    def test_verified_native_result_can_publish_but_invalid_evidence_remains_unknown(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        def verified(post, account, settings, on_submit):
            on_submit()
            return {"ok": True, "status": "published", "id": "123456789",
                    "url": "https://www.tiktok.com/@owner/video/123456789", "published_at": automation._iso(self.now)}
        with patch.object(automation, "_publisher", side_effect=verified):
            result = automation.run_once()
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["result"]["confirmation"], "native_verified")
        self.now += timedelta(minutes=30)
        other, _ = self.post()
        self.schedule(other)
        with patch.object(automation, "_publisher", side_effect=verified):
            result = automation.run_once()  # same remote post must not be assigned to two drafts
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertIn("already recorded", result["error"])

    def test_missing_runtime_does_not_claim_or_fail_the_approved_job(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        self.readiness.return_value = {"ready": False, "missing": ["adb"]}
        with patch.object(automation, "_publisher") as publisher:
            self.assertEqual(automation.run_once()["status"], "blocked")
            publisher.assert_not_called()
        self.assertEqual(self.queue(post_id)["status"], "queued")
        self.assertEqual(self.queue(post_id)["attempts"], 0)

    def test_future_due_time_is_utc_normalized_and_never_runs_early(self):
        self.configure()
        post_id, _ = self.post()
        due = self.now + timedelta(hours=2)
        result = automation.schedule("default", post_id, "tiktok", "2026-10-07T14:10:00-04:00", self.job_id)
        self.assertEqual(result["run_at"], automation._iso(due))
        with patch.object(automation, "_publisher") as publisher:
            self.assertIsNone(automation.run_once())
            publisher.assert_not_called()

    def test_cadence_rejects_close_reservations_and_outside_local_window(self):
        self.configure()
        first, _ = self.post()
        second, _ = self.post()
        self.schedule(first)
        with self.assertRaisesRegex(ValueError, "20 minutes"):
            self.schedule(second, when=self.now + timedelta(minutes=5))
        with self.assertRaisesRegex(ValueError, "Posting window"):
            self.schedule(second, when=self.now.replace(hour=3) + timedelta(days=1))
        self.assertEqual(ws.query("SELECT count(*) AS n FROM automation_approvals")[0]["n"], 1)

    def test_daily_cadence_ceiling_counts_reserved_posts_in_account_local_day(self):
        self.configure("instagram")
        for index in range(5):
            post_id, _ = self.post(platform="instagram")
            self.schedule(post_id, account="instagram", when=self.now + timedelta(minutes=20 * index))
        extra, _ = self.post(platform="instagram")
        with self.assertRaisesRegex(ValueError, "already has 5 approved posts"):
            self.schedule(extra, account="instagram", when=self.now + timedelta(minutes=100))

    def test_cadence_late_execution_is_visibly_blocked_without_rewriting_approval(self):
        self.configure()
        post_id, _ = self.post()
        queued = self.schedule(post_id)
        self.now += timedelta(hours=15)
        with patch.object(automation, "_publisher") as publisher:
            result = automation.run_once()
            publisher.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["run_at"], queued["run_at"])
        self.assertEqual(result["snapshot_hash"], queued["snapshot_hash"])

    def test_cadence_is_rechecked_after_long_preparation_before_final_press(self):
        self.configure()
        self.now = self.now.replace(hour=1, minute=50) + timedelta(days=1)  # 21:50 NY, inside the window
        post_id, _ = self.post()
        queued = self.schedule(post_id)
        submits = []
        def publisher(post, account, settings, on_submit):
            self.now += timedelta(minutes=20)  # 22:10 NY; final tap is no longer permitted
            on_submit()
            submits.append(post_id)
        with patch.object(automation, "_publisher", side_effect=publisher):
            result = automation.run_once()
        self.assertEqual(result["status"], "awaiting_manual_publish")
        self.assertIn("Posting window", result["error"])
        self.assertEqual(result["snapshot_hash"], queued["snapshot_hash"])
        self.assertIsNone(result["submitted_at"])
        self.assertEqual(submits, [])

    def test_only_pre_submit_transient_failures_have_bounded_retries(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        with patch.object(automation, "_publisher", side_effect=TimeoutError("Disconnected before compose")) as publisher:
            for attempt in range(3):
                result = automation.run_once()
                self.assertEqual(result["attempts"], attempt + 1)
                self.assertIsNone(result["submitted_at"])
                if attempt < 2:
                    self.assertEqual(result["status"], "queued")
                    self.now = automation._time(result["next_attempt_at"], "Retry")
            self.assertEqual(result["status"], "failed")
            self.assertIsNone(automation.run_once())
            self.assertEqual(publisher.call_count, 3)

    def test_screenshot_evidence_is_a_project_artifact_not_a_private_data_path(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        folder = store.DATA / "automation" / "screenshots"
        folder.mkdir()
        image = folder / "evidence.png"
        image.write_bytes(b"screenshot bytes")
        with patch.object(automation, "_publisher", return_value={"ok": True, "status": "awaiting_manual_publish", "screenshot": str(image)}):
            result = automation.run_once()
        self.assertNotIn("screenshot", result["result"])
        artifact = result["result"]["verification_artifact"]
        self.assertEqual(ws.query("SELECT project FROM artifacts WHERE id=?", (artifact["id"],))[0]["project"], "default")
        self.assertEqual(artifact["url"], "/api/artifacts/" + artifact["id"])

    def test_async_worker_cancellation_does_not_release_phone_while_thread_is_active(self):
        self.configure()
        post_id, _ = self.post()
        self.schedule(post_id)
        started, release = threading.Event(), threading.Event()
        def publisher(*args):
            started.set()
            self.assertTrue(release.wait(4))
            return self.submit(*args)
        async def scenario():
            task = asyncio.create_task(automation.serve())
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(await asyncio.to_thread(automation.run_once), {"status": "busy"})
            release.set()
        with patch.object(automation, "_publisher", side_effect=publisher):
            asyncio.run(scenario())  # shutdown also waits for the independent publisher thread
        self.assertEqual(self.queue(post_id)["status"], "needs_confirmation")

    def test_confirm_links_existing_audience_variant_and_keeps_identity_errors_visible(self):
        import audience
        audience.init()
        self.configure()
        post_id, _ = self.post()
        experiment = audience.create_experiment("default", "Existing test", "Use this generated video", account="owner")
        variant = audience.register_variant("default", experiment["id"], "A", "Approved response", post_id=post_id)
        self.schedule(post_id)
        with patch.object(automation, "_publisher", side_effect=self.manual):
            automation.run_once()
        with patch.object(audience, "_clock", return_value=self.now):
            result = self.confirm(post_id)
        self.assertEqual(result["result"]["audience_variant_id"], variant["id"])
        self.assertEqual(audience.variant_detail("default", variant["id"])["remote_id"], "123456789")
        self.assertEqual(ws.query("SELECT count(*) AS n FROM audience_checkpoints")[0]["n"], 3)

    def test_audience_identity_mismatch_is_visible_without_reassigning_old_experiment(self):
        import audience
        audience.init()
        self.configure()
        post_id, _ = self.post()
        experiment = audience.create_experiment("default", "Existing analytics identity", "Use the native video", account="another-account")
        variant = audience.register_variant("default", experiment["id"], "A", "Approved response", post_id=post_id)
        self.schedule(post_id)
        with patch.object(automation, "_publisher", side_effect=self.manual):
            automation.run_once()
        with patch.object(audience, "_clock", return_value=self.now):
            result = self.confirm(post_id)
        self.assertEqual(result["status"], "published")
        self.assertIn("audience tracking needs reconciliation", result["result"]["audience_warning"])
        self.assertIsNone(audience.variant_detail("default", variant["id"])["remote_id"])
        self.assertEqual(audience.experiment_detail("default", experiment["id"])["experiment"]["account"], "another-account")

    def test_global_hold_does_not_expose_another_projects_post_id(self):
        self.configure()
        post_id, _ = self.post(project=self.other)
        self.schedule(post_id, project=self.other, job=self.other_job)
        with patch.object(automation, "_publisher", side_effect=self.manual):
            automation.run_once()
        result = automation.status("default")
        self.assertTrue(result["blocked"])
        self.assertIsNone(result["blocking_post_id"])
        self.assertEqual(result["posts"], [])


if __name__ == "__main__":
    unittest.main()
