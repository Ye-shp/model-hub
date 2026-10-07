"""Offline shipping and in-place setup regressions; no package installs, devices, or network."""
import asyncio
from contextlib import closing
import io
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import automation_runtime as runtime
import code_update
import store


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in entries:
            info = tarfile.TarInfo("repo-commit/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Client:
    def __init__(self, payload):
        self.payload, self.urls = payload, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url):
        self.urls.append(url)
        return SimpleNamespace(content=self.payload, raise_for_status=lambda: None)


class AutomationDeployTests(unittest.TestCase):
    def test_additive_schema_init_preserves_legacy_notes_embeddings_and_memory_files(self):
        import social_automation
        import workspace as ws
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "agent-workspace"
            data.mkdir()
            memory = Path(folder) / "cowork" / "owner" / "threads" / "old-chat" / "MEMORY.md"
            memory.parent.mkdir(parents=True)
            memory.write_bytes(b"Old Cowork memory remains here")
            # Deliberately seed the old, unscoped notes schema. Automation itself
            # must not trigger the existing unrelated memory migration.
            with closing(sqlite3.connect(data / "hub.db")) as db:
                db.executescript("""
                    CREATE TABLE notes(id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
                      content TEXT NOT NULL, kind TEXT NOT NULL, sources TEXT NOT NULL,
                      updated_at TEXT NOT NULL, UNIQUE(project,title));
                    CREATE TABLE embeddings(source TEXT, row_id INTEGER, dim INTEGER, vector BLOB, created_at TEXT);
                    CREATE TABLE documents(id TEXT PRIMARY KEY, content_hash TEXT);
                """)
                db.execute("INSERT INTO notes VALUES(101,'default','Old note','Preserved content','fact','[]','2026-10-01')")
                db.execute("INSERT INTO embeddings VALUES('note:101',101,2,?,'2026-10-01')", (b"old-embedding",))
                db.execute("INSERT INTO documents VALUES('old-doc','unchanged-hash')")
                db.commit()
                schemas = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('notes','embeddings','documents')"))
                rows = {name: db.execute(f"SELECT * FROM {name}").fetchall() for name in schemas}
            with patch.object(store, "DATA", data), \
                 patch.object(ws, "_migrate_note_threads", side_effect=AssertionError("Automation must not migrate memory")):
                social_automation.init()
                social_automation.init()
            with closing(sqlite3.connect(data / "hub.db")) as db:
                for name, schema in schemas.items():
                    self.assertEqual(db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0], schema)
                    self.assertEqual(db.execute(f"SELECT * FROM {name}").fetchall(), rows[name])
                self.assertTrue(db.execute("SELECT name FROM sqlite_master WHERE name='automation_queue'").fetchone())
            self.assertEqual(memory.read_bytes(), b"Old Cowork memory remains here")
            self.assertFalse((data / "automation").exists())

    def test_code_staging_ships_automation_and_preserves_memory_and_runtime_files(self):
        with tempfile.TemporaryDirectory() as folder:
            persistent = Path(folder) / "data"
            data = persistent / "agent-workspace"
            code = persistent / "hub-code"
            data.mkdir(parents=True)
            code.mkdir()
            # Include actual database memory/vector records, not only a file sentinel.
            db = sqlite3.connect(data / "hub.db")
            db.executescript("CREATE TABLE notes(id INTEGER PRIMARY KEY, content TEXT);"
                             "CREATE TABLE embeddings(source TEXT, vector BLOB);")
            db.execute("INSERT INTO notes VALUES (101, 'Remember this existing chat')")
            db.execute("INSERT INTO embeddings VALUES ('note:101', ?)", (b"existing-vector",))
            db.commit()
            db.close()
            files = {data / "hub.db": (data / "hub.db").read_bytes(),
                     data / "automation" / "config.json": b'{"existing_account": true}',
                     data / "posts" / "42" / "video.mp4": b"retained-video",
                     Path(folder) / "cowork" / "owner" / "threads" / "chat-1" / "MEMORY.md": b"Existing memory file"}
            for path, content in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            # Exercise code snapshot cleanup too; data remains outside the code tree.
            for n in range(7):
                (code / str(n)).mkdir()
            ref = "a" * 40
            client = Client(archive([
                ("agents/console.py", b"# new code"),
                ("agents/social_automation.py", b"# additive backend"),
                ("tools/automation/bot/device.py", b"# controller"),
                ("data/agent-workspace/hub.db", b"must not replace database"),
                ("cowork/owner/threads/chat-1/MEMORY.md", b"must not replace memory"),
            ]))
            with patch.dict(os.environ, {"HUB_CODE_DIR": str(code)}), \
                 patch.object(store, "DATA", data), \
                 patch.object(code_update.httpx, "AsyncClient", return_value=client), \
                 patch.object(runtime, "ready", return_value={"ready": False, "missing": ["adb"]}):
                result = asyncio.run(code_update.stage(ref))
            self.assertEqual(result["files"], 3)
            self.assertEqual((code / "active").read_text(), ref)
            self.assertTrue((code / ref / "tools" / "automation" / "bot" / "device.py").is_file())
            self.assertFalse((code / ref / "data").exists())
            self.assertEqual(result["phone_automation"]["missing"], ["adb"])
            self.assertIn("in place", result["phone_automation"]["next"])
            for path, content in files.items():
                self.assertEqual(path.read_bytes(), content, str(path))
            with closing(sqlite3.connect(data / "hub.db")) as db:
                self.assertEqual(db.execute("SELECT * FROM notes").fetchall(), [(101, "Remember this existing chat")])
                self.assertEqual(db.execute("SELECT * FROM embeddings").fetchall(), [("note:101", b"existing-vector")])

    def test_automation_archive_still_refuses_directory_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                code_update.extract(archive([("tools/automation/../../../memory.txt", b"bad")]), Path(folder))

    def test_source_only_extraction_cannot_write_another_app_folder_via_dotdot(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "outside app folders"):
                code_update.extract(archive([("tools/automation/../../agents/console.py", b"bad")]),
                                    Path(folder), parts=("tools/automation/",))
            self.assertFalse((Path(folder) / "agents").exists())


class AutomationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "agent-workspace"
        self.patches = [patch.object(store, "DATA", self.data), patch.object(runtime, "_LOCK", asyncio.Lock()),
                        patch.object(sys, "path", list(sys.path))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_readiness_does_not_create_data_or_contact_a_device(self):
        with patch.object(runtime.importlib.util, "find_spec", return_value=None), \
             patch.object(runtime.shutil, "which", return_value=None), \
             patch.object(runtime, "_run", new_callable=AsyncMock) as run:
            state = runtime.ready()
        self.assertFalse(state["ready"])
        self.assertIn("adb", state["missing"])
        self.assertFalse(self.data.exists())
        run.assert_not_awaited()

    def test_readiness_registers_existing_persistent_dependencies_after_restart(self):
        target = self.data / "automation" / "python"
        target.mkdir(parents=True)
        versions = {name: version for name, _, version in runtime.PACKAGES}
        with patch.object(runtime.importlib.util, "find_spec", return_value=object()), \
             patch.object(runtime.importlib.metadata, "version", side_effect=versions.__getitem__), \
             patch.object(runtime.shutil, "which", return_value="/usr/bin/adb"):
            self.assertTrue(runtime.ready()["ready"])
        self.assertIn(str(target), sys.path)

    def test_explicit_setup_installs_only_into_automation_target_and_preserves_memories(self):
        self.data.mkdir()
        memories = {self.data / "hub.db": b"existing database", self.data / "notes.md": b"existing notes",
                    self.data / "automation" / "config.json": b"existing account config"}
        for path, content in memories.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        missing = [f"{name}=={version}" for name, _, version in runtime.PACKAGES] + ["adb"]
        states = [{"ready": False, "missing": missing}, {"ready": True, "missing": []}]
        def which(name):
            return "/usr/bin/apt-get" if name == "apt-get" else None
        with patch.object(runtime, "ready", side_effect=states), \
             patch.object(runtime.shutil, "which", side_effect=which), \
             patch.object(runtime.os, "geteuid", return_value=0, create=True), \
             patch.object(runtime, "_run", new_callable=AsyncMock) as run:
            self.assertTrue(asyncio.run(runtime.ensure())["ready"])
        self.assertEqual(run.await_count, 3)
        commands = [call.args[0] for call in run.await_args_list]
        pip = commands[0]
        self.assertEqual(pip[:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(pip[pip.index("--target") + 1], str(self.data / "automation" / "python"))
        self.assertTrue(set(missing[:-1]).issubset(pip))
        self.assertEqual(commands[-1], ["/usr/bin/apt-get", "install", "-y", "--no-install-recommends", "adb"])
        for call in run.await_args_list:
            self.assertGreater(call.kwargs["timeout"], 0)
            if "env" in call.kwargs:
                self.assertEqual(call.kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive")
        for path, content in memories.items():
            self.assertEqual(path.read_bytes(), content)

    def test_already_prepared_runtime_never_reinstalls(self):
        with patch.object(runtime, "ready", return_value={"ready": True, "missing": []}), \
             patch.object(runtime, "_run", new_callable=AsyncMock) as run:
            asyncio.run(runtime.ensure())
        run.assert_not_awaited()
        self.assertFalse(self.data.exists())

    def test_missing_adb_without_root_reports_in_place_preparation_requirement(self):
        with patch.object(runtime, "ready", return_value={"ready": False, "missing": ["adb"]}), \
             patch.object(runtime.shutil, "which", return_value="/usr/bin/apt-get"), \
             patch.object(runtime.os, "geteuid", return_value=1000, create=True), \
             patch.object(runtime, "_run", new_callable=AsyncMock) as run:
            # shutil.which(adb) must remain absent; apt-get alone is present.
            runtime.shutil.which.side_effect = lambda name: "/usr/bin/apt-get" if name == "apt-get" else None
            with self.assertRaisesRegex(RuntimeError, "persistent volume"):
                asyncio.run(runtime.ensure())
        run.assert_not_awaited()
        self.assertFalse(self.data.exists())

    def test_timed_out_and_cancelled_setup_kills_its_subprocess(self):
        for failure in (asyncio.TimeoutError(), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                proc = SimpleNamespace(returncode=None, pid=1234, kill=Mock(),
                                       communicate=AsyncMock(side_effect=[failure, (b"", None)]))
                with patch.object(runtime.asyncio, "create_subprocess_exec", new_callable=AsyncMock,
                                  return_value=proc), \
                     patch.object(runtime.os, "killpg", create=True) as kill_group:
                    expected = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else RuntimeError
                    with self.assertRaises(expected):
                        asyncio.run(runtime._run([sys.executable, "-m", "pip"], timeout=1))
                    if os.name == "posix":
                        kill_group.assert_called_once_with(proc.pid, runtime.signal.SIGKILL)
                    else:
                        proc.kill.assert_called_once()
                self.assertEqual(proc.communicate.await_count, 2)

    def test_source_repair_fetches_only_exact_running_commit_and_leaves_other_code_and_data(self):
        ref = "a" * 40
        app = Path(self.temp.name) / "hub-code" / ref
        original = app / "agents" / "console.py"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"existing running controller")
        (app.parent / "active").write_text("b" * 40)  # another staged update is not the running version
        self.data.mkdir()
        memory = self.data / "MEMORY.md"
        memory.write_bytes(b"preserve memory")
        client = Client(archive([
            *(('tools/automation/bot/' + name, b"# bundled source") for name in runtime.SOURCE_FILES),
            ("agents/console.py", b"must not replace running controller"),
            ("data/MEMORY.md", b"must not replace memory"),
        ]))
        with patch.object(runtime, "ROOT", app), \
             patch.object(code_update.httpx, "AsyncClient", return_value=client) as factory:
            asyncio.run(runtime._ensure_source())
            self.assertTrue(runtime._source_ready())
        self.assertEqual(client.urls, [f"https://codeload.github.com/{code_update.REPO}/tar.gz/{ref}"])
        self.assertEqual(factory.call_args.kwargs["timeout"], 120)
        self.assertEqual(original.read_bytes(), b"existing running controller")
        self.assertEqual(memory.read_bytes(), b"preserve memory")

    def test_missing_source_without_running_commit_never_fetches_latest(self):
        with patch.object(runtime, "ROOT", Path(self.temp.name) / "unversioned-checkout"), \
             patch.object(code_update.httpx, "AsyncClient") as client:
            with self.assertRaisesRegex(RuntimeError, "same code version"):
                asyncio.run(runtime._ensure_source())
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
