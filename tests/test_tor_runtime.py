"""Fake process contracts and Ubuntu24 package/config validation; no onion requests."""
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import code_update

GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.is_file() else shutil.which("bash")
UBUNTU24 = sys.platform == "linux" and Path("/etc/os-release").is_file() \
    and 'VERSION_ID="24.04"' in Path("/etc/os-release").read_text() \
    and "ID=ubuntu" in Path("/etc/os-release").read_text()


def bash_path(path):
    value = Path(path).resolve().as_posix()
    if os.name == "nt":
        value = "/" + value[0].lower() + value[2:]
    return value


@unittest.skipUnless(BASH, "bash is required for the fake-runtime tests")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT.parent, prefix="tor-runtime-test-")
        self.folder = Path(self.temp.name).resolve()
        self.assertTrue(self.folder.is_relative_to(ROOT.parent.resolve()))
        self.code = self.folder / "code"
        self.code.mkdir()
        self.runtime = self.folder / "private" / "tor"
        self.runtime.mkdir(parents=True)
        self.stubs = self.folder / "stubs"
        self.stubs.mkdir()
        for name in ("setup.sh", "torctl.sh", "torrc"):
            shutil.copyfile(ROOT / "tools" / "tor" / name, self.code / name)
        self.calls = self.folder / "calls"
        self.tor_called = self.folder / "tor-executed"
        self.script("curl", 'printf "curl %s\\n" "$*" >> "$CALLS"\n'
                    'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then out="$2"; shift; fi; shift; done\n'
                    'printf "corrupted package" > "$out"\n')
        self.script("dpkg-deb", 'printf "EXTRACTION %s\\n" "$*" >> "$CALLS"\nexit 99\n')
        self.script("python3", "exit 0\n")
        self.env = dict(os.environ, TOR_HOME=bash_path(self.runtime), CALLS=bash_path(self.calls),
                        TOR_EXECUTED=bash_path(self.tor_called), TEST_STUBS=bash_path(self.stubs))
        self.env.pop("TOR_SOCKS_SOCKET", None)
        # Bash receives a Unix PATH, including the real checksum utility.
        base_path = subprocess.check_output([BASH, "-c", 'printf "%s" "$PATH"'], text=True).strip()
        self.env["PATH"] = bash_path(self.stubs) + ":" + base_path

    def tearDown(self):
        self.assertTrue(self.folder.is_relative_to(ROOT.parent.resolve()))
        self.temp.cleanup()

    def script(self, name, body):
        file = self.stubs / name
        file.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
        file.chmod(0o755)

    def run_script(self, name, *args):
        # Override these boundaries explicitly: Git Bash prepends its own curl
        # directory during startup on Windows, even with a prepared inherited PATH.
        wrapper = 'export PATH="$TEST_STUBS:$PATH"; curl() { "$TEST_STUBS/curl" "$@"; }; '
        wrapper += 'dpkg-deb() { "$TEST_STUBS/dpkg-deb" "$@"; }; source "$1" "${@:2}"'
        return subprocess.run([BASH, "-c", wrapper, "test-runtime", bash_path(self.code / name), *args], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def seed_cache(self):
        (self.runtime / "bin").mkdir()
        (self.runtime / "lib").mkdir()
        tor = self.runtime / "bin" / "tor"
        tor.write_text('#!/usr/bin/env bash\nprintf "executed" > "$TOR_EXECUTED"\n',
                       encoding="utf-8", newline="\n")
        tor.chmod(0o755)
        lib = self.runtime / "lib" / "libevent-2.1.so.7"
        lib.write_bytes(b"fake cached libevent")
        source = (self.code / "setup.sh").read_text()
        values = [re.search(rf'^{key}="([^"]+)"', source, re.M).group(1)
                  for key in ("TOR_VER", "TOR_SHA256", "EV_VER", "EV_SHA256")]
        (self.runtime / "installed-packages").write_text(" ".join(values) + "\n")
        (self.runtime / "installed-files.sha256").write_text(
            "".join(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.relative_to(self.runtime).as_posix()}\n"
                    for file in (tor, lib)), encoding="utf-8", newline="\n")

    def test_corrupted_download_never_extracts_or_executes(self):
        result = self.run_script("setup.sh", "--install-only")
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls.read_text()
        self.assertNotIn("EXTRACTION", calls)
        self.assertFalse(self.tor_called.exists())
        self.assertFalse((self.runtime / "bin" / "tor").exists())
        for call in calls.splitlines():
            self.assertIn("--proto =https --proto-redir =https", call)
            self.assertIn("https://", call)
            self.assertNotIn(" http://", call)

    def test_verified_cache_survives_restart_without_download_or_daemon(self):
        self.seed_cache()
        result = self.run_script("setup.sh", "--install-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("using verified persistent Tor", result.stdout)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.tor_called.exists())

    def test_modified_cached_binary_is_not_reused(self):
        self.seed_cache()
        (self.runtime / "bin" / "tor").write_bytes(b"modified binary")
        result = self.run_script("setup.sh", "--install-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("curl", self.calls.read_text())
        self.assertNotIn("EXTRACTION", self.calls.read_text())
        self.assertFalse(self.tor_called.exists())

    def test_hub_foreground_uses_private_paths_and_propagates_process_exit(self):
        (self.runtime / "bin").mkdir()
        tor = self.runtime / "bin" / "tor"
        tor.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\nexit 23\n',
                       encoding="utf-8", newline="\n")
        tor.chmod(0o755)
        socket = self.runtime / "run" / "socks.sock"
        self.env["TOR_SOCKS_SOCKET"] = bash_path(socket)
        result = self.run_script("torctl.sh", "foreground")
        self.assertEqual(result.returncode, 23)
        args = result.stdout.splitlines()
        self.assertEqual(args[args.index("--DataDirectory") + 1], bash_path(self.runtime / "run"))
        self.assertEqual(args[args.index("--SocksPort") + 1], "unix:" + bash_path(socket))
        self.assertEqual(args[args.index("--ControlPort") + 1], "0")
        self.assertEqual(args[-2:], ["--RunAsDaemon", "0"])
        self.assertNotIn("127.0.0.1:9151", args)


class DeploymentTests(unittest.TestCase):
    @unittest.skipUnless(UBUNTU24 and BASH, "real package/config check requires Ubuntu 24.04")
    def test_authenticated_ubuntu_packages_accept_private_foreground_config(self):
        # This downloads verified Ubuntu packages and parses Tor's config only.
        # --verify-config does not start a daemon, open SOCKS, or contact relays.
        with tempfile.TemporaryDirectory(prefix="hub-tor-config-") as folder:
            runtime = Path(folder).resolve()
            env = dict(os.environ, TOR_HOME=str(runtime))
            installed = subprocess.run([BASH, str(ROOT / "tools" / "tor" / "setup.sh"), "--install-only"],
                                       env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            env["LD_LIBRARY_PATH"] = str(runtime / "lib")
            verified = subprocess.run([
                str(runtime / "bin" / "tor"), "--verify-config", "-f", str(ROOT / "tools" / "tor" / "torrc"),
                "--DataDirectory", str(runtime / "run"), "--PidFile", str(runtime / "run" / "tor.pid"),
                "--Log", f"notice file {runtime / 'run' / 'tor.log'}",
                "--SocksPort", f"unix:{runtime / 'run' / 'socks.sock'}", "--ControlPort", "0", "--RunAsDaemon", "0",
            ], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            self.assertIn("Configuration was valid", verified.stdout)
            self.assertFalse((runtime / "run" / "socks.sock").exists())
            self.assertFalse((runtime / "run" / "tor.pid").exists())

    def test_overlay_extracts_only_tor_toolkit_from_tools(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for name in ("repo/tools/tor/fetch.py", "repo/tools/tor/setup.sh", "repo/tools/other/unsafe.sh"):
                item = tarfile.TarInfo(name)
                item.size = 1
                tar.addfile(item, io.BytesIO(b"x"))
        with tempfile.TemporaryDirectory(dir=ROOT.parent, prefix="tor-overlay-test-") as folder:
            target = Path(folder).resolve()
            self.assertTrue(target.is_relative_to(ROOT.parent.resolve()))
            self.assertEqual(code_update.extract(archive.getvalue(), target), 2)
            self.assertTrue((target / "tools" / "tor" / "setup.sh").is_file())
            self.assertFalse((target / "tools" / "other").exists())

    @unittest.skipUnless(shutil.which("node"), "Node is required for supervisor process tests")
    def test_installer_completion_starts_foreground_tor_and_exit_restarts_it(self):
        # Evaluate the actual scoped supervisor functions with fake children/timers.
        program = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const source = fs.readFileSync(process.argv[1], 'utf8');
const keep = source.slice(source.indexOf('function keepRunning('), source.indexOf('\nfunction startModels('));
const startAt = source.indexOf('  async function startTor()');
const start = source.slice(startAt, source.indexOf('\n  startTor().catch', startAt));
const spawns = [], timers = [];
const context = {
  APP: '/active/code', TOR_HOME: '/private/tor', TOR_SOCKS_SOCKET: '/private/tor/run/socks.sock',
  torEnv: {TOR_HOME: '/private/tor', TOR_SOCKS_SOCKET: '/private/tor/run/socks.sock'},
  children: new Set(), stopping: false, console: {log(){}, error(){}},
  join: require('node:path').posix.join, mkdir: async()=>{}, chmod: async()=>{},
  setTimeout: (fn, delay) => { const timer = {fn, delay}; timers.push(timer); return timer; },
  clearTimeout: timer => {timer.cleared = true;},
  spawn: (command, args, options) => {
    const child = new EventEmitter(); child.kill = ()=>{};
    spawns.push({command, args, options, child}); return child;
  },
};
vm.runInNewContext(keep + '\n' + start + '\nglobalThis.runTor = startTor;', context);
(async()=>{
  const ready = context.runTor();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(spawns.length, 1);
  assert.equal(spawns[0].command, '/bin/bash');
  assert.deepEqual(Array.from(spawns[0].args), ['/active/code/tools/tor/setup.sh', '--install-only']);
  spawns[0].child.emit('exit', 0);
  await ready;
  assert.equal(spawns.length, 2);
  assert.deepEqual(Array.from(spawns[1].args), ['/active/code/tools/tor/torctl.sh', 'foreground']);
  assert.equal(spawns[1].options.env.TOR_SOCKS_SOCKET, '/private/tor/run/socks.sock');
  spawns[1].child.emit('exit', 23);
  const restart = timers.find(timer=>timer.delay===5000);
  assert.ok(restart);
  restart.fn();
  assert.equal(spawns.length, 3);
  assert.deepEqual(spawns[2].args, spawns[1].args);
  // Failed installation must never launch a daemon with unverified/missing files.
  const failed = context.runTor().then(()=>null, error=>error);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(spawns.length, 4);
  spawns[3].child.emit('exit', 1);
  assert.match((await failed).message, /installer exited/);
  assert.equal(spawns.length, 4);
  console.log('verified installer completion and foreground restart');
})().catch(error=>{console.error(error); process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", program, str(ROOT / "deploy" / "supervise.mjs")],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
