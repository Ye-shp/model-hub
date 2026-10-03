"""Project-scoped content experiments, immutable outcomes and durable analytics checkpoints.

No publishing or model execution occurs here. Reward v1 measures reach; other
metrics remain diagnostics. Exported preferences are observations, not causal proof.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import store
import workspace as ws

HORIZONS = (24, 72, 168)
SCORE_VERSION = "audience-v1"
COUNT_METRICS = {"views", "likes", "comments", "shares", "saves", "reach", "followers", "conversions"}
METRICS = COUNT_METRICS | {"average_watch_seconds", "completion_rate"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_experiments (
 id INTEGER PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL, brief TEXT NOT NULL,
 hypothesis TEXT NOT NULL, platform TEXT NOT NULL, account TEXT NOT NULL,
 kind TEXT NOT NULL, context TEXT NOT NULL, split TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audience_project ON audience_experiments(project,id);
CREATE TABLE IF NOT EXISTS audience_variants (
 id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL REFERENCES audience_experiments(id),
 project TEXT NOT NULL, label TEXT NOT NULL, response TEXT NOT NULL, model TEXT NOT NULL,
 strategy TEXT NOT NULL, media_hashes TEXT NOT NULL, content_hash TEXT NOT NULL,
 post_id INTEGER, job_id TEXT NOT NULL, created_at TEXT NOT NULL,
 remote_id TEXT, url TEXT, published_at TEXT, account TEXT, exposure TEXT,
 UNIQUE(experiment_id,label)
);
CREATE UNIQUE INDEX IF NOT EXISTS audience_draft ON audience_variants(project,post_id) WHERE post_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS audience_snapshots (
 id INTEGER PRIMARY KEY, variant_id INTEGER NOT NULL REFERENCES audience_variants(id),
 project TEXT NOT NULL, metrics TEXT NOT NULL, source TEXT NOT NULL, warnings TEXT NOT NULL,
 observed_at TEXT NOT NULL, age_hours REAL NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(variant_id,observed_at,source)
);
CREATE TABLE IF NOT EXISTS audience_checkpoints (
 id INTEGER PRIMARY KEY, variant_id INTEGER NOT NULL REFERENCES audience_variants(id),
 horizon_hours INTEGER NOT NULL, due_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until TEXT, last_error TEXT, snapshot_id INTEGER REFERENCES audience_snapshots(id),
 UNIQUE(variant_id,horizon_hours)
);
CREATE INDEX IF NOT EXISTS audience_due ON audience_checkpoints(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS audience_audit (
 id INTEGER PRIMARY KEY, variant_id INTEGER NOT NULL REFERENCES audience_variants(id),
 project TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def init():
    with ws.connection() as db:
        db.executescript(SCHEMA)


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _time(value: str | datetime | None = None) -> datetime:
    if value is None:
        return _clock()
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Use a timestamp with a timezone, such as 2026-10-03T12:00:00Z") from None


def _text(value: str, label: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} must be text of at most {maximum} characters")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} is required")
    return value


def _project(project):
    if not ws.project_exists(project):
        raise ValueError("Unknown project")


def create_experiment(project, name, brief, hypothesis="", platform="tiktok", account="",
                      kind="reel", context="", split="train") -> dict:
    _project(project)
    if platform not in {"tiktok", "instagram"} or split not in {"train", "eval"}:
        raise ValueError("Use platform tiktok/instagram and split train/eval")
    values = (project, _text(name, "Name", 160, True), _text(brief, "Brief", 12000, True),
              _text(hypothesis, "Hypothesis", 2000), platform, _text(account, "Account ID", 200),
              _text(kind, "Format", 80, True), _text(context, "Context", 1000), split, store.now())
    with ws.connection() as db, db:
        ident = db.execute("INSERT INTO audience_experiments(project,name,brief,hypothesis,platform,account,kind,context,split,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)", values).lastrowid
    return _experiment(project, ident)


def _experiment(project, experiment_id) -> dict:
    rows = ws.query("SELECT * FROM audience_experiments WHERE project=? AND id=?", (project, experiment_id))
    if not rows:
        raise ValueError("Experiment not found in this project")
    return rows[0]


def list_experiments(project) -> list[dict]:
    _project(project)
    return ws.query("SELECT e.*, (SELECT count(*) FROM audience_variants v WHERE v.experiment_id=e.id) AS variant_count "
                    "FROM audience_experiments e WHERE project=? ORDER BY id DESC LIMIT 200", (project,))


def register_variant(project, experiment_id, label, response, post_id=None, model="", strategy="",
                     media_hashes=None, job_id="") -> dict:
    experiment = _experiment(project, experiment_id)
    label, response = _text(label, "Variant name", 160, True), _text(response, "Generated response", 50000, True)
    if post_id is not None:
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id < 1:
            raise ValueError("Draft ID must be a positive integer")
        # This table exists after the posting tools are initialized. Older databases may not have it.
        with ws.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_posts'").fetchone():
                raise ValueError("No posting draft exists yet")
            post = db.execute("SELECT * FROM social_posts WHERE id=? AND project=?", (post_id, project)).fetchone()
        if not post or post["platform"] != experiment["platform"]:
            raise ValueError("Draft not found in this project/platform")
        # Follow-up chats may register an older draft. Preserve its actual generation job.
        job_id = post["job_id"] or ""
    if not media_hashes and post_id is not None:
        media_hashes = []
        for filename in json.loads(post["media"] or "[]"):
            path = Path(filename).resolve()
            folder = (store.DATA / "posts" / str(post_id)).resolve()
            if path.is_relative_to(folder) and path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                media_hashes.append(digest.hexdigest())
    if media_hashes is None:
        media_hashes = []
    if not isinstance(media_hashes, list) or len(media_hashes) > 10 or any(
            not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in media_hashes):
        raise ValueError("Supply up to ten SHA-256 media hashes")
    content_hash = hashlib.sha256(json.dumps([response, media_hashes], ensure_ascii=False).encode()).hexdigest()
    with ws.connection() as db, db:
        try:
            ident = db.execute("INSERT INTO audience_variants(experiment_id,project,label,response,model,strategy,media_hashes,"
                               "content_hash,post_id,job_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (experiment_id, project, label, response, _text(model, "Declared model", 300),
                                _text(strategy, "Strategy", 2000), json.dumps(media_hashes), content_hash, post_id,
                                _text(job_id, "Job ID", 120), store.now())).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("This variant name or draft is already registered") from None
    return variant_detail(project, ident)


def _variant(db, project, variant_id):
    row = db.execute("SELECT v.*,e.platform,e.kind,e.context,e.split FROM audience_variants v "
                     "JOIN audience_experiments e ON e.id=v.experiment_id WHERE v.project=? AND v.id=?",
                     (project, variant_id)).fetchone()
    if not row:
        raise ValueError("Variant not found in this project")
    result = dict(row)
    result["media_hashes"] = json.loads(result["media_hashes"])
    return result


def variant_detail(project, variant_id) -> dict:
    with ws.connection() as db:
        result = _variant(db, project, variant_id)
        result["snapshots"] = [_snapshot(r) for r in db.execute(
            "SELECT * FROM audience_snapshots WHERE project=? AND variant_id=? ORDER BY observed_at,id", (project, variant_id))]
        result["audit"] = [dict(r) for r in db.execute("SELECT * FROM audience_audit WHERE project=? AND variant_id=? ORDER BY id", (project, variant_id))]
    return result


def variant_for_post(project, post_id) -> dict | None:
    rows = ws.query("SELECT id FROM audience_variants WHERE project=? AND post_id=?", (project, post_id))
    return variant_detail(project, rows[0]["id"]) if rows else None


def confirm_publication(project, variant_id, remote_id, url, published_at, account, exposure="organic") -> dict:
    remote_id = _text(remote_id, "Published post ID", 40, True)
    account = _text(account, "Account ID", 200, True)
    if not re.fullmatch(r"[0-9]{1,40}", remote_id) or exposure not in {"organic", "paid", "mixed", "unknown"}:
        raise ValueError("Use a numeric post ID and exposure organic/paid/mixed/unknown")
    published = _time(published_at)
    if published > _clock() + timedelta(minutes=5) or published.year < 2000:
        raise ValueError("Publication time must describe an existing post")
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        variant = _variant(db, project, variant_id)
        experiment = dict(db.execute("SELECT * FROM audience_experiments WHERE id=?", (variant["experiment_id"],)).fetchone())
        if experiment["account"] and account != experiment["account"]:
            raise ValueError("Publication account differs from the experiment account")
        parsed = urlsplit(_text(url, "Post URL", 2000, True))
        hosts = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"} if variant["platform"] == "tiktok" else {"instagram.com", "www.instagram.com"}
        if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.port:
            raise ValueError("Use a direct HTTPS post URL on the experiment's platform")
        if variant["platform"] == "tiktok":
            if not re.fullmatch(r"/@[^/]+/video/" + re.escape(remote_id) + r"/?", parsed.path):
                raise ValueError("TikTok URL must contain this published video ID")
        elif not re.fullmatch(r"/(?:reel|p|tv)/[A-Za-z0-9_-]+/?", parsed.path):
            raise ValueError("Use an Instagram post or reel permalink")
        clean_url = urlunsplit(("https", parsed.netloc, parsed.path, "", ""))
        identity = (remote_id, clean_url, _iso(published), account, exposure)
        if variant["published_at"]:
            if tuple(variant[k] for k in ("remote_id", "url", "published_at", "account")) != identity[:4]:
                raise ValueError("Publication is already recorded; it cannot be reassigned")
            if variant["exposure"] != exposure:
                if exposure == "organic":
                    raise ValueError("An excluded post cannot be reclassified as organic")
                db.execute("UPDATE audience_variants SET exposure=? WHERE id=?", (exposure, variant_id))
                db.execute("INSERT INTO audience_audit(variant_id,project,kind,detail,created_at) VALUES (?,?,?,?,?)",
                           (variant_id, project, "exposure_exclusion", json.dumps({"before": variant["exposure"], "after": exposure}), store.now()))
        else:
            if variant["post_id"] is not None:
                saved_post = db.execute("SELECT result,status FROM social_posts WHERE id=? AND project=?", (variant["post_id"], project)).fetchone()
                try:
                    known = json.loads(saved_post["result"] or "{}")
                except (ValueError, TypeError):
                    known = {}
                if isinstance(known, dict) and saved_post["status"] == "published":
                    if known.get("id") and str(known["id"]) != remote_id:
                        raise ValueError("This draft already has a different published post ID")
                    if known.get("url"):
                        known_url = urlsplit(known["url"])
                        if known_url.path.rstrip("/") != parsed.path.rstrip("/"):
                            raise ValueError("This draft already has a different published post link")
            duplicate = db.execute("SELECT v.id FROM audience_variants v JOIN audience_experiments e ON e.id=v.experiment_id "
                                   "WHERE v.project=? AND e.platform=? AND v.remote_id=?", (project, variant["platform"], remote_id)).fetchone()
            if duplicate:
                raise ValueError("This published post is already assigned to a variant")
            db.execute("UPDATE audience_experiments SET account=? WHERE id=? AND account=''", (account, experiment["id"]))
            db.execute("UPDATE audience_variants SET remote_id=?,url=?,published_at=?,account=?,exposure=? WHERE id=?", (*identity, variant_id))
            for hours in HORIZONS:
                due = _iso(published + timedelta(hours=hours))
                db.execute("INSERT INTO audience_checkpoints(variant_id,horizon_hours,due_at,next_attempt_at) VALUES (?,?,?,?)",
                           (variant_id, hours, due, due))
            if variant["post_id"] is not None:
                row = db.execute("SELECT result FROM social_posts WHERE id=? AND project=?", (variant["post_id"], project)).fetchone()
                try:
                    result = json.loads(row["result"] or "{}")
                except (ValueError, TypeError):
                    result = {}
                if not isinstance(result, dict):
                    result = {}
                result.update(id=remote_id, url=clean_url, published_at=_iso(published), confirmation="owner_recorded")
                db.execute("UPDATE social_posts SET status='published',result=?,updated_at=? WHERE id=? AND project=?",
                           (json.dumps(result), store.now(), variant["post_id"], project))
    return variant_detail(project, variant_id)


def _clean_metrics(metrics):
    if not isinstance(metrics, dict) or set(metrics) - METRICS:
        raise ValueError("Supply only supported audience metric names")
    clean = {key: None for key in METRICS}
    for key, value in metrics.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > 10**15:
            raise ValueError(f"{key} must be a finite nonnegative number or null")
        if key in COUNT_METRICS and int(value) != value:
            raise ValueError(f"{key} must be a whole count")
        if key == "completion_rate" and value > 1:
            raise ValueError("completion_rate is a fraction between 0 and 1")
        clean[key] = int(value) if key in COUNT_METRICS else float(value)
    if all(v is None for v in clean.values()):
        raise ValueError("At least one metric must be available")
    return clean


def _snapshot(row):
    result = dict(row)
    result["metrics"], result["warnings"] = json.loads(result["metrics"]), json.loads(result["warnings"])
    return result


def _record(db, project, variant_id, metrics, observed_at, source, warnings):
    variant = _variant(db, project, variant_id)
    if not variant["published_at"]:
        raise ValueError("Record the confirmed publication before its audience metrics")
    observed, published = _time(observed_at), _time(variant["published_at"])
    if observed < published or observed > _clock() + timedelta(minutes=5):
        raise ValueError("Observation must be after publication and cannot be in the future")
    clean = _clean_metrics(metrics)
    source = _text(source, "Metric source", 80, True)
    warnings = warnings or []
    if not isinstance(warnings, list) or len(warnings) > 30:
        raise ValueError("Supply up to thirty collection warnings")
    warnings = [_text(w, "Collection warning", 300) for w in warnings]
    encoded = json.dumps(clean, sort_keys=True)
    existing = db.execute("SELECT * FROM audience_snapshots WHERE variant_id=? AND observed_at=? AND source=?",
                          (variant_id, _iso(observed), source)).fetchone()
    if existing:
        if existing["metrics"] != encoded or json.loads(existing["warnings"]) != warnings:
            raise ValueError("A snapshot is immutable; use the actual time of the new observation")
        return _snapshot(existing)
    ident = db.execute("INSERT INTO audience_snapshots(variant_id,project,metrics,source,warnings,observed_at,age_hours,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (variant_id, project, encoded, source, json.dumps(warnings), _iso(observed),
                                                  (observed - published).total_seconds() / 3600, store.now())).lastrowid
    return _snapshot(db.execute("SELECT * FROM audience_snapshots WHERE id=?", (ident,)).fetchone())


def record_snapshot(project, variant_id, metrics, observed_at=None, source="manual", warnings=None) -> dict:
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        snapshot = _record(db, project, variant_id, metrics, observed_at, source, warnings)
        # A timestamped manual import can satisfy its matching checkpoint, even after
        # a restart marked that window missed. It never steals an active worker's lease.
        if source == "manual":
            for hours in HORIZONS:
                if abs(snapshot["age_hours"] - hours) <= max(2, hours * .2):
                    db.execute("UPDATE audience_checkpoints SET status='done',snapshot_id=?,last_error=NULL "
                               "WHERE variant_id=? AND horizon_hours=? AND status!='leased'",
                               (snapshot["id"], variant_id, hours))
        return snapshot


def claim_due(now=None, lease_seconds=180) -> dict | None:
    if not 10 <= lease_seconds <= 3600:
        raise ValueError("Collection lease must be between 10 and 3600 seconds")
    moment = _time(now)
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        # Missed checkpoints are visible. Today's lifetime counts are never relabeled as yesterday's counts.
        for row in db.execute("SELECT id,due_at,horizon_hours FROM audience_checkpoints WHERE status IN ('queued','retry','leased')"):
            if moment > _time(row["due_at"]) + timedelta(hours=max(2, row["horizon_hours"] * .2)):
                db.execute("UPDATE audience_checkpoints SET status='missed',lease_token=NULL,lease_until=NULL,last_error=? WHERE id=?",
                           ("Observation window missed; a later count cannot replace this checkpoint", row["id"]))
        row = db.execute("SELECT c.*,v.project,v.remote_id,v.url,v.account,v.published_at,e.platform "
                         "FROM audience_checkpoints c JOIN audience_variants v ON v.id=c.variant_id "
                         "JOIN audience_experiments e ON e.id=v.experiment_id WHERE c.due_at<=? AND c.next_attempt_at<=? "
                         "AND (c.status IN ('queued','retry') OR (c.status='leased' AND c.lease_until<=?)) "
                         "ORDER BY c.due_at,c.id LIMIT 1", (_iso(moment), _iso(moment), _iso(moment))).fetchone()
        if not row:
            return None
        token = uuid.uuid4().hex
        db.execute("UPDATE audience_checkpoints SET status='leased',attempts=attempts+1,lease_token=?,lease_until=? WHERE id=?",
                   (token, _iso(moment + timedelta(seconds=lease_seconds)), row["id"]))
        return {**dict(row), "lease_token": token, "attempts": row["attempts"] + 1, "status": "leased"}


def _lease(db, task_id, lease_token):
    row = db.execute("SELECT c.*,v.project FROM audience_checkpoints c JOIN audience_variants v ON v.id=c.variant_id "
                     "WHERE c.id=? AND c.status='leased' AND c.lease_token=?", (task_id, lease_token)).fetchone()
    if not row or _time(row["lease_until"]) <= _clock():
        raise ValueError("Collection lease expired or was replaced")
    return row


def finish_collection(task_id, lease_token, metrics, source, observed_at=None, warnings=None):
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        task = _lease(db, task_id, lease_token)
        snapshot = _record(db, task["project"], task["variant_id"], metrics, observed_at, source, warnings)
        db.execute("UPDATE audience_checkpoints SET status='done',snapshot_id=?,lease_token=NULL,lease_until=NULL,last_error=NULL WHERE id=?",
                   (snapshot["id"], task_id))
    return snapshot


def fail_collection(task_id, lease_token, error, retry_after_seconds=300):
    if not 1 <= retry_after_seconds <= 86400:
        raise ValueError("Retry delay is outside its limit")
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        task = _lease(db, task_id, lease_token)
        db.execute("UPDATE audience_checkpoints SET status=?,next_attempt_at=?,last_error=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                   ("failed" if task["attempts"] >= 8 else "retry", _iso(_clock() + timedelta(seconds=retry_after_seconds)),
                    _text(error, "Collection error", 800), task_id))


def _selected(variant, horizon):
    candidates = [s for s in variant["snapshots"] if abs(s["age_hours"] - horizon) <= max(2, horizon * .2)
                  and s["metrics"]["views"] is not None]
    return min(candidates, key=lambda s: (abs(s["age_hours"] - horizon), -s["id"])) if candidates else None


def _results(project, horizon=72):
    variants = [variant_detail(project, r["id"]) for r in ws.query("SELECT id FROM audience_variants WHERE project=?", (project,))]
    for variant in variants:
        selected = _selected(variant, horizon)
        variant["selected_snapshot"] = selected
    for variant in variants:
        selected = variant["selected_snapshot"]
        if not selected:
            variant["reward"] = {"score": None, "eligible": False, "reason": "No views measured near this checkpoint", "horizon_hours": horizon}
            continue
        metrics, views = selected["metrics"], selected["metrics"]["views"]
        peers = [v for v in variants if v["experiment_id"] == variant["experiment_id"] and v["selected_snapshot"]
                 and v["exposure"] == "organic" and v["account"] == variant["account"]]
        earliest = min((v["published_at"] for v in peers), default=variant["published_at"])
        history = [v["selected_snapshot"]["metrics"]["views"] for v in variants if v["selected_snapshot"] and
                   v["platform"] == variant["platform"] and v["account"] == variant["account"] and
                   v["kind"] == variant["kind"] and v["context"] == variant["context"] and v["split"] == "train" and
                   v["exposure"] == "organic" and v["experiment_id"] != variant["experiment_id"] and v["published_at"] < earliest
                   and v["selected_snapshot"]["observed_at"] <= earliest]
        baseline = statistics.median(history) if history else statistics.median([v["selected_snapshot"]["metrics"]["views"] for v in peers]) if peers else views
        share_rate = metrics["shares"] / views if views and metrics["shares"] is not None else None
        variant["reward"] = {"score": math.log((views + 1) / (baseline + 1)), "reach_lift": (views + 1) / (baseline + 1),
                             "baseline_views": baseline, "baseline_count": len(history),
                             "baseline_kind": "historical" if history else "experiment_peers", "views": views,
                             "share_rate": share_rate, "confidence": views / (views + 500), "horizon_hours": horizon,
                             "eligible": variant["split"] == "train" and variant["exposure"] == "organic" and views >= 500,
                             "reason": "Evaluation only; excluded from training" if variant["split"] != "train" else
                                       "Non-organic exposure excluded" if variant["exposure"] != "organic" else "Small sample" if views < 500 else "",
                             "score_version": SCORE_VERSION}
    return variants


def experiment_detail(project, experiment_id) -> dict:
    experiment = _experiment(project, experiment_id)
    variants = [v for v in _results(project) if v["experiment_id"] == experiment_id]
    checkpoints = ws.query("SELECT c.* FROM audience_checkpoints c JOIN audience_variants v ON v.id=c.variant_id "
                           "WHERE v.project=? AND v.experiment_id=? ORDER BY c.due_at", (project, experiment_id))
    # Lease fencing tokens stay internal to the collector.
    for checkpoint in checkpoints:
        checkpoint.pop("lease_token", None)
    return {"experiment": experiment, "variants": variants, "checkpoints": checkpoints}


def performance_summary(project) -> dict:
    _project(project)
    results = _results(project)
    return {"results": results, "horizon_hours": 72, "score_version": SCORE_VERSION,
            "summary": {"variants": len(results), "published": sum(bool(v["published_at"]) for v in results),
                        "measured": sum(bool(v["selected_snapshot"]) for v in results)}}


def export_preferences(project, horizon_hours=72, min_views=500, min_margin=.15) -> dict:
    _project(project)
    if horizon_hours not in HORIZONS or isinstance(min_views, bool) or not isinstance(min_views, int) or min_views < 500 or min_views > 10**9:
        raise ValueError("Use a 24/72/168 hour horizon and at least 500 views")
    if isinstance(min_margin, bool) or not isinstance(min_margin, (float, int)) or not math.isfinite(min_margin) or not .01 <= min_margin <= 10:
        raise ValueError("Preference margin must be between .01 and 10 log reach units")
    results = _results(project, horizon_hours)
    records, comparisons, skipped = [], [], []
    for experiment in list_experiments(project):
        ident = experiment["id"]
        if experiment["split"] != "train":
            skipped.append({"experiment_id": ident, "reason": "Held out for evaluation"})
            continue
        candidates = [v for v in results if v["experiment_id"] == ident and v["selected_snapshot"] and
                      v["exposure"] == "organic" and v["selected_snapshot"]["metrics"]["views"] >= min_views]
        if len(candidates) < 2:
            skipped.append({"experiment_id": ident, "reason": "Need two organic variants measured at the same horizon with enough views"})
            continue
        candidates.sort(key=lambda v: v["selected_snapshot"]["metrics"]["views"])
        rejected, chosen = candidates[0], candidates[-1]
        margin = math.log((chosen["reward"]["views"] + 1) / (rejected["reward"]["views"] + 1))
        if chosen["response"] == rejected["response"] or margin < min_margin:
            skipped.append({"experiment_id": ident, "reason": "Responses are identical or the reach difference is too small"})
            continue
        # Same nominal checkpoint is insufficient if one was measured much later than the other.
        if abs(chosen["selected_snapshot"]["age_hours"] - rejected["selected_snapshot"]["age_hours"]) > max(2, horizon_hours * .1):
            skipped.append({"experiment_id": ident, "reason": "Observation ages are too different"})
            continue
        records.append({"prompt": experiment["brief"], "chosen": chosen["response"], "rejected": rejected["response"]})
        def evidence(variant):
            return {key: variant[key] for key in ("id", "label", "content_hash", "media_hashes", "model", "strategy",
                                                  "job_id", "post_id", "remote_id", "url", "published_at", "account", "exposure")} | {
                "observation": variant["selected_snapshot"], "reward": variant["reward"], "exclusion_audit": variant["audit"]}
        comparisons.append({"experiment_id": ident, "chosen_id": chosen["id"], "rejected_id": rejected["id"],
                            "chosen_snapshot_id": chosen["selected_snapshot"]["id"], "rejected_snapshot_id": rejected["selected_snapshot"]["id"],
                            "margin": margin, "confidence": min(chosen["reward"]["confidence"], rejected["reward"]["confidence"]),
                            "experiment": experiment, "chosen": evidence(chosen), "rejected": evidence(rejected),
                            "score_version": SCORE_VERSION})
    return {"records": records, "comparisons": comparisons, "skipped": skipped, "horizon_hours": horizon_hours,
            "min_views": min_views, "min_margin": min_margin, "score_version": SCORE_VERSION,
            "dataset_digest": hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
            "exported_at": store.now(),
            "note": "Observed reach preferences; account, timing, editing and distribution can still confound results. Review before training."}
