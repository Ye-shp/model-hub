"""Tor run by the controller on boxes whose image supervisor doesn't start it (tor_service.py)."""
import asyncio
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import tor_fetch
import tor_service


def archive(files: dict) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"model-hub-abc/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class TorServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(tor_service._state.update, {"state": "off", "detail": "", "managed_by": None})

    async def test_supervisor_managed_tor_is_left_alone(self):
        with patch.dict(os.environ, {"TOR_SOCKS_SOCKET": "/data/tor/run/socks.sock", "HUB_ROOT_DATA_DIR": str(self.root)}):
            self.assertIsNone(tor_service.start())
        self.assertEqual(tor_service.status()["managed_by"], "supervisor")
        self.assertFalse((self.root / "tor").exists())

    async def test_off_the_hub_box_nothing_starts(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(tor_service.start())
            self.assertNotIn("TOR_SOCKS_SOCKET", os.environ)

    def test_kit_falls_back_to_the_copy_in_tor_home(self):
        fake_agents = self.root / "code" / "agents"
        fake_agents.mkdir(parents=True)
        kit = self.root / "tor" / "kit"
        kit.mkdir(parents=True)
        (kit / "fetch.py").write_text("")
        with patch.object(tor_service, "HERE", fake_agents), patch.dict(os.environ, {"TOR_HOME": str(self.root / "tor")}):
            self.assertEqual(tor_service.kit_dir(), kit)
            bundled = self.root / "code" / "tools" / "tor"
            bundled.mkdir(parents=True)
            (bundled / "fetch.py").write_text("")
            self.assertEqual(tor_service.kit_dir(), bundled)

    def test_fetched_kit_contains_only_tools_tor(self):
        data = archive({"tools/tor/fetch.py": "x", "tools/tor/setup.sh": "y", "agents/console.py": "z", "README.md": "r"})
        tor_service._replace_kit(data, self.root / "kit.tmp", self.root / "kit")
        self.assertEqual(sorted(p.name for p in (self.root / "kit").iterdir()), ["fetch.py", "setup.sh"])
        self.assertFalse((self.root / "kit.tmp").exists())

    async def test_installs_then_keeps_tor_running_and_stops_it_on_shutdown(self):
        kit = self.root / "kit"
        kit.mkdir()
        (kit / "fetch.py").write_text("")
        (kit / "setup.sh").write_text('echo "$TOR_HOME|$TOR_SOCKS_SOCKET|$1" > "$TOR_HOME/installed"\n')
        (kit / "torctl.sh").write_text('echo "$$" > "$TOR_HOME/pid"; echo "$1" > "$TOR_HOME/ran"; exec sleep 30\n')
        home = self.root / "data" / "tor"
        with patch.object(tor_service, "kit_dir", return_value=kit):
            task = asyncio.create_task(tor_service._serve(home, home / "run" / "socks.sock"))
            for _ in range(100):
                if (home / "ran").exists():
                    break
                await asyncio.sleep(0.05)
            self.assertEqual((home / "installed").read_text().strip(), f"{home}|{home}/run/socks.sock|--install-only")
            self.assertEqual((home / "ran").read_text().strip(), "foreground")
            self.assertEqual(tor_service.status()["state"], "running")
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        pid = int((home / "pid").read_text())
        await asyncio.sleep(0.1)
        self.assertFalse(Path(f"/proc/{pid}").exists() and "sleep" in Path(f"/proc/{pid}/cmdline").read_text())

    async def test_failed_install_is_reported_and_tor_is_not_started(self):
        kit = self.root / "kit"
        kit.mkdir()
        (kit / "setup.sh").write_text("exit 3\n")
        (kit / "torctl.sh").write_text('touch "$TOR_HOME/ran"\n')
        home = self.root / "tor"
        with patch.object(tor_service, "kit_dir", return_value=kit):
            await tor_service._serve(home, home / "run" / "socks.sock")
        self.assertEqual(tor_service.status()["state"], "unavailable")
        self.assertIn("code 3", tor_service.status()["detail"])
        self.assertFalse((home / "ran").exists())

    async def test_reader_reports_the_service_state_instead_of_a_curl_error(self):
        tor_service._state.update(state="starting", detail="installing Tor", managed_by="controller")
        with patch.dict(os.environ, {"TOR_SOCKS_SOCKET": str(self.root / "socks.sock")}), \
                patch.object(tor_fetch, "_fetcher") as reader:
            with self.assertRaisesRegex(RuntimeError, "starting: installing Tor"):
                await tor_fetch.read_page("http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/")
        reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
