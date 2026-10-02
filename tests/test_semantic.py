"""Semantic (vector) hybrid search. Temporary database, no network."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import semantic
import store
import workspace as ws

LIKE_SQL = "SELECT * FROM notes WHERE project=? AND (title LIKE ? OR content LIKE ?) ORDER BY updated_at DESC, id DESC LIMIT ?"


def fake_embed(texts):
    """Tiny 4-dim 'semantic' space: weather words and umbrella words point the same way."""
    out = []
    for t in texts:
        t = t.lower()
        if "umbrella" in t or "rain" in t:
            out.append([1.0, 0.0, 0.0, 0.0])
        elif "weather" in t:
            out.append([0.9, 0.1, 0.0, 0.0])
        else:
            out.append([0.0, 1.0, 0.0, 0.0])
    return out


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = store.DATA
        store.DATA = Path(self.temp.name)
        ws.init()

    def tearDown(self):
        store.DATA = self.old_data
        self.temp.cleanup()

    def test_local_embedder_is_deterministic_fixed_dim_and_safe(self):
        a, b = semantic.local_embed(["hello world", ""]), semantic.local_embed(["hello world", ""])
        self.assertEqual(a, b)
        for vec in a:
            self.assertEqual(len(vec), semantic.LOCAL_DIM)
            self.assertTrue(any(vec))
        self.assertGreater(semantic.cosine(*semantic.local_embed(["red apple pie", "apple pie recipe"])),
                           semantic.cosine(*semantic.local_embed(["red apple pie", "quantum chromodynamics"])))

    def test_embed_never_raises_and_falls_back_offline(self):
        with patch.object(semantic, "_remote_embed", side_effect=RuntimeError("down")):
            self.assertIsNone(semantic.embed([]))
        with patch.dict("os.environ", {"HUB_URL": "", "HUB_KEY": ""}):
            vecs = semantic.embed(["x", "y"])
        self.assertEqual([len(v) for v in vecs], [semantic.LOCAL_DIM] * 2)

    def test_cosine_known_vectors(self):
        self.assertAlmostEqual(semantic.cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(semantic.cosine([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(semantic.cosine([1, 0], [-1, 0]), -1.0)
        self.assertEqual(semantic.cosine([1, 0], [1, 0, 0]), -1.0)
        self.assertEqual(semantic.cosine([0, 0], [1, 0]), -1.0)

    def test_semantic_match_outranks_exact_keyword_hit(self):
        with patch.object(semantic, "embed", fake_embed):
            ws.save_note("default", "Gear", "I always carry an umbrella", "fact")
            ws.save_note("default", "Misc", "the weather word appears here only as filler text", "fact")
            ws.save_note("default", "Other", "completely unrelated groceries", "fact")
            found = ws.memories("default", "weather")
        self.assertEqual(found[0]["title"], "Misc")  # keyword hit AND close vector wins
        self.assertEqual(found[1]["title"], "Gear")  # no shared words, still above the unrelated note
        with patch.object(semantic, "embed", fake_embed):
            found = ws.memories("default", "rain")
        self.assertEqual(found[0]["title"], "Gear")  # zero keyword overlap with "umbrella" text, ranked by vector alone
        self.assertEqual({n["title"] for n in found}, {"Gear", "Misc", "Other"})

    def test_like_fallback_when_embed_returns_none(self):
        with patch.object(semantic, "embed", fake_embed):
            ws.save_note("default", "Gear", "umbrella", "fact")
            ws.save_note("default", "Misc", "weather filler", "fact")
        with patch.object(semantic, "embed", lambda texts: None):
            got = ws.memories("default", "weather")
            self.assertEqual(got, ws.query(LIKE_SQL, ("default", "%weather%", "%weather%", 10)))
            self.assertEqual([n["title"] for n in got], ["Misc"])
            self.assertEqual(ws.memories("default", ""), ws.query(LIKE_SQL, ("default", "%%", "%%", 10)))

    def test_search_fts_empty_falls_back_to_vectors(self):
        with patch.object(semantic, "embed", fake_embed):
            ws.ingest("default", "Doc", "Bring an umbrella when it is wet outside.", "test")
            ws.ingest("default", "Doc2", "Quarterly accounting totals.", "test2")
            rows = ws.search("default", "rain")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Doc")
        self.assertEqual(set(rows[0]), {"document_id", "title", "source", "chunk_no", "content", "score"})

    def test_search_fts_hits_are_reranked_never_dropped(self):
        with patch.object(semantic, "embed", fake_embed):
            ws.ingest("default", "One", "rain forecast", "s1")
            ws.ingest("default", "Two", "rain and umbrella", "s2")
            rows = ws.search("default", "rain umbrella")
        self.assertEqual({r["title"] for r in rows}, {"One", "Two"})
        self.assertTrue(all("score" in r for r in rows))
        with patch.object(semantic, "embed", lambda texts: None):
            plain = ws.search("default", "rain umbrella")
        self.assertEqual({r["title"] for r in plain}, {"One", "Two"})
        self.assertTrue(all("score" not in r for r in plain))

    def test_dimension_mismatch_skips_old_rows(self):
        note = ws.save_note("default", "Old", "four dims", "fact")
        ws._store_vectors([(f"note:{note}", note, [1.0, 0.0, 0.0, 0.0])])
        self.assertEqual(len(ws.vectors("default")[f"note:{note}"]), 4)
        newer = ws.save_note("default", "New", "eight dims", "fact")
        ws._store_vectors([(f"note:{newer}", newer, [1.0] + [0.0] * 7)])
        loaded = ws.vectors("default")
        self.assertEqual(list(loaded), [f"note:{newer}"])
        self.assertEqual(list(ws.vectors("default", dim=4)), [f"note:{note}"])
        with patch.object(semantic, "embed", lambda texts: [[1.0] + [0.0] * 7 for _ in texts]):
            self.assertEqual(len(ws.memories("default", "dims")), 2)  # mismatched row scored -1, no crash

    def test_save_note_and_ingest_survive_embed_failure(self):
        with patch.object(semantic, "embed", side_effect=RuntimeError("boom")):
            note = ws.save_note("default", "T", "content", "fact")
            doc = ws.ingest("default", "D", "some text", "src")
            self.assertEqual(ws.memories("default", "content")[0]["id"], note)
            self.assertEqual(len(ws.search("default", "text")), 1)
        self.assertFalse(doc["duplicate"])
        self.assertEqual(ws.vectors("default"), {})

    def test_init_is_idempotent(self):
        ws.init()
        ws.init()
        self.assertEqual(ws.query("SELECT COUNT(*) AS n FROM embeddings")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
