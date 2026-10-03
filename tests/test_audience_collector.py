"""Offline collector/worker regressions; no posts, accounts, or model calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import audience_metrics as metrics
import audience_worker as worker
import audience
import store
import workspace as ws


TOKEN = "test-secret-must-never-appear"


class MetricsTests(unittest.TestCase):
    def task(self, platform="tiktok"):
        return {"id": 1, "platform": platform, "account": "open-id" if platform == "tiktok" else "1234",
                "remote_id": "7123456789012345678", "url": "https://www.instagram.com/reel/test_post/" if platform == "instagram"
                else "https://malicious.invalid/ignored"}

    def credentials(self, platform="tiktok"):
        return {platform: {"account_id" if platform == "tiktok" else "user_id": "open-id" if platform == "tiktok" else "1234",
                           "access_token": TOKEN}}

    def test_tiktok_requests_owned_video_with_counts_not_a_public_url(self):
        calls = []

        def request(method, url, token, body=None):
            calls.append((method, url, token, body))
            if "/user/info/" in url:
                return {"data": {"user": {"open_id": "open-id"}}}
            return {"data": {"videos": [{"id": self.task()["remote_id"], "view_count": 100, "like_count": 0,
                                         "comment_count": 3, "share_count": 10}]}}

        result = metrics.collect_sync(self.task(), credentials=self.credentials(), request=request)
        self.assertEqual(result["metrics"], {"views": 100, "likes": 0, "comments": 3, "shares": 10, "saves": None, "reach": None})
        self.assertEqual(calls[1][3], {"filters": {"video_ids": [self.task()["remote_id"]]}})
        self.assertTrue(all(TOKEN not in call[1] and "malicious.invalid" not in call[1] for call in calls))
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(result["source"], "tiktok_display_api")

    def test_tiktok_rejects_token_identity_mismatch_before_video_request(self):
        calls = []

        def request(*args):
            calls.append(args)
            return {"data": {"user": {"open_id": "other-account"}}}

        with self.assertRaises(metrics.MetricsError) as error:
            metrics.collect_sync(self.task(), credentials=self.credentials(), request=request)
        self.assertEqual(error.exception.code, "account_mismatch")
        self.assertEqual(len(calls), 1)

    def test_tiktok_missing_owned_video_is_never_zero(self):
        def request(method, url, token, body=None):
            return {"data": {"user": {"open_id": "open-id"}}} if "/user/info/" in url else {"data": {"videos": []}}

        with self.assertRaises(metrics.MetricsError) as error:
            metrics.collect_sync(self.task(), credentials=self.credentials(), request=request)
        self.assertEqual(error.exception.code, "ownership_unverified")

    def test_invalid_post_id_or_wrong_config_account_never_sends_a_request(self):
        for task in ({**self.task(), "remote_id": "../me?access_token=bad"}, {**self.task(), "account": "someone-else"}):
            with self.subTest(task=task), self.assertRaises(metrics.MetricsError):
                metrics.collect_sync(task, credentials=self.credentials(), request=lambda *args: self.fail("request sent"))

    def test_instagram_keeps_partial_metrics_and_visible_permission_warning(self):
        def request(method, url, token, body=None):
            if "/insights?" not in url:
                return {"id": self.task("instagram")["remote_id"], "owner": {"id": "1234"}, "like_count": 7, "comments_count": 0,
                        "permalink": "https://instagram.com/reel/test_post?utm_source=ignored"}
            field = url.split("metric=")[1]
            if field == "shares":
                raise metrics.MetricsError("permission_missing")
            if field == "saved":
                return {"data": []}
            return {"data": [{"name": field, "total_value": {"value": 120} if field == "views" else {"value": 100}}]}

        result = metrics.collect_sync(self.task("instagram"), credentials=self.credentials("instagram"), request=request)
        self.assertEqual(result["metrics"], {"views": 120, "likes": 7, "comments": 0, "shares": None, "saves": None, "reach": 100})
        self.assertIn("shares: permission_missing", result["warnings"])
        self.assertIn("saves: not returned by the API", result["warnings"])
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_instagram_foreign_owner_rejected_before_any_insights(self):
        calls = []

        def request(*args):
            calls.append(args)
            return {"id": self.task("instagram")["remote_id"], "owner": {"id": "5678"}, "like_count": 999999}

        with self.assertRaises(metrics.MetricsError) as error:
            metrics.collect_sync(self.task("instagram"), credentials=self.credentials("instagram"), request=request)
        self.assertEqual(error.exception.code, "ownership_unverified")
        self.assertEqual(len(calls), 1)

    def test_instagram_all_missing_metrics_fail_and_unknown_owner_fails(self):
        for owner in ({"id": "1234"}, None):
            def request(method, url, token, body=None):
                if "/insights?" in url:
                    return {"data": []}
                return {"id": self.task("instagram")["remote_id"], "owner": owner, "permalink": self.task("instagram")["url"]}
            with self.subTest(owner=owner), self.assertRaises(metrics.MetricsError):
                metrics.collect_sync(self.task("instagram"), credentials=self.credentials("instagram"), request=request)

    def test_instagram_unavailable_one_metric_does_not_erase_other_metrics(self):
        def request(method, url, token, body=None):
            if "/insights?" not in url:
                return {"id": self.task("instagram")["remote_id"], "owner": {"id": "1234"}, "like_count": 1,
                        "permalink": self.task("instagram")["url"]}
            field = url.split("metric=")[1]
            if field == "saved":
                raise metrics.MetricsError("metric_unavailable")
            return {"data": [{"name": field, "values": [{"value": 0}]}]}

        result = metrics.collect_sync(self.task("instagram"), credentials=self.credentials("instagram"), request=request)
        self.assertEqual(result["metrics"]["views"], 0)
        self.assertEqual(result["metrics"]["shares"], 0)
        self.assertIsNone(result["metrics"]["saves"])
        self.assertIn("saves: metric_unavailable", result["warnings"])

    def test_instagram_owned_post_with_wrong_or_missing_permalink_is_rejected_before_insights(self):
        for link in ("https://www.instagram.com/reel/different_post/", None, "https://evil.invalid/reel/test_post/"):
            calls = []

            def request(*args):
                calls.append(args)
                return {"id": self.task("instagram")["remote_id"], "owner": {"id": "1234"}, "like_count": 999999,
                        "permalink": link}

            with self.subTest(link=link), self.assertRaises(metrics.MetricsError) as error:
                metrics.collect_sync(self.task("instagram"), credentials=self.credentials("instagram"), request=request)
            self.assertEqual(error.exception.code, "publication_mismatch")
            self.assertEqual(len(calls), 1)

    def test_provider_error_body_and_token_are_not_exposed(self):
        class Opener:
            def open(self, request, timeout):
                self.assertions(request)
                raise urllib.error.HTTPError(request.full_url, 400, "secret " + TOKEN, {},
                                             io.BytesIO(json.dumps({"error": {"code": 190, "message": TOKEN}}).encode()))

            @staticmethod
            def assertions(request):
                assert request.get_header("Authorization") == "Bearer " + TOKEN
                assert TOKEN not in request.full_url

        with patch("urllib.request.build_opener", return_value=Opener()), self.assertRaises(metrics.MetricsError) as error:
            metrics._request_json("GET", "https://graph.instagram.com/v23.0/1234?fields=id", TOKEN)
        self.assertEqual(error.exception.code, "credentials_expired")
        self.assertNotIn(TOKEN, str(error.exception))

    def test_redirects_and_arbitrary_endpoints_are_refused(self):
        self.assertIsNone(metrics._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid/"))
        for url in ("https://evil.invalid/123", "http://graph.instagram.com/123", "https://open.tiktokapis.com.evil.invalid/123"):
            with self.subTest(url=url), self.assertRaises(metrics.MetricsError):
                metrics._request_json("GET", url, TOKEN)
        with patch.dict("os.environ", {"IG_GRAPH_BASE": "https://evil.invalid/v23.0"}), self.assertRaises(metrics.MetricsError):
            metrics._instagram_base()

    def test_count_parser_preserves_unavailable_values(self):
        for value in (None, True, -1, 1.5, "1K", {}, 2**64):
            self.assertIsNone(metrics._count(value))
        self.assertEqual(metrics._count(0), 0)
        self.assertEqual(metrics._count("123"), 123)
        self.assertIsNone(metrics._insight_count({"data": [{"name": "views", "values": [{"value": 10}, {"value": 20}]}]}, "views"))

    def test_standalone_worker_loads_env_before_selecting_database(self):
        # Fresh interpreter with the real env loader, isolated .env, and no API calls.
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder)
            data = fixture / "persistent-data"
            shutil.copyfile(ROOT / "agents" / "hub.py", fixture / "hub.py")
            (fixture / ".env").write_text("HUB_DATA_DIR=" + str(data) + "\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.pop("HUB_DATA_DIR", None)
            code = ("import sys; sys.path[:0] = " + repr([str(fixture), str(ROOT / "agents")])
                    + "; import audience_worker, store; print(store.DATA)")
            run = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, "Fresh worker import failed")
            self.assertEqual(Path(run.stdout.strip()), data)


class QueueFixture:
    """Small repository double; durable restart/fencing is covered with real core below."""

    def __init__(self, attempts=1):
        self.task = {"id": "task-1", "lease_token": "lease-1", "attempts": attempts}
        self.finished, self.failed, self.initialized = [], [], False

    def init(self):
        self.initialized = True

    def claim_due(self, **kwargs):
        task, self.task = self.task, None
        return task

    def finish_collection(self, *args, **kwargs):
        self.finished.append((args, kwargs))

    def fail_collection(self, *args, **kwargs):
        self.failed.append((args, kwargs))


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_passes_lease_metrics_source_and_warnings(self):
        repository = QueueFixture()

        async def collector(task):
            return {"metrics": {"views": 10, "saves": None}, "source": "tiktok_display_api", "observed_at": "2026-10-03T00:00:00+00:00",
                    "warnings": ["saves: unavailable"]}

        result = await worker.collect_once(repository=repository, collector=collector)
        self.assertEqual(result["status"], "collected")
        self.assertEqual(repository.finished[0][0][:2], ("task-1", "lease-1"))
        self.assertEqual(repository.finished[0][1]["warnings"], ["saves: unavailable"])
        self.assertEqual(repository.failed, [])

    async def test_failure_retries_with_bounded_backoff_without_raw_exception(self):
        repository = QueueFixture(attempts=3)

        async def collector(task):
            raise RuntimeError("private failure " + TOKEN)

        result = await worker.collect_once(repository=repository, collector=collector)
        self.assertEqual(result["retry_after_seconds"], 240)
        self.assertEqual(repository.finished, [])
        self.assertNotIn(TOKEN, str(repository.failed))
        self.assertEqual(worker.retry_delay(100), 21600)

    async def test_rate_limit_cooldown_is_honoured(self):
        repository = QueueFixture()

        async def collector(task):
            raise metrics.MetricsError("rate_limited", 900)

        result = await worker.collect_once(repository=repository, collector=collector)
        self.assertEqual(result["retry_after_seconds"], 900)
        self.assertIn("rate_limited", repository.failed[0][0][2])

    async def test_timeout_retries_before_lease_expires(self):
        repository = QueueFixture()

        async def collector(task):
            await asyncio.Event().wait()

        result = await worker.collect_once(repository=repository, collector=collector, timeout_seconds=0.01, lease_seconds=31)
        self.assertEqual(result["status"], "retry")
        self.assertIn("timed out", repository.failed[0][0][2])
        self.assertEqual(repository.finished, [])

    async def test_cancellation_releases_lease_and_does_not_finish(self):
        repository, started = QueueFixture(), asyncio.Event()

        async def collector(task):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(worker.collect_once(repository=repository, collector=collector))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(repository.finished, [])
        self.assertEqual(repository.failed[0][0][:2], ("task-1", "lease-1"))
        self.assertEqual(repository.failed[0][1]["retry_after_seconds"], 60)

    async def test_worker_service_runs_without_any_chat_or_model_and_stops(self):
        repository, stop = QueueFixture(), asyncio.Event()

        async def collector(task):
            stop.set()
            return {"metrics": {"views": 20}, "source": "fixture"}

        await worker.serve(repository=repository, collector=collector, poll_seconds=0.01, stop_event=stop)
        self.assertTrue(repository.initialized)
        self.assertEqual(len(repository.finished), 1)

    async def test_invalid_timeout_lease_configuration_does_not_claim(self):
        repository = QueueFixture()
        with self.assertRaises(ValueError):
            await worker.collect_once(repository=repository, lease_seconds=60, timeout_seconds=120)
        self.assertIsNotNone(repository.task)


class DurableWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(store, "DATA", Path(self.temp.name))
        self.data_patch.start()
        self.now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
        self.clock_patch = patch.object(audience, "_clock", side_effect=lambda: self.now)
        self.clock_patch.start()
        ws.init()
        audience.init()
        experiment = audience.create_experiment("default", "Hook trial", "Compare two hooks", account="open-id")
        variant = audience.register_variant("default", experiment["id"], "A", "Show the result first")
        self.variant_id = variant["id"]
        audience.confirm_publication("default", self.variant_id, "7123456789012345678",
                                     "https://www.tiktok.com/@example/video/7123456789012345678",
                                     self.now - timedelta(hours=24), "open-id")

    def tearDown(self):
        self.clock_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    async def collect(self, task):
        return {"metrics": {"views": 100, "saves": None}, "source": "tiktok_display_api",
                "observed_at": self.now.isoformat(), "warnings": ["saves: not exposed by TikTok Display API"]}

    def checkpoint(self):
        return ws.query("SELECT * FROM audience_checkpoints WHERE variant_id=? AND horizon_hours=24", (self.variant_id,))[0]

    async def test_hard_restart_reclaims_expired_lease_and_fences_old_worker(self):
        old_task = audience.claim_due()
        # Simulate the process exiting without cleanup. Reinitialization preserves rows.
        audience.init()
        self.assertIsNone(audience.claim_due())
        self.now += timedelta(seconds=181)
        new_task = audience.claim_due()
        self.assertEqual(new_task["id"], old_task["id"])
        self.assertEqual(new_task["attempts"], 2)
        self.assertNotEqual(new_task["lease_token"], old_task["lease_token"])
        with self.assertRaises(ValueError):
            audience.finish_collection(old_task["id"], old_task["lease_token"], {"views": 999999}, "fixture")
        self.assertEqual(audience.variant_detail("default", self.variant_id)["snapshots"], [])
        audience.finish_collection(new_task["id"], new_task["lease_token"], {"views": 100}, "fixture")
        self.assertEqual(self.checkpoint()["status"], "done")
        self.assertEqual(len(audience.variant_detail("default", self.variant_id)["snapshots"]), 1)

    async def test_two_workers_cannot_claim_the_same_checkpoint(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = list(executor.map(lambda _: audience.claim_due(), range(2)))
        self.assertEqual(sum(task is not None for task in tasks), 1)

    async def test_retry_survives_restart_then_success_records_one_snapshot(self):
        async def unavailable(task):
            raise metrics.MetricsError("provider_unavailable")

        result = await worker.collect_once(repository=audience, collector=unavailable)
        self.assertEqual(result["status"], "retry")
        self.assertEqual(self.checkpoint()["status"], "retry")
        audience.init()
        self.now += timedelta(seconds=59)
        self.assertEqual((await worker.collect_once(repository=audience, collector=self.collect))["status"], "idle")
        self.now += timedelta(seconds=1)
        self.assertEqual((await worker.collect_once(repository=audience, collector=self.collect))["status"], "collected")
        self.assertEqual(self.checkpoint()["attempts"], 2)
        snapshots = audience.variant_detail("default", self.variant_id)["snapshots"]
        self.assertEqual(len(snapshots), 1)
        self.assertIsNone(snapshots[0]["metrics"]["saves"])
        self.assertIn("saves: not exposed by TikTok Display API", snapshots[0]["warnings"])

    async def test_cancelled_collection_persists_retry_for_next_process(self):
        started = asyncio.Event()

        async def blocked(task):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(worker.collect_once(repository=audience, collector=blocked))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.checkpoint()["status"], "retry")
        self.assertIsNone(self.checkpoint()["lease_token"])
        self.assertEqual(audience.variant_detail("default", self.variant_id)["snapshots"], [])
        audience.init()
        self.now += timedelta(seconds=60)
        self.assertEqual((await worker.collect_once(repository=audience, collector=self.collect))["status"], "collected")

    async def test_late_collector_cannot_finish_or_fail_a_replaced_lease(self):
        async def late(task):
            self.now += timedelta(seconds=181)
            self.new_owner = audience.claim_due()
            return await self.collect(task)

        result = await worker.collect_once(repository=audience, collector=late)
        self.assertEqual(result["status"], "lease_lost")
        self.assertEqual(self.checkpoint()["lease_token"], self.new_owner["lease_token"])
        self.assertEqual(audience.variant_detail("default", self.variant_id)["snapshots"], [])

    async def test_invalid_all_null_result_is_retried_not_recorded(self):
        async def unavailable(task):
            return {"metrics": {"views": None, "likes": None}, "source": "fixture"}

        result = await worker.collect_once(repository=audience, collector=unavailable)
        self.assertEqual(result["status"], "retry")
        self.assertEqual(self.checkpoint()["status"], "retry")
        self.assertEqual(audience.variant_detail("default", self.variant_id)["snapshots"], [])


if __name__ == "__main__":
    unittest.main()
