"""Outcome provenance, project isolation, comparable rewards and training holdouts."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import audience
import store
import workspace as ws


class AudienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = store.DATA
        store.DATA = Path(self.temp.name)
        self.now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        self.clock = patch.object(audience, "_clock", side_effect=lambda: self.now)
        self.clock.start()
        ws.init()
        audience.init()
        self.project = ws.create_project("Other project")

    def tearDown(self):
        self.clock.stop()
        store.DATA = self.old_data
        self.temp.cleanup()

    def experiment(self, project="default", **kwargs):
        return audience.create_experiment(project, "Hook test", "Create a 20-second product demo", account="account1", **kwargs)

    def variant(self, experiment, label="A", response=None, **kwargs):
        return audience.register_variant(experiment["project"], experiment["id"], label, response or f"Script {label}", **kwargs)

    def publish(self, variant, remote_id=None, published=None, **kwargs):
        remote_id = remote_id or str(1000000 + variant["id"])
        return audience.confirm_publication(variant["project"], variant["id"], remote_id,
                                            f"https://www.tiktok.com/@owner/video/{remote_id}",
                                            audience._iso(published or self.now - timedelta(hours=72)), "account1", **kwargs)

    def observe(self, variant, views, **metrics):
        return audience.record_snapshot(variant["project"], variant["id"], {"views": views, **metrics}, audience._iso(self.now))

    def test_restart_retains_experiments_publication_and_all_checkpoints(self):
        experiment = self.experiment()
        variant = self.publish(self.variant(experiment))
        audience.init()
        detail = audience.experiment_detail("default", experiment["id"])
        self.assertEqual(detail["variants"][0]["remote_id"], variant["remote_id"])
        self.assertEqual([c["horizon_hours"] for c in detail["checkpoints"]], [24, 72, 168])
        self.assertEqual(len(audience.list_experiments("default")), 1)
        self.assertTrue(all("lease_token" not in c for c in detail["checkpoints"]))

    def test_every_write_and_query_is_project_scoped(self):
        experiment = self.experiment()
        variant = self.publish(self.variant(experiment))
        for action in (
            lambda: audience.register_variant(self.project, experiment["id"], "B", "Wrong project"),
            lambda: audience.variant_detail(self.project, variant["id"]),
            lambda: audience.experiment_detail(self.project, experiment["id"]),
            lambda: audience.record_snapshot(self.project, variant["id"], {"views": 3000}),
            lambda: audience.confirm_publication(self.project, variant["id"], "9", "https://www.tiktok.com/@x/video/9", audience._iso(self.now), "account1"),
        ):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(audience.performance_summary(self.project)["results"], [])
        self.assertEqual(audience.export_preferences(self.project)["records"], [])

    def test_publication_idempotent_and_cannot_reassign_or_duplicate(self):
        experiment = self.experiment()
        variant = self.variant(experiment)
        self.publish(variant, "999")
        self.publish(variant, "999")
        self.assertEqual(len(audience.experiment_detail("default", experiment["id"])["checkpoints"]), 3)
        with self.assertRaises(ValueError):
            self.publish(variant, "1000")
        with self.assertRaises(ValueError):
            self.publish(self.variant(experiment, "B"), "999")
        with self.assertRaises(ValueError):
            audience.confirm_publication("default", variant["id"], "999", "https://www.tiktok.com/@x/video/888", audience._iso(self.now), "account1")

    def test_urls_accounts_and_timestamp_validation(self):
        variant = self.variant(self.experiment())
        for url in ("http://www.tiktok.com/@x/video/1", "https://evil.example/@x/video/1", "https://www.tiktok.com:9000/@x/video/1",
                    "https://secret@www.tiktok.com/@x/video/1", "https://www.tiktok.com/@x/video/2"):
            with self.assertRaises(ValueError):
                audience.confirm_publication("default", variant["id"], "1", url, audience._iso(self.now), "account1")
        with self.assertRaises(ValueError):
            audience.confirm_publication("default", variant["id"], "1", "https://www.tiktok.com/@x/video/1", "2026-10-03T12:00:00", "account1")
        with self.assertRaises(ValueError):
            audience.confirm_publication("default", variant["id"], "1", "https://www.tiktok.com/@x/video/1", audience._iso(self.now), "someoneelse")

    def test_missing_is_not_zero_and_snapshot_is_immutable(self):
        variant = self.publish(self.variant(self.experiment()))
        observed = self.observe(variant, 0, shares=None)
        self.assertEqual(observed["metrics"]["views"], 0)
        self.assertIsNone(observed["metrics"]["likes"])
        self.assertIsNone(observed["metrics"]["shares"])
        self.assertEqual(self.observe(variant, 0, shares=None)["id"], observed["id"])
        with self.assertRaises(ValueError):
            self.observe(variant, 1)
        self.assertEqual(len(audience.variant_detail("default", variant["id"])["snapshots"]), 1)

    def test_invalid_metrics_do_not_enter_store(self):
        variant = self.publish(self.variant(self.experiment()))
        for metrics in ({"views": True}, {"views": -1}, {"views": float("nan")}, {"views": float("inf")},
                        {"views": "2"}, {"views": 2.5}, {"views": None}, {"completion_rate": 1.1}, {"unknown": 1}):
            with self.assertRaises(ValueError):
                audience.record_snapshot("default", variant["id"], metrics)
        self.assertEqual(audience.variant_detail("default", variant["id"])["snapshots"], [])

    def test_unknown_unpublished_outcome_is_not_a_loser(self):
        experiment = self.experiment()
        self.observe(self.publish(self.variant(experiment, "A")), 10000)
        self.variant(experiment, "B")
        exported = audience.export_preferences("default")
        self.assertEqual(exported["records"], [])
        self.assertIn("two organic", exported["skipped"][0]["reason"])

    def test_same_brief_measured_variants_export_soup_format_and_audit(self):
        experiment = self.experiment()
        a, b = self.publish(self.variant(experiment, "A")), self.publish(self.variant(experiment, "B"))
        self.observe(a, 1000, shares=10)
        self.observe(b, 4000, shares=80)
        exported = audience.export_preferences("default")
        self.assertEqual(exported["records"], [{"prompt": experiment["brief"], "chosen": "Script B", "rejected": "Script A"}])
        self.assertEqual(exported["comparisons"][0]["chosen_id"], b["id"])
        self.assertEqual(exported["comparisons"][0]["chosen"]["observation"]["metrics"]["views"], 4000)
        self.assertEqual(exported["comparisons"][0]["chosen"]["observation"]["source"], "manual")
        self.assertEqual(exported["comparisons"][0]["chosen"]["url"], b["url"])
        self.assertEqual(exported["comparisons"][0]["experiment"]["split"], "train")
        self.assertEqual(exported["dataset_digest"], audience.export_preferences("default")["dataset_digest"])
        self.assertGreater(exported["comparisons"][0]["margin"], 1)
        self.assertLess(exported["comparisons"][0]["confidence"], 1)
        self.assertEqual(len(audience.experiment_detail("default", experiment["id"])["checkpoints"]), 6)

    def test_holdouts_and_different_briefs_never_become_pairs(self):
        held_out = self.experiment(split="eval")
        for label, views in (("A", 1000), ("B", 8000)):
            self.observe(self.publish(self.variant(held_out, label)), views)
        for views in (1000, 8000):
            self.observe(self.publish(self.variant(self.experiment())), views)
        result = audience.export_preferences("default")
        self.assertEqual(result["records"], [])
        self.assertTrue(any(s["reason"] == "Held out for evaluation" for s in result["skipped"]))
        self.assertFalse(audience.experiment_detail("default", held_out["id"])["variants"][0]["reward"]["eligible"])

    def test_paid_unknown_small_and_identical_examples_excluded(self):
        for exposure in ("paid", "unknown", "mixed"):
            exp = self.experiment()
            self.observe(self.publish(self.variant(exp, "A")), 1000)
            self.observe(self.publish(self.variant(exp, "B"), exposure=exposure), 8000)
        exp = self.experiment()
        self.observe(self.publish(self.variant(exp, "A")), 100)
        self.observe(self.publish(self.variant(exp, "B")), 4000)
        exp = self.experiment()
        for label, views in (("A", 1000), ("B", 8000)):
            self.observe(self.publish(self.variant(exp, label, "Identical script")), views)
        self.assertEqual(audience.export_preferences("default")["records"], [])

    def test_age_mismatch_and_old_lifetime_counts_are_not_fresh_checkpoints(self):
        exp = self.experiment()
        a = self.publish(self.variant(exp, "A"), published=self.now - timedelta(hours=60))
        b = self.publish(self.variant(exp, "B"), published=self.now - timedelta(hours=84))
        self.observe(a, 1000)
        self.observe(b, 8000)
        self.assertEqual(audience.export_preferences("default")["records"], [])
        old = self.publish(self.variant(self.experiment()), published=self.now - timedelta(days=30))
        self.observe(old, 2000000)
        self.assertIsNone(audience.experiment_detail("default", old["experiment_id"])["variants"][0]["reward"]["score"])

    def test_manual_import_satisfies_the_matching_checkpoint_only(self):
        exp = self.experiment()
        variant = self.publish(self.variant(exp))
        self.observe(variant, 1000)
        checkpoints = audience.experiment_detail("default", exp["id"])["checkpoints"]
        self.assertEqual({c["horizon_hours"]: c["status"] for c in checkpoints}, {24: "queued", 72: "done", 168: "queued"})

    def test_historical_baseline_excludes_other_context_account_paid_and_eval(self):
        exp = self.experiment()
        old = self.publish(self.variant(exp), published=self.now - timedelta(days=10))
        audience.record_snapshot("default", old["id"], {"views": 1000}, audience._iso(self.now - timedelta(days=7)))
        held_out = self.experiment(split="eval")
        old_eval = self.publish(self.variant(held_out), published=self.now - timedelta(days=9))
        audience.record_snapshot("default", old_eval["id"], {"views": 1000000}, audience._iso(self.now - timedelta(days=6)))
        current = self.publish(self.variant(self.experiment()))
        self.observe(current, 4000)
        reward = audience.experiment_detail("default", current["experiment_id"])["variants"][0]["reward"]
        self.assertEqual(reward["baseline_views"], 1000)
        self.assertEqual(reward["baseline_count"], 1)
        self.assertGreater(reward["reach_lift"], 3)
        self.assertEqual(reward["baseline_kind"], "historical")

    def test_linked_draft_keeps_actual_job_and_asset_hash_and_confirms_status(self):
        with ws.connection() as db:
            db.executescript("CREATE TABLE social_posts (id INTEGER PRIMARY KEY,project TEXT,platform TEXT,job_id TEXT,media TEXT,result TEXT,status TEXT,updated_at TEXT);")
        media = store.DATA / "posts" / "1" / "final.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"final edited asset")
        with ws.connection() as db, db:
            db.execute("INSERT INTO social_posts VALUES (1,'default','tiktok','original-job',?,NULL,'on_phone',?)", (json.dumps([str(media)]), store.now()))
        variant = self.variant(self.experiment(), post_id=1, job_id="followup-job")
        self.assertEqual(variant["job_id"], "original-job")
        self.assertEqual(variant["media_hashes"], [hashlib.sha256(media.read_bytes()).hexdigest()])
        with self.assertRaises(ValueError):
            self.variant(self.experiment(self.project), post_id=1)
        self.publish(variant)
        post = ws.query("SELECT * FROM social_posts WHERE id=1")[0]
        self.assertEqual(post["status"], "published")
        self.assertEqual(json.loads(post["result"])["confirmation"], "owner_recorded")

    def test_baseline_does_not_use_outcomes_observed_after_experiment_started(self):
        prior = self.publish(self.variant(self.experiment()), published=self.now - timedelta(days=4))
        audience.record_snapshot("default", prior["id"], {"views": 900000}, audience._iso(self.now - timedelta(days=1)))
        current = self.publish(self.variant(self.experiment()))
        self.observe(current, 4000)
        reward = audience.experiment_detail("default", current["experiment_id"])["variants"][0]["reward"]
        self.assertEqual(reward["baseline_count"], 0)
        self.assertEqual(reward["baseline_kind"], "experiment_peers")

    def test_concurrent_identical_imports_are_idempotent(self):
        variant = self.publish(self.variant(self.experiment()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.observe(variant, 1000), range(2)))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(len(audience.variant_detail("default", variant["id"])["snapshots"]), 1)

    def test_late_paid_boost_excludes_existing_pair_and_cannot_be_undone(self):
        exp = self.experiment()
        a, b = self.publish(self.variant(exp, "A")), self.publish(self.variant(exp, "B"))
        self.observe(a, 1000)
        self.observe(b, 4000)
        self.assertEqual(len(audience.export_preferences("default")["records"]), 1)
        excluded = self.publish(b, exposure="mixed")
        self.assertEqual(audience.export_preferences("default")["records"], [])
        self.assertEqual(excluded["audit"][0]["kind"], "exposure_exclusion")
        with self.assertRaises(ValueError):
            self.publish(b, exposure="organic")

    def test_cannot_overwrite_known_published_draft_identity(self):
        with ws.connection() as db:
            db.executescript("CREATE TABLE social_posts (id INTEGER PRIMARY KEY,project TEXT,platform TEXT,job_id TEXT,media TEXT,result TEXT,status TEXT,updated_at TEXT);")
        with ws.connection() as db, db:
            db.execute("INSERT INTO social_posts VALUES (1,'default','tiktok','original-job','[]',?,'published',?)",
                       (json.dumps({"id": "123", "url": "https://www.tiktok.com/@owner/video/123"}), store.now()))
        exp = self.experiment()
        variant = self.variant(exp, post_id=1)
        with self.assertRaises(ValueError):
            self.publish(variant, "456")
        self.assertEqual(json.loads(ws.query("SELECT result FROM social_posts WHERE id=1")[0]["result"])["id"], "123")
        self.assertEqual(audience.experiment_detail("default", exp["id"])["checkpoints"], [])


if __name__ == "__main__":
    unittest.main()
