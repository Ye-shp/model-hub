"""Owner-only Higgsfield MCP OAuth. Credentials never enter connector summaries.

The SDK owns discovery, client registration, PKCE and refresh. Browser sign-in
runs in a separate task so its MCP session and cleanup keep task affinity.
"""
from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken
try:
    from mcp.shared.auth import AuthorizationCodeResult
except ImportError:  # MCP 1.x callbacks return (code, state).
    AuthorizationCodeResult = None

import connectors
import store

MCP_URL = "https://mcp.higgsfield.ai/mcp"
ISSUER = "https://clerk.higgsfield.ai"
CALLBACK_PATH = "/api/connections/higgsfield/callback"
SIGN_IN_SECONDS = 600
RESPONSE_SECONDS = 30
CLEANUP_SECONDS = 10
_guard = threading.RLock()
_pending = None
_cached = None
_generation = 0
_last_error = None
_protected_log = contextvars.ContextVar("higgsfield_oauth_log", default=False)


class HiggsfieldError(ValueError):
    """A fixed, public error code; never includes a provider response or URL."""


class _PrivateAuthLogs(logging.Filter):
    def filter(self, record):
        if _protected_log.get():
            record.msg = "Higgsfield OAuth request failed" if record.levelno >= logging.WARNING else "Higgsfield OAuth request"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


for _logger_name in ("mcp.client.auth.oauth2", "mcp.client.auth.utils"):
    logging.getLogger(_logger_name).addFilter(_PrivateAuthLogs())


def _path():
    return store.DATA / "higgsfield-oauth.json"


def _callback(public_url):
    try:
        parts = urlsplit(public_url)
        port = parts.port
        if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in ("", "/") or "\\" in public_url or any(ord(c) < 33 for c in public_url)
                or parts.scheme not in ("https", "http")
                or (parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"))
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        return urlunsplit((parts.scheme, parts.netloc, CALLBACK_PATH, "", ""))
    except (ValueError, TypeError, AttributeError):
        raise HiggsfieldError("invalid_callback") from None


def _official_url(url, *, issuer_only=False):
    try:
        p = urlsplit(str(url))
        hosts = {"clerk.higgsfield.ai"} if issuer_only else {"clerk.higgsfield.ai", "mcp.higgsfield.ai"}
        if p.scheme != "https" or p.hostname not in hosts or p.port not in (None, 443) or p.username or p.password or p.fragment:
            raise ValueError
    except (ValueError, TypeError):
        raise HiggsfieldError("authorization_failed") from None


def _metadata(data):
    metadata = OAuthMetadata.model_validate(data)
    if str(metadata.issuer) != ISSUER:
        raise HiggsfieldError("authorization_failed")
    for field in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        value = getattr(metadata, field, None)
        if value:
            _official_url(value, issuer_only=True)
    if "S256" not in (metadata.code_challenge_methods_supported or []):
        raise HiggsfieldError("authorization_failed")
    return metadata


def _read():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("resource") != MCP_URL or not data.get("connection_id"):
            return None
        _callback(data["redirect_uri"].removesuffix(CALLBACK_PATH))
        _metadata(data["metadata"])
        info = OAuthClientInformationFull.model_validate(data["client_info"])
        if getattr(info, "issuer", ISSUER) not in (None, ISSUER):
            return None
        token = OAuthToken.model_validate(data["tokens"])
        if not token.access_token or not info.client_id:
            return None
        expiry = data.get("expires_at")
        if expiry is not None and (not isinstance(expiry, (int, float)) or not math.isfinite(expiry)):
            return None
        if token.expires_in is not None and expiry is None:
            return None
        if not isinstance(data.get("revision"), int):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write(data):
    store.DATA.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".higgsfield-", suffix=".tmp", dir=store.DATA)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _path())
        os.chmod(_path(), 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _public(connected=False, status="disconnected", error=None, tools=None, **extra):
    result = {"connected": connected, "status": status, "tools": list(tools or [])}
    if error:
        result["error"] = error
    result.update(extra)
    return result


def status():
    with _guard:
        if _pending and not _pending.task.done():
            return _public(status="awaiting_sign_in" if _pending.state and not _pending.consumed else "connecting")
        data = _read()
        entry = connectors.load()["mcp"].get("higgsfield") or {}
        if data and entry.get("auth") == "higgsfield" and entry.get("url") == MCP_URL:
            if _cached and _cached[0] == (str(_path()), data["connection_id"]) and _cached[1]._reconnect_required:
                return _public(status="reconnect_required", error="reconnect_required")
            token = data["tokens"]
            if data.get("expires_at") is not None and data["expires_at"] <= time.time() and not token.get("refresh_token"):
                return _public(status="reconnect_required", error="reconnect_required")
            return _public(True, "connected", tools=data.get("tools", []))
        if _last_error:
            return _public(status="error", error=_last_error)
        return _public()


class _Storage:
    """Stage new sign-ins in memory; fence live refresh writes by connection/revision."""
    def __init__(self, data=None, *, live=False):
        self.data = json.loads(json.dumps(data or {}))
        self.live = live

    async def get_tokens(self):
        value = self.data.get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def get_client_info(self):
        value = self.data.get("client_info")
        return OAuthClientInformationFull.model_validate(value) if value else None

    def _update(self, values):
        with _guard:
            if self.live:
                current = _read()
                if not current or (current["connection_id"], current["revision"]) != (self.data.get("connection_id"), self.data.get("revision")):
                    raise HiggsfieldError("reconnect_required")
                current.update(values)
                current["revision"] += 1
                _write(current)
                self.data = current
            else:
                self.data.update(values)

    async def set_tokens(self, tokens):
        if not tokens.access_token or (tokens.expires_in is not None and tokens.expires_in < 0):
            raise HiggsfieldError("authorization_failed")
        self._update({"tokens": tokens.model_dump(mode="json"),
                      "expires_at": time.time() + tokens.expires_in if tokens.expires_in is not None else None})

    async def set_client_info(self, client_info):
        self._update({"client_info": client_info.model_dump(mode="json")})


class _Provider(OAuthClientProvider):
    """Restore absolute expiry and AS metadata omitted by the SDK storage protocol."""
    def __init__(self, storage, redirect_uri, redirect_handler=None, callback_handler=None):
        self._storage = storage
        self._invalid = False
        self._reconnect_required = False
        super().__init__(server_url=MCP_URL,
                         client_metadata=OAuthClientMetadata(
                             redirect_uris=[redirect_uri], client_name="Model Hub Cowork",
                             application_type="native" if urlsplit(redirect_uri).hostname in ("localhost", "127.0.0.1", "::1") else "web",
                             token_endpoint_auth_method="none", response_types=["code"],
                             grant_types=["authorization_code", "refresh_token"], scope="openid email offline_access"),
                         storage=storage, redirect_handler=redirect_handler or self._no_redirect,
                         callback_handler=callback_handler or self._no_callback)

    async def _no_redirect(self, url):
        self._reconnect_required = True
        raise HiggsfieldError("reconnect_required")

    async def _no_callback(self):
        self._reconnect_required = True
        raise HiggsfieldError("reconnect_required")

    async def _initialize(self):
        await super()._initialize()
        data = self._storage.data
        if data.get("metadata"):
            self.context.oauth_metadata = _metadata(data["metadata"])
            self.context.auth_server_url = ISSUER
        self.context.token_expiry_time = data.get("expires_at")

    def _select_authorization_server(self, advertised):
        if ISSUER not in advertised:
            raise HiggsfieldError("authorization_failed")
        return ISSUER

    async def async_auth_flow(self, request):
        if self._invalid:
            raise HiggsfieldError("reconnect_required")
        flow = super().async_auth_flow(request)
        marker = _protected_log.set(True)
        try:
            outgoing = await flow.__anext__()
            while True:
                if self._invalid:
                    raise HiggsfieldError("reconnect_required")
                _official_url(outgoing.url)
                response = yield outgoing
                if self._invalid:
                    raise HiggsfieldError("reconnect_required")
                if outgoing.url.host == "clerk.higgsfield.ai" and ".well-known/" in outgoing.url.path and response.status_code == 200:
                    _metadata(json.loads(await response.aread()))
                outgoing = await flow.asend(response)
        except StopAsyncIteration:
            return
        except Exception as exc:
            code = str(exc) if isinstance(exc, HiggsfieldError) else "authorization_failed"
            raise HiggsfieldError(code) from None
        finally:
            try:
                await flow.aclose()
            except Exception:
                raise HiggsfieldError("authorization_failed") from None
            finally:
                _protected_log.reset(marker)


def auth():
    """Noninteractive, shared SDK auth for owner tasks; never prompts inside a tool."""
    global _cached
    with _guard:
        data = _read()
        if not data:
            raise HiggsfieldError("reconnect_required")
        if data.get("expires_at") is not None and data["expires_at"] <= time.time() and not data["tokens"].get("refresh_token"):
            raise HiggsfieldError("reconnect_required")
        cache_key = (str(_path()), data["connection_id"])
        if not _cached or _cached[0] != cache_key:
            if _cached:
                _cached[1]._invalid = True
            _cached = (cache_key, _Provider(_Storage(data, live=True), data["redirect_uri"]))
        if _cached[1]._reconnect_required:
            raise HiggsfieldError("reconnect_required")
        return _cached[1]


def _make_server(provider):
    from agents.mcp import MCPServerStreamableHttp
    return MCPServerStreamableHttp({"url": MCP_URL, "auth": provider, "timeout": SIGN_IN_SECONDS},
                                   name="higgsfield", cache_tools_list=True,
                                   client_session_timeout_seconds=SIGN_IN_SECONDS)


async def _cleanup_server(server):
    # Never use shield/a separate task: the MCP task group belongs to this task.
    try:
        async with asyncio.timeout(CLEANUP_SECONDS):
            await server.cleanup()
    except Exception:
        pass


@dataclass
class _Pending:
    generation: int
    redirect_uri: str
    storage: _Storage
    ready: asyncio.Future
    callback: asyncio.Future
    result: asyncio.Future
    deadline: float
    existing: bool = False
    state: str | None = None
    consumed: bool = False
    task: asyncio.Task | None = None
    provider: _Provider | None = None


async def _run(pending):
    global _pending, _cached, _last_error
    server = None
    result = _public(status="error", error="connection_failed")

    async def redirect(url):
        _official_url(url, issuer_only=True)
        parameters = parse_qs(urlsplit(url).query)
        values = parameters.get("state", [])
        if (len(values) != 1 or len(values[0]) < 32
                or parameters.get("redirect_uri") != [pending.redirect_uri]):
            raise HiggsfieldError("authorization_failed")
        pending.state = values[0]
        if not pending.ready.done():
            pending.ready.set_result(_public(status="awaiting_sign_in", authorization_url=url))

    async def callback():
        return await pending.callback

    try:
        async with asyncio.timeout(max(0, pending.deadline - time.monotonic())):
            # Checking an existing account shares its provider/refresh lock. Never
            # stage old rotating refresh tokens in a second OAuth transaction.
            if pending.existing:
                try:
                    pending.provider = auth()
                except HiggsfieldError as exc:
                    if str(exc) != "reconnect_required":
                        raise
                if pending.provider:
                    server = _make_server(pending.provider)
                    try:
                        await server.connect()
                        tools = await server.list_tools()
                    except Exception:
                        # The transport's task group may wrap the safe auth error
                        # in ExceptionGroup/UserError. Consult our own flag, never
                        # provider exception text, before starting browser sign-in.
                        if not pending.provider._reconnect_required:
                            raise
                        await _cleanup_server(server)
                        server = None
                        pending.provider = None
            if pending.provider is None:
                pending.provider = _Provider(pending.storage, pending.redirect_uri, redirect, callback)
                server = _make_server(pending.provider)
                await server.connect()
                tools = await server.list_tools()
        names = [tool.name for tool in tools if isinstance(tool.name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool.name)]
        with _guard:
            if pending.generation != _generation or _pending is not pending:
                raise HiggsfieldError("reconnect_required")
            live = pending.provider._storage.live
            if live:
                # This storage belongs to the shared cached provider and follows
                # any refresh completed while list_tools was in progress.
                pending.provider._storage._update({"tools": names})
            else:
                metadata = _metadata(pending.provider.context.oauth_metadata.model_dump(mode="json"))
                data = {**pending.storage.data, "connection_id": uuid.uuid4().hex, "revision": 0,
                        "resource": MCP_URL, "redirect_uri": pending.redirect_uri,
                        "metadata": metadata.model_dump(mode="json"), "tools": names, "saved_at": store.now()}
                if not data.get("tokens") or not data.get("client_info"):
                    raise HiggsfieldError("authorization_failed")
                previous = _read()
                _write(data)
            entries = connectors.load()
            entries["mcp"]["higgsfield"] = {"transport": "http", "url": MCP_URL, "auth": "higgsfield", "saved_at": store.now()}
            try:
                connectors._save(entries)
            except Exception:
                if not live:
                    if previous:
                        _write(previous)
                    else:
                        _path().unlink(missing_ok=True)
                raise
            if not live:
                if _cached:
                    _cached[1]._invalid = True
                _cached = None
            _last_error = None
        result = _public(True, "connected", tools=names)
    except asyncio.CancelledError:
        result = _public(status="disconnected")
    except TimeoutError:
        result = _public(status="error", error="sign_in_expired")
    except Exception as exc:
        code = str(exc) if isinstance(exc, HiggsfieldError) else "connection_failed"
        result = _public(status="error", error=code)
    finally:
        try:
            if server:
                await _cleanup_server(server)
        except asyncio.CancelledError:
            pass
        finally:
            with _guard:
                if pending.generation != _generation:
                    result = _public()
                if _pending is pending:
                    _pending = None
                    _last_error = result.get("error")
            for future in (pending.ready, pending.result):
                if not future.done():
                    future.set_result(result)


async def _cancel_pending():
    global _pending
    pending = _pending
    if pending and pending.task and not pending.task.done():
        pending.task.cancel()
        try:
            await pending.task
        except asyncio.CancelledError:
            # A task canceled before its first instruction has no finally block.
            pass
        with _guard:
            if _pending is pending:
                _pending = None
            for future in (pending.ready, pending.result):
                if not future.done():
                    future.set_result(_public())


async def _wait_response(future):
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=RESPONSE_SECONDS)
    except TimeoutError:
        return _public(status="connecting")


async def connect(public_url):
    global _pending, _generation, _last_error
    try:
        redirect_uri = _callback(public_url)
    except HiggsfieldError:
        return _public(status="error", error="invalid_callback")
    with _guard:
        if _pending and not _pending.task.done():
            # Repeated clicks share the pending flow rather than racing new clients.
            ready = _pending.ready
            if _pending.consumed:
                return _public(status="connecting")
        else:
            _generation += 1
            _last_error = None
            previous = _read()
            if not previous or previous["redirect_uri"] != redirect_uri:
                previous = None
            loop = asyncio.get_running_loop()
            # Reuse registration only. Old tokens are owned by the shared live
            # provider, and must never be refreshed by a staged sign-in runner.
            seed = {key: previous[key] for key in ("client_info", "metadata")} if previous else None
            pending = _Pending(_generation, redirect_uri, _Storage(seed), loop.create_future(),
                               loop.create_future(), loop.create_future(), time.monotonic() + SIGN_IN_SECONDS,
                               existing=previous is not None)
            _pending = pending
            pending.task = asyncio.create_task(_run(pending), name="higgsfield-sign-in")
            ready = pending.ready
    # Caller cancellation must not tear down the browser's pending session.
    return await _wait_response(ready)


async def complete(code, state, issuer=None, error=None):
    with _guard:
        pending = _pending
        if (not pending or not pending.state or pending.consumed or not isinstance(state, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", state)
                or not hmac.compare_digest(state, pending.state) or time.monotonic() >= pending.deadline):
            return _public(status="error", error="invalid_state")
        pending.consumed = True
        metadata = pending.provider.context.oauth_metadata
        requires_issuer = getattr(metadata, "authorization_response_iss_parameter_supported", False)
        if issuer != ISSUER and (issuer is not None or requires_issuer):
            pending.callback.set_exception(HiggsfieldError("authorization_failed"))
        elif error or not isinstance(code, str) or not code or len(code) > 8192:
            pending.callback.set_exception(HiggsfieldError("authorization_failed"))
        else:
            value = AuthorizationCodeResult(code=code, state=state, iss=issuer) if AuthorizationCodeResult else (code, state)
            pending.callback.set_result(value)
    return await _wait_response(pending.result)


async def disconnect():
    global _generation, _cached, _last_error
    with _guard:
        _generation += 1
        if _cached:
            _cached[1]._invalid = True
        _cached = None
        _last_error = None
        entries = connectors.load()
        entry = entries["mcp"].get("higgsfield") or {}
        if entry.get("auth") == "higgsfield":
            entries["mcp"].pop("higgsfield", None)
            connectors._save(entries)
        _path().unlink(missing_ok=True)
    await _cancel_pending()
    return _public()
