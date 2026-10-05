"""Fetcher regressions using mocks and loopback servers, never Tor/onion sites."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tor_fetcher_under_test", ROOT / "tools" / "tor" / "fetch.py")
fetcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetcher)
URL = "http://127.0.0.1:1/mock"


class MockSocksHandler(socketserver.StreamRequestHandler):
    """Accept SOCKS locally and supply an HTTP fixture without forwarding it."""

    def handle(self):
        self.connection.settimeout(3)
        greeting = self.rfile.read(2)
        if len(greeting) != 2 or greeting[0] != 5:
            return
        self.rfile.read(greeting[1])
        self.connection.sendall(b"\x05\x00")
        request = self.rfile.read(4)
        if len(request) != 4:
            return
        atyp = request[3]
        if atyp == 1:
            self.rfile.read(4)
        elif atyp == 3:
            self.rfile.read(self.rfile.read(1)[0])
        elif atyp == 4:
            self.rfile.read(16)
        else:
            return
        self.rfile.read(2)
        self.server.proxy_hits += 1
        self.connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        while self.rfile.readline() not in (b"\r\n", b"", b"\n"):
            pass
        self.connection.sendall(self.server.response)


class MockSocksServer(socketserver.ThreadingTCPServer):
    daemon_threads = True


@contextlib.contextmanager
def mock_socks(response):
    server = MockSocksServer(("127.0.0.1", 0), MockSocksHandler)
    server.proxy_hits = 0
    server.response = response
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextlib.contextmanager
def mock_unix_socks(response, socket_path):
    # Defined at runtime because UnixStreamServer is unavailable on some hosts.
    class UnixSocksServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    server = UnixSocksServer(str(socket_path), MockSocksHandler)
    server.proxy_hits = 0
    server.response = response
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TOR_HOME": "", "TOR_SOCKS_SOCKET": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def completed(returncode=0, status=200, body=b"complete"):
        def run(args, **kwargs):
            Path(args[args.index("-o") + 1]).write_bytes(body)
            return subprocess.CompletedProcess(args, returncode,
                f"{status:03d}\t{len(body)}\t{URL}\t0.1", "provider details")
        return run

    def test_transport_errors_reject_http_metadata_and_partial_content(self):
        for returncode, status in ((7, 0), (18, 200), (28, 200), (35, 0), (60, 0)):
            with self.subTest(returncode=returncode), patch("subprocess.run", self.completed(returncode, status, b"partial")):
                with self.assertRaises(fetcher.FetchError) as raised:
                    fetcher.fetch(URL, rotate=False)
                self.assertEqual(raised.exception.curl_code, returncode)
                self.assertNotIn("provider details", str(raised.exception))

    def test_zero_http_status_is_not_a_successful_transfer(self):
        with patch("subprocess.run", self.completed(status=0, body=b"")):
            with self.assertRaisesRegex(fetcher.FetchError, "without an HTTP response"):
                fetcher.fetch(URL, rotate=False)

    def test_timeout_raises_transport_error_and_removes_partial_file(self):
        paths = []
        def run(args, **kwargs):
            paths.append(Path(args[args.index("-o") + 1]))
            paths[-1].write_bytes(b"partial")
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        with patch("subprocess.run", run), self.assertRaisesRegex(fetcher.FetchError, "timed out"):
            fetcher.fetch(URL, timeout=1, rotate=False)
        self.assertFalse(paths[0].exists())

    def test_private_unix_socket_and_hub_protocol_limits_are_opt_in(self):
        with patch.dict(os.environ, {"TOR_SOCKS_SOCKET": "/controller/tor/run/socks.sock"}), \
                patch("subprocess.run", side_effect=self.completed(body=b"okay")) as run:
            result = fetcher.fetch(URL, rotate=False, http_only=True, max_bytes=4)
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["curl", "-q"])
        self.assertEqual(args[args.index("--proxy") + 1], "socks5h://localhost/controller/tor/run/socks.sock")
        self.assertNotIn("--socks5-hostname", args)
        self.assertEqual(args[args.index("--noproxy") + 1], "")
        self.assertEqual(args[args.index("--proto") + 1], "=http,https")
        self.assertEqual(args[args.index("--proto-redir") + 1], "=http,https")
        self.assertIn("--globoff", args)
        self.assertEqual(args[args.index("--max-filesize") + 1], "4")
        self.assertEqual(args[-2:], ["--url", URL])
        self.assertEqual(result["text"], "okay")

    def test_standalone_defaults_keep_tcp_proxy_and_protocol_behavior(self):
        with patch("subprocess.run", side_effect=self.completed()) as run:
            fetcher.fetch(URL, rotate=False)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--socks5-hostname") + 1], fetcher.SOCKS)
        self.assertNotIn("--proto", args)
        self.assertNotIn("--max-filesize", args)
        self.assertEqual(args[-1], URL)

    def test_oversized_body_is_rejected_before_reading_it(self):
        with patch("subprocess.run", self.completed(body=b"too large")), \
                patch("builtins.open", side_effect=AssertionError("body must not be read")):
            with self.assertRaisesRegex(fetcher.FetchError, "size limit"):
                fetcher.fetch(URL, rotate=False, max_bytes=4)

    def test_invalid_size_limits_never_start_curl(self):
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), patch("subprocess.run") as run:
                with self.assertRaises(ValueError):
                    fetcher.fetch(URL, rotate=False, max_bytes=limit)
                run.assert_not_called()

    def test_tor_home_changes_cookie_path_without_changing_standalone_default(self):
        self.assertEqual(fetcher._cookie_path(), fetcher.COOKIE)
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"TOR_HOME": td}):
            self.assertEqual(fetcher._cookie_path(), os.path.join(td, "run", "control_auth_cookie"))

    @unittest.skipUnless(shutil.which("curl"), "curl executable is unavailable")
    def test_real_curl_ignores_no_proxy_and_default_configuration(self):
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\ncomplete"
        with tempfile.TemporaryDirectory() as td, mock_socks(response) as proxy:
            # This would make curl fail before connecting if -q were omitted.
            Path(td, ".curlrc").write_text("invalid-review-option = 1\n", encoding="ascii")
            with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*", "CURL_HOME": td}), \
                    patch.object(fetcher, "SOCKS", f"127.0.0.1:{proxy.server_address[1]}"):
                result = fetcher.fetch(URL, timeout=2, rotate=False, http_only=True)
            self.assertEqual(result["text"], "complete")
            self.assertEqual(proxy.proxy_hits, 1)

    @unittest.skipUnless(shutil.which("curl"), "curl executable is unavailable")
    def test_real_curl_rejects_truncated_http_200_body(self):
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\npartial"
        with mock_socks(response) as proxy, \
                patch.object(fetcher, "SOCKS", f"127.0.0.1:{proxy.server_address[1]}"):
            with self.assertRaises(fetcher.FetchError) as raised:
                fetcher.fetch(URL, timeout=2, rotate=False, http_only=True)
        self.assertEqual(raised.exception.curl_code, 18)

    @unittest.skipUnless(sys.platform.startswith("linux") and hasattr(socketserver, "UnixStreamServer")
                         and shutil.which("curl"), "requires Linux with Unix sockets and curl")
    def test_real_curl_routes_through_configured_unix_socks_socket(self):
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\ncomplete"
        # Keep the Unix socket path short enough for sockaddr_un's path limit.
        with tempfile.TemporaryDirectory(prefix="tor-socks-", dir="/tmp") as td:
            socket_path = Path(td, "socks")
            with mock_unix_socks(response, socket_path) as proxy, \
                    patch.dict(os.environ, {"TOR_SOCKS_SOCKET": str(socket_path), "NO_PROXY": "*", "no_proxy": "*"}):
                result = fetcher.fetch(URL, timeout=2, rotate=False, http_only=True)
            self.assertEqual(result["status"], 200)
            self.assertEqual(result["text"], "complete")
            self.assertEqual(proxy.proxy_hits, 1)

    @unittest.skipUnless(shutil.which("curl"), "curl executable is unavailable")
    def test_real_curl_hub_reader_rejects_local_file_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            local = Path(td, "fixture.txt")
            local.write_text("local fixture must not be returned", encoding="utf-8")
            with self.assertRaises(fetcher.FetchError) as raised:
                fetcher.fetch(local.as_uri(), timeout=2, rotate=False, http_only=True)
        self.assertEqual(raised.exception.curl_code, 1)

    @unittest.skipUnless(shutil.which("curl"), "curl executable is unavailable")
    def test_real_curl_hub_reader_rejects_response_above_size_limit(self):
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\ncomplete"
        with mock_socks(response) as proxy, \
                patch.object(fetcher, "SOCKS", f"127.0.0.1:{proxy.server_address[1]}"):
            with self.assertRaises(fetcher.FetchError) as raised:
                fetcher.fetch(URL, timeout=2, rotate=False, http_only=True, max_bytes=4)
        self.assertEqual(raised.exception.curl_code, 63)


class CliTests(unittest.TestCase):
    def invoke(self, side_effect, retries=2):
        with tempfile.TemporaryDirectory() as td:
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", ["fetch.py", "--json", "--out", td, "--retry", str(retries), URL]), \
                    patch.object(fetcher, "fetch", side_effect=side_effect) as fetch, \
                    patch.object(fetcher.time, "sleep") as sleep, \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as exited:
                    fetcher.main()
            return exited.exception.code, fetch.call_count, [call.args[0] for call in sleep.call_args_list], out.getvalue(), err.getvalue()

    @staticmethod
    def response(status, text=""):
        return {"url": URL, "status": status, "time": 1, "text": text}

    def test_exhausted_transport_failures_exit_nonzero(self):
        code, calls, delays, out, err = self.invoke(fetcher.FetchError("unavailable"))
        self.assertEqual(code, 2)
        self.assertEqual(calls, 3)
        self.assertEqual(delays, [1, 2])
        self.assertEqual(json.loads(out)["status"], 0)
        self.assertIn("0/1 succeeded", err)

    def test_status_zero_never_counts_as_success(self):
        code, calls, delays, out, err = self.invoke([self.response(0)] * 3)
        self.assertEqual(code, 2)
        self.assertEqual(calls, 3)
        self.assertIn("0/1 succeeded", err)

    def test_transient_error_bodies_are_retried_before_success(self):
        code, calls, delays, out, err = self.invoke([
            self.response(429, "rate limited"), self.response(503, "temporarily unavailable"), self.response(200, "complete")])
        self.assertEqual((code, calls, delays), (0, 3, [1, 2]))
        self.assertEqual(json.loads(out)["status"], 200)

    def test_retry_delays_are_capped_and_skip_sleep_after_last_attempt(self):
        code, calls, delays, out, err = self.invoke(fetcher.FetchError("unavailable"), retries=5)
        self.assertEqual((code, calls), (2, 6))
        self.assertEqual(delays, [1, 2, 4, 8, 8])

    def test_permanent_error_and_empty_success_do_not_retry(self):
        for status, code in ((404, 2), (204, 0)):
            with self.subTest(status=status):
                result = self.invoke([self.response(status)])
                self.assertEqual(result[:3], (code, 1, []))

    def test_failed_fetch_preserves_previously_saved_html(self):
        for outcome in (fetcher.FetchError("unavailable"), self.response(404, "error page")):
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory() as td:
                saved = Path(td, fetcher.slug(URL) + ".html")
                saved.write_bytes(b"previously successful page")
                out = io.StringIO()
                with patch.object(sys, "argv", ["fetch.py", "--out", td, "--retry", "0", URL]), \
                        patch.object(fetcher, "fetch", side_effect=[outcome]), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exited:
                        fetcher.main()
                self.assertEqual(exited.exception.code, 2)
                self.assertEqual(saved.read_bytes(), b"previously successful page")
                self.assertIn("failed", out.getvalue())
                self.assertIn("no file written", out.getvalue())


if __name__ == "__main__":
    unittest.main()
