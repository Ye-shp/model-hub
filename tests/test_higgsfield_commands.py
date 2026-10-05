"""Native owner sign-in commands and safe browser callbacks; no provider/model calls."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import console
import store
import workspace as ws
import hub
import escalate
import toolbox
import connectors
import telegram_bot
from fastapi.testclient import TestClient
import httpx2 as httpx
from integrations.openwebui_cowork import Pipe


KEY = "higgsfield-command-test-owner-key-1234567890"
AUTH = {"Authorization": "Bearer " + KEY}
SIGN_IN = "https://clerk.higgsfield.ai/oauth/authorize?client_id=cowork&state=pending-state&code_challenge=pkce"


class HiggsfieldCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT.parent, prefix="higgsfield-command-test-")
        self.folder = Path(self.temp.name).resolve()
        self.assertTrue(self.folder.is_relative_to(ROOT.parent.resolve()))
        self.provider = SimpleNamespace(
            status=Mock(return_value={"connected": False, "status": "disconnected"}),
            connect=AsyncMock(return_value={"connected": False, "status": "awaiting_sign_in", "authorization_url": SIGN_IN}),
            complete=AsyncMock(return_value={"connected": True, "status": "connected", "tools": ["models_list", "jobs_wait"]}),
            disconnect=AsyncMock(return_value={"connected": False, "status": "disconnected"}),
        )
        self.patches = [
            patch.object(store, "DATA", self.folder / "data"),
            patch.dict(os.environ, {"CONSOLE_URL": "https://console.example.test"}),
            patch.dict(sys.modules, {"higgsfield": self.provider}),
            patch.object(console.cf_access, "configured", return_value=False),
            patch.object(ws, "create_job", side_effect=AssertionError("sign-in must not create a job")),
            patch.object(hub, "async_client", side_effect=AssertionError("sign-in must not reach the model")),
        ]
        for item in self.patches:
            item.start()
        self.app = console.create_app(key=KEY, run_worker=False)
        self.client = TestClient(self.app)
        self.pipe = Pipe()
        self.pipe.valves.OWNER_KEY = KEY
        self.pipe.valves.ALLOWED_EMAILS = "friend@example.test"
        self.pipe._client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                                    base_url="http://testserver", headers=AUTH)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.assertTrue(self.folder.is_relative_to(ROOT.parent.resolve()))
        self.temp.cleanup()

    def chat(self, text, user=None):
        async def run():
            return "".join([part async for part in self.pipe.pipe(
                {"messages": [{"role": "user", "content": text}]},
                __user__=user or {"role": "admin", "id": "owner", "email": "owner@example.test"},
                __chat_id__="signin-chat")])
        return asyncio.run(run())

    def test_owner_command_starts_signin_without_task_or_model(self):
        result = self.chat("/connect higgsfield")
        self.provider.connect.assert_awaited_once_with("https://console.example.test")
        self.assertIn(f"[Sign in to Higgsfield]({SIGN_IN})", result)
        self.assertIn("existing account", result)
        self.assertEqual(ws.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 0)

    def test_connected_command_lists_tools_and_disconnect_intercepts(self):
        self.provider.connect.return_value = {"connected": True, "status": "connected", "tools": ["models_list", "jobs_wait"],
                                              "access_token": "must-not-be-shown", "refresh_token": "never-public"}
        result = self.chat("/CONNECT Higgsfield")
        self.assertIn("2 tools", result)
        self.assertIn("models_list", result)
        self.assertNotIn("must-not-be-shown", result)
        self.assertNotIn("never-public", result)
        self.assertEqual(self.chat("/connect higgsfield off"), "Higgsfield disconnected.")
        self.provider.disconnect.assert_awaited_once_with()

    def test_guest_and_invalid_command_never_start_signin(self):
        result = self.chat("/connect higgsfield", {"role": "user", "id": "friend", "email": "friend@example.test"})
        self.assertIn("Only the owner", result)
        self.provider.connect.assert_not_awaited()
        self.provider.disconnect.assert_not_awaited()
        result = self.chat("/connect higgsfield accidentally-pasted-secret")
        self.assertIn("Use `/connect higgsfield`", result)
        self.assertNotIn("accidentally-pasted-secret", result)
        self.provider.connect.assert_not_awaited()

    def test_provider_failure_does_not_echo_sensitive_exception(self):
        self.provider.connect.side_effect = ValueError("code=private-code token=private-token")
        result = self.chat("/connect higgsfield")
        self.assertIn("could not be reached", result)
        self.assertNotIn("private-code", result)
        self.assertNotIn("private-token", result)

    def test_later_task_does_not_receive_authorization_or_callback_urls(self):
        submitted = []

        async def call(client, method, path, **kwargs):
            if path == "/api/thread/active":
                return {"job": None, "question": None}
            if path == "/api/jobs":
                submitted.append(kwargs["json"]["task"])
                return {"id": "captured-task"}
            raise AssertionError(f"unexpected endpoint: {path}")

        async def follow(*args):
            yield "captured"

        self.pipe._call = call
        self.pipe._follow = follow

        async def run():
            body = {"messages": [
                {"role": "user", "content": "/connect higgsfield"},
                {"role": "assistant", "content": f"[Sign in to Higgsfield]({SIGN_IN})"},
                {"role": "user", "content": "Make a cover. My browser returned "
                    "https://console.example.test/api/connections/higgsfield/callback?code=private-code&state=private-state"},
            ]}
            return [part async for part in self.pipe.pipe(body, __user__={"role": "admin", "id": "owner"})]
        asyncio.run(run())
        self.assertEqual(len(submitted), 1)
        for secret in ("pending-state", "client_id", "code_challenge", "private-code", "private-state"):
            self.assertNotIn(secret, submitted[0])
        self.assertIn("Make a cover", submitted[0])

    def test_connections_shows_native_status_once_without_host_state_reads(self):
        self.provider.status.return_value = {"connected": True, "status": "connected", "tools": ["models_list", "jobs_wait"]}
        with patch.object(escalate, "status", return_value={}), \
                patch.object(toolbox, "connected", return_value={}), \
                patch.object(toolbox, "status", return_value={"state": "ready"}), \
                patch.object(connectors, "summary", return_value={
                    "mcp": {"higgsfield": {"transport": "http", "target": "mcp.higgsfield.ai"}}, "api": {}}), \
                patch.object(telegram_bot, "status", return_value={"connected": False}):
            result = self.chat("/connections")
        self.assertEqual(result.lower().count("higgsfield"), 1)
        self.assertIn("connected (2 tools)", result)
        self.provider.status.assert_called_once_with()

    def test_native_api_is_owner_only_and_uses_configured_public_url(self):
        self.assertEqual(self.client.get("/api/connections/higgsfield").status_code, 401)
        self.assertEqual(self.client.post("/api/connections/higgsfield", json={}).status_code, 401)
        self.assertEqual(self.client.get("/api/connections/higgsfield/callback-extra?state=private").status_code, 401)
        self.assertEqual(self.client.post("/api/connections/higgsfield/callback?state=private").status_code, 401)
        result = self.client.post("/api/connections/higgsfield", headers=AUTH, json={})
        self.assertEqual(result.status_code, 200)
        self.provider.connect.assert_awaited_once_with("https://console.example.test")
        self.assertEqual(result.json()["authorization_url"], SIGN_IN)
        self.assertEqual(self.client.post("/api/connections/higgsfield", headers=AUTH,
                                         json={"action": "disconnect"}).json()["connected"], False)
        self.provider.disconnect.assert_awaited_once_with()

    def test_browser_callback_forwards_parameters_without_bearer_or_echo(self):
        result = self.client.get("/api/connections/higgsfield/callback", params={
            "code": "private-authorization-code", "state": "private-pending-state", "iss": "https://clerk.higgsfield.ai",
            "error": "private-provider-error", "error_description": "secret-provider-detail"})
        self.assertEqual(result.status_code, 200)
        self.provider.complete.assert_awaited_once_with("private-authorization-code", "private-pending-state",
                                                        issuer="https://clerk.higgsfield.ai", error="private-provider-error")
        self.assertIn("Higgsfield connected", result.text)
        self.assertIn('href="/"', result.text)
        self.assertEqual(result.headers["referrer-policy"], "no-referrer")
        for secret in ("private-authorization-code", "private-pending-state", "private-provider-error", "secret-provider-detail"):
            self.assertNotIn(secret, result.text)

    def test_callback_rejection_and_pending_pages_are_safe(self):
        self.provider.complete.return_value = {"connected": False, "status": "error", "error": "invalid_state",
                                               "access_token": "private-token"}
        result = self.client.get("/api/connections/higgsfield/callback?state=unknown-state&code=secret-code")
        self.assertEqual(result.status_code, 400)
        self.assertIn("could not finish", result.text)
        for secret in ("unknown-state", "secret-code", "private-token", "invalid_state"):
            self.assertNotIn(secret, result.text)
        self.provider.complete.return_value = {"connected": False, "status": "connecting"}
        self.assertEqual(self.client.get("/api/connections/higgsfield/callback?state=pending").status_code, 202)
        self.provider.complete.side_effect = ValueError("secret provider exception")
        result = self.client.get("/api/connections/higgsfield/callback?state=pending")
        self.assertEqual(result.status_code, 400)
        self.assertNotIn("secret provider exception", result.text)

    def test_callback_query_is_removed_before_access_log_response_start(self):
        captured = []

        async def log_boundary(scope, receive, send):
            async def observe(message):
                if message["type"] == "http.response.start":
                    captured.append(scope.get("query_string"))
                await send(message)
            await self.app(scope, receive, observe)

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=log_boundary), base_url="http://testserver") as client:
                await client.get("/api/connections/higgsfield/callback?code=private-code&state=private-state")
                await client.get("/api/connections/higgsfield/callback?code=private-code", headers={"Host": "wrong.example"})
                await client.get("/api/connections/higgsfield?ordinary=value", headers=AUTH)
        asyncio.run(run())
        self.assertEqual(captured, [b"", b"", b"ordinary=value"])


if __name__ == "__main__":
    unittest.main()
