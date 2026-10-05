"""Real SDK OAuth flows against an HTTP mock; no provider or paid tool calls."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import higgsfield as hf
import connectors
import store
try:
    import httpx2 as httpx
except ImportError:
    import httpx


METADATA = {
    "issuer": hf.ISSUER,
    "authorization_endpoint": hf.ISSUER + "/oauth/authorize",
    "token_endpoint": hf.ISSUER + "/oauth/token",
    "registration_endpoint": hf.ISSUER + "/oauth/register",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "token_endpoint_auth_methods_supported": ["none"],
    "scopes_supported": ["openid", "email", "offline_access"],
    "code_challenge_methods_supported": ["S256"],
    "authorization_response_iss_parameter_supported": True,
}
PRM = "https://mcp.higgsfield.ai/.well-known/oauth-protected-resource/mcp"
PROTOCOL = {"MCP-Protocol-Version": "2025-06-18"}


class ProviderFixture:
    def __init__(self):
        self.registrations = []
        self.exchanges = []
        self.refreshes = []
        self.requests = []
        self.servers = []
        self.metadata = dict(METADATA)
        self.bad_token = False
        self.fail_list = False
        self.refresh_fail = False
        self.token_redirect = None
        self.expected_refresh = "refresh-private-0"
        self.valid_access = "access-private-0"
        self.block_connect = None
        self.block_list = None
        self.list_started = asyncio.Event()
        self.block_cleanup = False
        self.cancel_cleanup = False
        self.wrap_auth_error = False

    def response(self, request):
        self.requests.append(request)
        url = str(request.url)
        if url == hf.MCP_URL:
            if request.headers.get("Authorization") == "Bearer " + self.valid_access:
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{PRM}", scope="openid email offline_access"'})
        if url == PRM:
            return httpx.Response(200, json={"resource": hf.MCP_URL, "authorization_servers": [hf.ISSUER],
                                             "scopes_supported": ["openid", "email", "offline_access"]})
        if request.url.host == "clerk.higgsfield.ai" and ".well-known/" in request.url.path:
            return httpx.Response(200, json=self.metadata)
        if url == hf.ISSUER + "/oauth/register":
            metadata = json.loads(request.content)
            self.registrations.append(metadata)
            return httpx.Response(201, json={**metadata, "client_id": "registered-client", "token_endpoint_auth_method": "none"})
        if url == hf.ISSUER + "/oauth/token":
            data = parse_qs(request.content.decode())
            if data.get("grant_type") == ["refresh_token"]:
                self.refreshes.append(data)
                if self.refresh_fail:
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh-private-0"})
                if data.get("refresh_token") != [self.expected_refresh]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.valid_access = f"access-private-{len(self.refreshes)}"
                self.expected_refresh = f"refresh-private-{len(self.refreshes)}"
            else:
                self.exchanges.append(data)
            if self.token_redirect:
                return httpx.Response(307, headers={"Location": self.token_redirect})
            if self.bad_token:
                return httpx.Response(400, json={"error_description": "access-private-leak refresh-private-leak"})
            return httpx.Response(200, json={"access_token": self.valid_access, "refresh_token": self.expected_refresh,
                                             "expires_in": 3600, "token_type": "Bearer"})
        raise AssertionError("Unexpected mocked endpoint: " + request.url.host + request.url.path)

    def server(self, provider):
        fixture = self

        class Server:
            def __init__(self):
                self.tasks = []
                self.cleaned = False

            async def connect(self):
                self.tasks.append(asyncio.current_task())
                if fixture.block_connect:
                    await fixture.block_connect.wait()
                async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(fixture.response)) as client:
                    try:
                        response = await client.get(hf.MCP_URL, headers=PROTOCOL)
                    except hf.HiggsfieldError as error:
                        if fixture.wrap_auth_error:
                            raise ExceptionGroup("private transport wrapper", [error]) from None
                        raise
                    response.raise_for_status()

            async def list_tools(self):
                self.tasks.append(asyncio.current_task())
                fixture.list_started.set()
                if fixture.block_list:
                    await fixture.block_list.wait()
                if fixture.fail_list:
                    raise RuntimeError("private provider response")
                return [SimpleNamespace(name="models_list"), SimpleNamespace(name="create_video")]

            async def cleanup(self):
                self.tasks.append(asyncio.current_task())
                self.cleaned = True
                if fixture.block_cleanup:
                    await asyncio.Event().wait()
                if fixture.cancel_cleanup:
                    raise asyncio.CancelledError

        server = Server()
        self.servers.append(server)
        return server


class OAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(store, "DATA", Path(self.directory.name))
        self.data_patch.start()
        hf._pending = None
        hf._cached = None
        hf._generation = 0
        hf._last_error = None
        self.provider = ProviderFixture()
        self.server_patch = patch.object(hf, "_make_server", side_effect=self.provider.server)
        self.server_patch.start()

    async def asyncTearDown(self):
        await hf.disconnect()
        self.server_patch.stop()
        self.data_patch.stop()
        self.directory.cleanup()

    async def sign_in(self, public_url="https://console.example"):
        result = await hf.connect(public_url)
        self.assertEqual(result["status"], "awaiting_sign_in")
        self.authorization = parse_qs(urlsplit(result["authorization_url"]).query)
        return await hf.complete("owner-code-private", self.authorization["state"][0], issuer=hf.ISSUER)

    async def request(self, provider=None):
        async with httpx.AsyncClient(auth=provider or hf.auth(), transport=httpx.MockTransport(self.provider.response)) as client:
            return await client.get(hf.MCP_URL, headers=PROTOCOL)

    def expire(self):
        data = hf._read()
        data["expires_at"] = time.time() - 20
        hf._write(data)
        hf._cached = None

    async def test_sdk_pkce_registration_state_resource_and_private_persistence(self):
        connectors._save({"mcp": {"existing": {"transport": "http", "url": "https://other.example"}}, "api": {"existing": {"key": "private"}}})
        result = await self.sign_in()
        self.assertTrue(result["connected"])
        self.assertEqual(result["tools"], ["models_list", "create_video"])
        self.assertEqual(hf.status(), result)
        self.assertEqual(self.provider.registrations[0]["redirect_uris"], ["https://console.example" + hf.CALLBACK_PATH])
        self.assertEqual(self.provider.registrations[0]["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(self.provider.registrations[0]["application_type"], "web")
        self.assertEqual(self.provider.registrations[0]["client_name"], "Model Hub Cowork")
        exchange = self.provider.exchanges[0]
        verifier = exchange["code_verifier"][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, self.authorization["code_challenge"][0])
        self.assertEqual(self.authorization["code_challenge_method"], ["S256"])
        self.assertEqual(self.authorization["resource"], [hf.MCP_URL])
        self.assertEqual(exchange["resource"], [hf.MCP_URL])
        data = hf._read()
        self.assertAlmostEqual(data["expires_at"] - time.time(), 3600, delta=3)
        entries = connectors.load()
        self.assertIn("existing", entries["mcp"])
        self.assertIn("existing", entries["api"])
        self.assertNotIn("headers", entries["mcp"]["higgsfield"])
        self.assertNotIn("private-0", json.dumps(entries["mcp"]["higgsfield"]))
        for word in ("access-private", "refresh-private", "owner-code-private", self.authorization["state"][0]):
            self.assertNotIn(word, json.dumps(result))
        server = self.provider.servers[0]
        self.assertTrue(server.cleaned)
        self.assertEqual(len(set(server.tasks)), 1)
        if os.name != "nt":
            self.assertEqual(hf._path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(store.DATA.glob(".higgsfield-*.tmp")), [])

    async def test_restart_uses_absolute_expiry_and_correct_issuer_for_rotating_refresh(self):
        await self.sign_in()
        self.expire()
        provider = hf.auth()
        self.assertEqual((await self.request(provider)).status_code, 200)
        self.assertEqual(len(self.provider.refreshes), 1)
        self.assertEqual(self.provider.refreshes[0]["refresh_token"], ["refresh-private-0"])
        data = hf._read()
        self.assertEqual(data["tokens"]["refresh_token"], "refresh-private-1")
        self.assertEqual(data["tokens"]["access_token"], "access-private-1")
        self.assertGreater(data["expires_at"], time.time() + 3500)
        hf._cached = None  # Simulate another restart; it must not reset lifetime from expires_in.
        self.assertEqual((await self.request()).status_code, 200)
        self.assertEqual(len(self.provider.refreshes), 1)
        self.assertEqual(len(self.provider.registrations), 1)
        self.assertEqual(len(self.provider.exchanges), 1)

    async def test_cached_provider_serializes_concurrent_refreshes(self):
        await self.sign_in()
        self.expire()
        provider = hf.auth()
        self.assertIs(provider, hf.auth())
        responses = await asyncio.gather(self.request(provider), self.request(provider))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(len(self.provider.refreshes), 1)

    async def test_refresh_failure_requires_reconnect_without_interactive_prompt_or_secret(self):
        await self.sign_in()
        self.expire()
        self.provider.refresh_fail = True
        with self.assertLogs("mcp.client.auth.oauth2", level="WARNING") as logs:
            with self.assertRaisesRegex(hf.HiggsfieldError, "reconnect_required"):
                await self.request()
        self.assertNotIn("refresh-private-0", "\n".join(logs.output))
        self.assertEqual(len(self.provider.exchanges), 1)
        self.assertIsNone(hf._pending)
        self.assertEqual(hf.status()["status"], "reconnect_required")

    async def test_wrong_missing_reused_and_expired_state_cannot_exchange(self):
        initial = await hf.connect("http://127.0.0.1:8080")
        state = parse_qs(urlsplit(initial["authorization_url"]).query)["state"][0]
        for bad in (None, "", "wrong-state", "é" * 43, "a" * 10000):
            result = await hf.complete("private-code", bad, issuer=hf.ISSUER)
            self.assertEqual(result["error"], "invalid_state")
        self.assertEqual(self.provider.exchanges, [])
        hf._pending.deadline = time.monotonic() - 1
        self.assertEqual((await hf.complete("private-code", state, issuer=hf.ISSUER))["error"], "invalid_state")
        hf._pending.deadline = time.monotonic() + 60
        result = await hf.complete("private-code", state, issuer=hf.ISSUER)
        self.assertTrue(result["connected"])
        self.assertEqual((await hf.complete("private-code", state, issuer=hf.ISSUER))["error"], "invalid_state")
        self.assertEqual(len(self.provider.exchanges), 1)

    async def test_missing_or_wrong_issuer_is_rejected_before_exchange(self):
        for issuer in (None, "https://other.example", hf.ISSUER + "/"):
            initial = await hf.connect("https://console.example")
            state = parse_qs(urlsplit(initial["authorization_url"]).query)["state"][0]
            result = await hf.complete("private-code", state, issuer=issuer)
            self.assertEqual(result["error"], "authorization_failed")
            self.assertEqual(self.provider.exchanges, [])
            self.assertFalse(hf._path().exists())

    async def test_provider_denial_is_safe_and_consumes_state(self):
        initial = await hf.connect("https://console.example")
        state = parse_qs(urlsplit(initial["authorization_url"]).query)["state"][0]
        result = await hf.complete(None, state, issuer=hf.ISSUER, error="secret-error-description")
        self.assertEqual(result["error"], "authorization_failed")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(self.provider.exchanges, [])
        self.assertEqual((await hf.complete("again", state, issuer=hf.ISSUER))["error"], "invalid_state")

    async def test_canceled_waiter_does_not_cancel_pending_sign_in(self):
        task = asyncio.create_task(hf.connect("https://console.example"))
        while not hf._pending or not hf._pending.state:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertIsNotNone(hf._pending)
        state = hf._pending.state
        self.assertTrue((await hf.complete("private-code", state, issuer=hf.ISSUER))["connected"])

    async def test_disconnect_cancels_pending_runner_and_invalidates_callbacks(self):
        initial = await hf.connect("https://console.example")
        state = parse_qs(urlsplit(initial["authorization_url"]).query)["state"][0]
        self.assertEqual((await hf.disconnect())["status"], "disconnected")
        self.assertIsNone(hf._pending)
        self.assertTrue(self.provider.servers[0].cleaned)
        self.assertEqual(len(set(self.provider.servers[0].tasks)), 1)
        self.assertEqual((await hf.complete("private-code", state, issuer=hf.ISSUER))["error"], "invalid_state")
        self.assertEqual(self.provider.exchanges, [])
        self.assertFalse(hf._path().exists())

    async def test_disconnect_removes_only_its_connection_and_fences_cached_provider(self):
        await self.sign_in()
        provider = hf.auth()
        entries = connectors.load()
        entries["mcp"]["other"] = {"transport": "http", "url": "https://other.example"}
        connectors._save(entries)
        await hf.disconnect()
        self.assertIn("other", connectors.load()["mcp"])
        self.assertNotIn("higgsfield", connectors.load()["mcp"])
        self.assertFalse(hf._path().exists())
        with self.assertRaisesRegex(hf.HiggsfieldError, "reconnect_required"):
            await self.request(provider)
        with self.assertRaisesRegex(hf.HiggsfieldError, "reconnect_required"):
            hf.auth()

    async def test_stale_refresh_storage_cannot_restore_disconnected_or_replaced_tokens(self):
        await self.sign_in()
        stale = hf.auth()._storage
        old = hf._read()
        replacement = {**old, "connection_id": "new-owner-connection"}
        hf._write(replacement)
        with self.assertRaisesRegex(hf.HiggsfieldError, "reconnect_required"):
            await stale.set_tokens(hf.OAuthToken(access_token="stale-private-token", refresh_token="stale-refresh"))
        self.assertEqual(hf._read()["connection_id"], "new-owner-connection")
        self.assertNotIn("stale", hf._path().read_text())

    async def test_failed_reconnect_preserves_previous_credentials_and_connector(self):
        await self.sign_in()
        previous = hf._path().read_text()
        entries = connectors.load()
        self.provider.fail_list = True
        result = await hf.connect("https://console.example")
        self.assertEqual(result["error"], "connection_failed")
        self.assertEqual(hf._path().read_text(), previous)
        self.assertEqual(connectors.load(), entries)
        self.assertTrue(hf.status()["connected"])

    async def test_expired_without_refresh_and_corrupt_storage_require_reconnect(self):
        await self.sign_in()
        data = hf._read()
        data["tokens"]["refresh_token"] = None
        data["expires_at"] = time.time() - 1
        hf._write(data)
        self.assertEqual(hf.status()["status"], "reconnect_required")
        with self.assertRaisesRegex(hf.HiggsfieldError, "reconnect_required"):
            hf.auth()
        for corruption in ("not json", json.dumps({**data, "metadata": {**METADATA, "token_endpoint": "https://evil.example/token"}})):
            hf._path().write_text(corruption)
            with self.assertRaisesRegex(hf.HiggsfieldError, "reconnect_required"):
                hf.auth()

    async def test_invalid_callback_url_cannot_start_provider_requests(self):
        for url in ("http://public.example", "https://u:p@console.example", "https://console.example/path",
                    "https://console.example/?key=secret", "https://console.example/#secret", "https://console.example:bad",
                    "https://console.example\n", "https://console.example\\evil", None):
            with self.subTest(url=url):
                self.assertEqual((await hf.connect(url))["error"], "invalid_callback")
        self.assertEqual(self.provider.requests, [])

    async def test_provider_error_body_and_exception_logs_do_not_leak_tokens(self):
        self.provider.bad_token = True
        with self.assertLogs("mcp.client.auth.oauth2", level="ERROR") as logs:
            result = await self.sign_in()
        self.assertEqual(result["error"], "authorization_failed")
        self.assertNotIn("private-leak", json.dumps(result) + "\n".join(logs.output))
        self.assertFalse(hf._path().exists())

    async def test_metadata_hijack_and_cross_origin_token_redirect_cannot_send_secrets(self):
        self.provider.metadata["token_endpoint"] = "https://evil.example/token"
        self.assertEqual((await hf.connect("https://console.example"))["error"], "authorization_failed")
        self.assertEqual(self.provider.registrations, [])
        self.provider.metadata = dict(METADATA)
        self.provider.token_redirect = "https://evil.example/token?access_token=private"
        result = await self.sign_in()
        self.assertEqual(result["error"], "authorization_failed")
        self.assertFalse(any(r.url.host == "evil.example" for r in self.provider.requests))
        self.assertFalse(hf._path().exists())

    async def test_pending_flow_expires_and_cleans_up_on_same_task(self):
        with patch.object(hf, "SIGN_IN_SECONDS", 0.05):
            result = await hf.connect("https://console.example")
            self.assertEqual(result["status"], "awaiting_sign_in")
            pending = hf._pending
            outcome = await pending.result
        self.assertEqual(outcome["error"], "sign_in_expired")
        self.assertTrue(self.provider.servers[0].cleaned)
        self.assertIsNone(hf._pending)
        self.assertEqual(self.provider.exchanges, [])

    async def test_existing_probe_preserves_new_tokens_rotated_during_list_tools(self):
        await self.sign_in()
        previous_id = hf._read()["connection_id"]
        self.provider.block_list = asyncio.Event()
        self.provider.list_started.clear()
        probe = asyncio.create_task(hf.connect("https://console.example"))
        await asyncio.wait_for(self.provider.list_started.wait(), 2)
        shared = hf.auth()
        self.assertIs(hf._pending.provider, shared)
        shared.context.token_expiry_time = time.time() - 1
        self.assertEqual((await self.request(shared)).status_code, 200)
        rotated = hf._read()
        self.assertEqual(rotated["tokens"]["refresh_token"], "refresh-private-1")
        self.provider.block_list.set()
        self.assertTrue((await probe)["connected"])
        data = hf._read()
        self.assertEqual(data["tokens"], rotated["tokens"])
        self.assertEqual(data["connection_id"], previous_id)
        self.assertEqual(len(self.provider.refreshes), 1)
        self.assertEqual(len(self.provider.registrations), 1)
        self.assertEqual(len(self.provider.exchanges), 1)

    async def test_revoked_saved_connection_can_start_fresh_sign_in(self):
        await self.sign_in()
        previous_id = hf._read()["connection_id"]
        self.provider.valid_access = "access-private-new-login"
        initial = await hf.connect("https://console.example")
        self.assertEqual(initial["status"], "awaiting_sign_in")
        state = parse_qs(urlsplit(initial["authorization_url"]).query)["state"][0]
        self.assertTrue((await hf.complete("new-code", state, issuer=hf.ISSUER))["connected"])
        self.assertNotEqual(hf._read()["connection_id"], previous_id)
        self.assertEqual(hf._read()["tokens"]["access_token"], "access-private-new-login")
        self.assertEqual(len(self.provider.refreshes), 0)
        self.assertEqual(len(self.provider.registrations), 1)
        self.assertEqual(len(self.provider.exchanges), 2)
        self.assertTrue(all(server.cleaned for server in self.provider.servers))

    async def test_transport_wrapped_auth_error_can_still_reauthorize_revoked_connection(self):
        await self.sign_in()
        self.provider.valid_access = "access-private-after-revocation"
        self.provider.wrap_auth_error = True
        initial = await hf.connect("https://console.example")
        self.assertEqual(initial["status"], "awaiting_sign_in")
        state = parse_qs(urlsplit(initial["authorization_url"]).query)["state"][0]
        self.assertTrue((await hf.complete("new-code", state, issuer=hf.ISSUER))["connected"])
        self.assertNotIn("private transport wrapper", json.dumps(hf.status()))

    async def test_slow_discovery_returns_promptly_without_canceling_runner(self):
        self.provider.block_connect = asyncio.Event()
        with patch.object(hf, "RESPONSE_SECONDS", 0.01):
            result = await hf.connect("https://console.example")
        self.assertEqual(result["status"], "connecting")
        pending = hf._pending
        self.assertFalse(pending.task.done())
        self.provider.block_connect.set()
        repeated = await hf.connect("https://console.example")
        self.assertEqual(repeated["status"], "awaiting_sign_in")
        state = parse_qs(urlsplit(repeated["authorization_url"]).query)["state"][0]
        self.assertTrue((await hf.complete("owner-code", state, issuer=hf.ISSUER))["connected"])

    async def test_slow_tool_verification_callback_returns_promptly_and_finishes_later(self):
        self.provider.block_list = asyncio.Event()
        initial = await hf.connect("https://console.example")
        state = parse_qs(urlsplit(initial["authorization_url"]).query)["state"][0]
        with patch.object(hf, "RESPONSE_SECONDS", 0.01):
            result = await hf.complete("owner-code", state, issuer=hf.ISSUER)
        self.assertEqual(result["status"], "connecting")
        self.assertEqual(hf.status()["status"], "connecting")
        self.assertNotIn("authorization_url", await hf.connect("https://console.example"))
        pending = hf._pending
        self.provider.block_list.set()
        self.assertTrue((await pending.result)["connected"])
        self.assertTrue(hf.status()["connected"])

    async def test_stalled_cleanup_is_bounded_and_cancellation_cannot_orphan_pending(self):
        for canceled in (False, True):
            self.provider.block_cleanup = not canceled
            self.provider.cancel_cleanup = canceled
            with patch.object(hf, "CLEANUP_SECONDS", 0.01):
                result = await asyncio.wait_for(self.sign_in(), 2)
            self.assertTrue(result["connected"])
            self.assertIsNone(hf._pending)
            self.assertEqual(len(set(self.provider.servers[-1].tasks)), 1)
            await hf.disconnect()

    async def test_disconnect_during_stalled_cleanup_still_resolves_callback(self):
        initial = await hf.connect("https://console.example")
        state = parse_qs(urlsplit(initial["authorization_url"]).query)["state"][0]
        self.provider.block_cleanup = True
        callback = asyncio.create_task(hf.complete("owner-code", state, issuer=hf.ISSUER))
        while not self.provider.servers[-1].cleaned:
            await asyncio.sleep(0)
        await asyncio.wait_for(hf.disconnect(), 2)
        self.assertEqual((await asyncio.wait_for(callback, 2))["status"], "disconnected")
        self.assertIsNone(hf._pending)
        self.assertFalse(hf._path().exists())

    async def test_cancel_before_runner_starts_resolves_waiters(self):
        loop = asyncio.get_running_loop()
        pending = hf._Pending(1, "https://console.example" + hf.CALLBACK_PATH, hf._Storage(),
                              loop.create_future(), loop.create_future(), loop.create_future(), time.monotonic() + 60)
        pending.task = asyncio.create_task(hf._run(pending))
        hf._pending = pending
        await hf.disconnect()
        self.assertTrue(pending.ready.done())
        self.assertTrue(pending.result.done())
        self.assertIsNone(hf._pending)

    async def test_flow_cleanup_failure_cannot_leak_error_or_keep_log_context(self):
        async def broken_flow(self, request):
            try:
                yield request
            finally:
                raise RuntimeError("access-private-close-error")
        provider = hf._Provider(hf._Storage(), "https://console.example" + hf.CALLBACK_PATH)
        with patch.object(hf.OAuthClientProvider, "async_auth_flow", broken_flow):
            with self.assertRaisesRegex(hf.HiggsfieldError, "authorization_failed"):
                await self.request(provider)
        self.assertFalse(hf._protected_log.get())


if __name__ == "__main__":
    unittest.main()
