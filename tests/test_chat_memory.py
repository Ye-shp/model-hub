"""Conversation memory isolation, including nondestructive upgrades of old databases."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from agents.tool_context import ToolContext as Invocation
from cowork.tools import _recall, _remember, _search_knowledge, _search_posts
import semantic
import store
import workspace as ws


def invoke(tool, arguments):
    encoded = json.dumps(arguments)
    context = Invocation(context=None, tool_name=tool.name, tool_call_id="memory-test", tool_arguments=encoded)
    return asyncio.run(tool.on_invoke_tool(context, encoded))


def tool_context(thread, job_id="memory-job"):
    return SimpleNamespace(project="default", job_id=job_id, space=SimpleNamespace(thread=thread),
                           budget=SimpleNamespace(active=lambda: None), log=lambda kind, detail: None)


class ChatMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch_data = patch.object(store, "DATA", Path(self.temp.name))
        self.patch_data.start()
        ws.init()

    def tearDown(self):
        self.patch_data.stop()
        self.temp.cleanup()

    def test_same_title_is_independent_across_chats_and_global_notes(self):
        global_note = ws.save_note("default", "Budget", "global value")
        a = ws.save_note("default", "Budget", "100", thread="chat-a")
        b = ws.save_note("default", "Budget", "900", thread="chat-b")
        self.assertEqual(ws.save_note("default", "Budget", "150", thread="chat-a"), a)
        self.assertEqual(ws.save_note("default", "Budget", "global updated"), global_note)
        self.assertEqual(len({global_note, a, b}), 3)
        self.assertEqual([(n["id"], n["content"]) for n in ws.memories("default", thread="chat-a")], [(a, "150")])
        self.assertEqual([(n["id"], n["content"]) for n in ws.memories("default", thread="chat-b")], [(b, "900")])
        # Console/team callers still see the full project without a thread filter.
        self.assertEqual({n["id"] for n in ws.memories("default")}, {global_note, a, b})

    def test_empty_keyword_and_semantic_recall_never_return_another_chat(self):
        with patch.object(semantic, "embed", lambda texts: [[1.0, 0.0] for _ in texts]):
            a = ws.save_note("default", "A", "needle alpha", thread="chat-a")
            ws.save_note("default", "B", "needle beta", thread="chat-b")
            ws.save_note("default", "Global", "needle global")
            for search in ("", "needle", "beta", "meaning without a keyword"):
                self.assertEqual({n["id"] for n in ws.memories("default", search, thread="chat-a")}, {a})
        with patch.object(semantic, "embed", return_value=None):
            self.assertEqual({n["id"] for n in ws.memories("default", "needle", thread="chat-a")}, {a})
            self.assertEqual(ws.memories("default", "beta", thread="chat-a"), [])

    def test_project_isolation_still_applies_with_the_same_thread_name(self):
        other = ws.create_project("Other")
        ws.save_note("default", "Decision", "mine", thread="chat-a")
        ws.save_note(other, "Decision", "theirs", thread="chat-a")
        self.assertEqual([n["content"] for n in ws.memories("default", thread="chat-a")], ["mine"])

    def test_real_tools_keep_followups_and_continuations_in_the_same_chat(self):
        first = ws.create_job("default", "first turn", "chat", "fast", thread="chat-a")
        next_turn = ws.create_job("default", "continue", "chat", "fast", thread="chat-a", parent=first)
        other = ws.create_job("default", "different request", "cowork", "fast", thread="chat-b")
        invoke(_remember(tool_context("chat-a", first)), {"title": "Tone", "content": "brief", "kind": "preference"})
        invoke(_remember(tool_context("chat-b", other)), {"title": "Tone", "content": "detailed", "kind": "preference"})
        a = json.loads(invoke(_recall(tool_context("chat-a", next_turn)), {"query": ""}))["items"]
        b = json.loads(invoke(_recall(tool_context("chat-b", other)), {"query": ""}))["items"]
        self.assertEqual([n["content"] for n in a], ["brief"])
        self.assertEqual([n["content"] for n in b], ["detailed"])
        self.assertEqual(a[0]["sources"], json.dumps([f"job:{first}"]))
        self.assertEqual(a[0]["thread"], "chat-a")

    def test_shared_knowledge_and_posts_remain_available_in_both_chats(self):
        ws.ingest("default", "Growth playbook", "Shared evergreen hook strategy", "user supplied")
        with ws.connection() as db:
            store.save_post(db, "tiktok", {"source_id": "shared-post", "caption": "Shared strategy"}, "", "default")
        for thread in ("chat-a", "chat-b"):
            ctx = tool_context(thread)
            docs = json.loads(invoke(_search_knowledge(ctx), {"query": "strategy", "limit": 5}))
            posts = json.loads(invoke(_search_posts(ctx), {"query": "strategy", "limit": 15}))
            self.assertIn("Growth playbook", json.dumps(docs))
            self.assertIn("Shared strategy", json.dumps(posts))
            self.assertEqual(json.loads(invoke(_recall(ctx), {"query": ""}))["items"], [])

    def test_invalid_memory_scope_is_rejected_without_becoming_global(self):
        for thread in ("", "../chat-b", "x" * 81):
            with self.assertRaisesRegex(ValueError, "Invalid memory thread"):
                ws.save_note("default", "Invalid", "value", thread=thread)
            with self.assertRaisesRegex(ValueError, "Invalid memory thread"):
                ws.memories("default", thread=thread)
        self.assertEqual(ws.memories("default"), [])


class ChatMemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch_data = patch.object(store, "DATA", Path(self.temp.name))
        self.patch_data.start()
        db = sqlite3.connect(store.DATA / "hub.db")
        db.row_factory = sqlite3.Row
        legacy = ws.SCHEMA.replace("thread TEXT NOT NULL DEFAULT '', UNIQUE(project, thread, title)", "UNIQUE(project, title)")
        db.executescript(legacy)
        db.execute("ALTER TABLE jobs ADD COLUMN thread TEXT")
        db.execute("INSERT INTO projects VALUES ('default','Workspace','','2026-10-01')")
        db.execute("INSERT INTO projects VALUES ('other','Other','','2026-10-01')")
        for job_id, project, thread in (("a", "default", "chat-a"), ("a2", "default", "chat-a"),
                                        ("b", "default", "chat-b"), ("wrong", "other", "chat-a"),
                                        ("console", "default", None)):
            db.execute("INSERT INTO jobs(id,project,task,skill,profile,status,created_at,thread) VALUES (?,?,?,'cowork','fast','completed',?,?)",
                       (job_id, project, "Old request", "2026-10-01", thread))
        examples = [(101, ["job:a"]), (102, ["job:a", "job:a2"]), (103, ["job:a", "job:b"]),
                    (104, ["job:missing"]), (105, ["user supplied"]), (106, ["job:wrong"]),
                    (107, "malformed JSON"), (108, ["job:console"]), (109, []),
                    (110, ["job:a", "https://example.test/source"]), (111, {"job": "a"})]
        for note_id, sources in examples:
            source_text = sources if isinstance(sources, str) else json.dumps(sources)
            db.execute("INSERT INTO notes(id,project,title,content,kind,sources,updated_at) VALUES (?,'default',?,?,'fact',?,'2026-10-01')",
                       (note_id, f"Old {note_id}", f"Preserved content {note_id}", source_text))
        self.vector = ws._pack([0.5, 1.0])
        db.execute("INSERT INTO embeddings VALUES ('note:101',101,2,?,'2026-10-01')", (self.vector,))
        self.before = [dict(r) for r in db.execute("SELECT * FROM notes ORDER BY id")]
        db.commit()
        db.close()

    def tearDown(self):
        self.patch_data.stop()
        self.temp.cleanup()

    def test_upgrade_preserves_every_note_id_content_source_and_embedding(self):
        ws.init()
        after = ws.query("SELECT * FROM notes ORDER BY id")
        self.assertEqual([{k: v for k, v in n.items() if k != "thread"} for n in after], self.before)
        self.assertEqual([n["thread"] for n in after], ["chat-a", "chat-a"] + [""] * 9)
        self.assertEqual(ws.query("SELECT source,row_id,dim,vector,created_at FROM embeddings"),
                         [{"source": "note:101", "row_id": 101, "dim": 2, "vector": self.vector, "created_at": "2026-10-01"}])
        self.assertEqual({n["id"] for n in ws.memories("default", thread="chat-a")}, {101, 102})
        self.assertEqual(ws.memories("default", thread="chat-b"), [])
        self.assertEqual(len(ws.memories("default", limit=30)), len(self.before))

    def test_repeated_startup_is_idempotent_and_same_titles_can_be_saved_elsewhere(self):
        ws.init()
        before = ws.query("SELECT * FROM notes ORDER BY id")
        for _ in range(2):
            ws.init()
        self.assertEqual(ws.query("SELECT * FROM notes ORDER BY id"), before)
        new = ws.save_note("default", "Old 101", "Different chat", thread="chat-b")
        self.assertNotEqual(new, 101)
        self.assertEqual(ws.query("SELECT content FROM notes WHERE id=101")[0]["content"], "Preserved content 101")
        self.assertEqual(ws.memories("default", thread="chat-b")[0]["id"], new)

    def test_an_interrupted_upgrade_is_retried_on_the_next_start(self):
        # A crash after the copy table was created but before the commit leaves it behind next to the old notes.
        db = sqlite3.connect(store.DATA / "hub.db")
        db.execute("CREATE TABLE notes_scoped (id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL)")
        db.commit()
        db.close()
        ws.init()
        after = ws.query("SELECT * FROM notes ORDER BY id")
        self.assertEqual([{k: v for k, v in n.items() if k != "thread"} for n in after], self.before)
        self.assertFalse(ws.query("SELECT name FROM sqlite_master WHERE name='notes_scoped'"))


if __name__ == "__main__":
    unittest.main()
