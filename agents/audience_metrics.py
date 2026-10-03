"""Read first-party audience counts for the owner's connected accounts.

No scraping or publishing happens here. TikTok Display API verifies video ownership;
Instagram observations require a matching media owner. Analytics access tokens stay
in headers; OAuth renewal secrets stay in form bodies. Redirects are refused, and
provider bodies/URLs are never included in errors.

API references:
https://developers.tiktok.com/docs/en/tiktok-api-v2-video-query
https://developers.tiktok.com/docs/en/tiktok-api-v2-get-user-info
https://developers.tiktok.com/docs/en/oauth-user-access-token-management
https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/insights/
https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/igmedia.py
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import toolbox

METRICS = ("views", "likes", "comments", "shares", "saves", "reach")
_NUMERIC_ID = re.compile(r"[0-9]{1,40}\Z")
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_.-]{1,200}\Z")
_OAUTH_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"
_REFRESH_FIELDS = ("refresh_token", "client_key", "client_secret")
_ERROR_MESSAGES = {
    "credentials_missing": "Account analytics are not connected.",
    "account_mismatch": "The post account does not match the connected account.",
    "invalid_identifier": "The account or published post identifier is invalid.",
    "invalid_credentials": "The connected account credentials are invalid.",
    "credentials_expired": "The account token has expired or was revoked; reconnect the account.",
    "permission_missing": "The token lacks permission to read these analytics.",
    "ownership_unverified": "The API did not confirm that the post belongs to the connected account.",
    "publication_mismatch": "The published post ID does not match its recorded link.",
    "metric_unavailable": "The requested metric is unavailable for this media.",
    "rate_limited": "The analytics API rate limit was reached.",
    "provider_unavailable": "The analytics API is temporarily unavailable.",
    "provider_rejected": "The analytics API rejected the request.",
    "network_error": "The analytics API could not be reached.",
    "invalid_response": "The analytics API returned an invalid response.",
    "unsafe_endpoint": "The analytics endpoint configuration is invalid.",
    "platform_unsupported": "Automatic analytics are supported for TikTok and Instagram only.",
    "credentials_changed": "The account connection changed during token renewal; retry scheduled.",
    "refresh_busy": "Another worker is renewing the account token; retry scheduled.",
}


class MetricsError(RuntimeError):
    """Only fixed, credential-free messages can cross the collector boundary."""

    def __init__(self, code: str, retry_after_seconds: int | None = None):
        self.code = code if code in _ERROR_MESSAGES else "provider_rejected"
        self.retry_after_seconds = retry_after_seconds
        super().__init__(_ERROR_MESSAGES[self.code])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to a different resource or origin.
        return None


def _provider_error(status: int, payload: dict, retry_after: str | None = None) -> MetricsError:
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else error if isinstance(error, str) else None
    if not isinstance(code, (str, int)):
        code = None
    if status == 429 or code in {4, 17, 32, 613, "rate_limit_exceeded"}:
        delay = int(retry_after) if retry_after and retry_after.isdigit() else 300
        return MetricsError("rate_limited", min(max(delay, 60), 21600))
    if status >= 500:
        return MetricsError("provider_unavailable")
    if status == 401 or code in {190, "access_token_invalid", "access_token_expired", "invalid_grant", "invalid_token"}:
        return MetricsError("credentials_expired", 900)
    if code == "invalid_client":
        return MetricsError("invalid_credentials", 900)
    if status == 403 or code in {10, 200, "scope_not_authorized", "scope_permission_missed"}:
        return MetricsError("permission_missing", 21600)
    if code == 100:
        return MetricsError("metric_unavailable", 21600)
    return MetricsError("provider_rejected")


def _request_json(method: str, url: str, token: str | None, body: dict | None = None, *, form: dict | None = None) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"open.tiktokapis.com", "graph.instagram.com"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise MetricsError("unsafe_endpoint")
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" if form is not None else "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = urllib.parse.urlencode(form).encode() if form is not None else json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        # Even an error's message may echo a token. Inspect only known codes.
        try:
            payload = json.loads(error.read(65536))
        except (ValueError, OSError):
            payload = {}
        headers = error.headers or {}
        raise _provider_error(error.code, payload if isinstance(payload, dict) else {}, headers.get("Retry-After")) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise MetricsError("network_error") from None
    if len(raw) > 1024 * 1024:
        raise MetricsError("invalid_response")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise MetricsError("invalid_response") from None
    if not isinstance(payload, dict):
        raise MetricsError("invalid_response")
    error = payload.get("error")
    if error and (not isinstance(error, dict) or error.get("code") not in ("ok", None)):
        raise _provider_error(200, payload)
    return payload


def _request_oauth(form: dict) -> dict:
    # OAuth secrets belong in the POST body, never in URLs or diagnostic text.
    return _request_json("POST", _OAUTH_ENDPOINT, None, form=form)


def _safe_token(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 600 and not any(c in value for c in "\r\n\x00")


def _has_refresh(entry: dict) -> bool:
    present = [bool(entry.get(field)) for field in _REFRESH_FIELDS]
    if any(present) and (not all(present) or any(not _safe_token(entry.get(field)) for field in _REFRESH_FIELDS)):
        raise MetricsError("invalid_credentials", 900)
    return all(present)


def _expires_soon(entry: dict, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None:
            return True
        return (expires - now).total_seconds() <= 120
    except (KeyError, TypeError, AttributeError, ValueError):
        # The initially pasted token has no trusted expiry. Renew before first use.
        return True


@contextmanager
def _tiktok_refresh_lock():
    """Serialize rotating refresh grants across threads/processes, including restart."""
    path = toolbox._path().with_name("tiktok-refresh.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        deadline = time.monotonic() + 20
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise MetricsError("refresh_busy", 60) from None
                time.sleep(.05)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _prepare_tiktok(task: dict, credentials: dict, *, live: bool, oauth_request, persist_tokens,
                    force_for_token: str | None = None) -> dict:
    account = str(task["account"])
    _credential("tiktok", account, credentials)
    entry = dict(credentials["tiktok"])
    if not _has_refresh(entry):
        return entry
    # Injected credentials stay local unless the caller explicitly supplies persistence.
    with _tiktok_refresh_lock() if live else nullcontext():
        if live:
            current = toolbox.credentials()
            _credential("tiktok", account, current)
            entry = dict(current["tiktok"])
            if not _has_refresh(entry):
                return entry  # Owner intentionally reconnected in manual-token mode.
        now = datetime.now(timezone.utc)
        if not _expires_soon(entry, now) and force_for_token != entry["access_token"]:
            return entry
        payload = oauth_request({"grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                                 "client_key": entry["client_key"], "client_secret": entry["client_secret"]})
        if not isinstance(payload, dict):
            raise MetricsError("invalid_response")
        if payload.get("error"):
            raise _provider_error(200, payload)
        if str(payload.get("open_id") or "") != account:
            raise MetricsError("account_mismatch", 21600)
        expiry = payload.get("expires_in")
        if (not _safe_token(payload.get("access_token")) or not _safe_token(payload.get("refresh_token"))
                or not isinstance(payload.get("token_type"), str) or payload["token_type"].lower() != "bearer"
                or isinstance(expiry, bool) or not isinstance(expiry, int) or not 1 <= expiry <= 31536000):
            raise MetricsError("invalid_response")
        scopes = payload.get("scope")
        if not isinstance(scopes, str):
            raise MetricsError("invalid_response")
        expires_at = datetime.fromtimestamp(now.timestamp() + expiry, timezone.utc).isoformat(timespec="seconds")
        if persist_tokens is not None and not persist_tokens(entry, payload["access_token"], payload["refresh_token"], expires_at=expires_at):
            # A concurrent owner reconnect/disconnect is authoritative. Do not overwrite it.
            raise MetricsError("credentials_changed", 60)
        updated = {**entry, "access_token": payload["access_token"], "refresh_token": payload["refresh_token"], "expires_at": expires_at}
        if not {"user.info.basic", "video.list"}.issubset({scope.strip() for scope in scopes.split(",")}):
            # Keep a valid rotated refresh token even if the grant now lacks analytics permission.
            raise MetricsError("permission_missing", 21600)
        return updated


def _count(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int) and 0 <= value <= 10**15:
        return value
    return None


def _credential(platform: str, account: str, credentials: dict) -> tuple[str, str]:
    entry = credentials.get(platform)
    if not isinstance(entry, dict):
        raise MetricsError("credentials_missing", 900)
    identity = str(entry.get("account_id" if platform == "tiktok" else "user_id") or "")
    token = entry.get("access_token")
    if not _ACCOUNT_ID.fullmatch(identity) or not isinstance(token, str) or not token or any(c in token for c in "\r\n\x00"):
        raise MetricsError("invalid_credentials", 900)
    if identity != account:
        raise MetricsError("account_mismatch", 21600)
    return identity, token


def _tiktok(task: dict, credentials: dict, request) -> dict:
    account, token = _credential("tiktok", str(task["account"]), credentials)
    identity = request("GET", "https://open.tiktokapis.com/v2/user/info/?fields=open_id", token)
    if str(identity.get("data", {}).get("user", {}).get("open_id") or "") != account:
        raise MetricsError("account_mismatch", 21600)
    # Unlike a public scrape, this endpoint verifies ownership against the token.
    payload = request("POST", "https://open.tiktokapis.com/v2/video/query/?fields=id,view_count,like_count,comment_count,share_count",
                      token, {"filters": {"video_ids": [str(task["remote_id"])]}})
    rows = payload.get("data", {}).get("videos", [])
    video = next((row for row in rows if isinstance(row, dict) and str(row.get("id")) == str(task["remote_id"])), None)
    if video is None:
        raise MetricsError("ownership_unverified", 21600)
    metrics = {name: None for name in METRICS}
    warnings = ["saves: not exposed by TikTok Display API", "reach: not exposed by TikTok Display API"]
    for name, field in {"views": "view_count", "likes": "like_count", "comments": "comment_count", "shares": "share_count"}.items():
        metrics[name] = _count(video.get(field))
        if metrics[name] is None:
            warnings.append(f"{name}: not returned by the API")
    if all(metrics[name] is None for name in ("views", "likes", "comments", "shares")):
        raise MetricsError("metric_unavailable")
    return {"metrics": metrics, "source": "tiktok_display_api", "warnings": warnings}


def _instagram_base() -> str:
    # Honour the publisher's API version, while keeping its origin fixed.
    base = os.environ.get("IG_GRAPH_BASE", "https://graph.instagram.com/v23.0").rstrip("/")
    if not re.fullmatch(r"https://graph\.instagram\.com/v[0-9]{1,3}\.0", base):
        raise MetricsError("unsafe_endpoint")
    return base


def _insight_count(payload: dict, metric: str) -> int | None:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or row.get("name") != metric:
            continue
        total = row.get("total_value")
        if isinstance(total, dict):
            return _count(total.get("value"))
        values = row.get("values")
        if isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict):
            return _count(values[0].get("value"))
    # Empty data is unavailable, not a zero. Do not sum arbitrary time-series rows.
    return None


def _instagram_link_identity(url) -> tuple[str, str] | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"instagram.com", "www.instagram.com"}
                or parsed.username or parsed.password or parsed.port
                or not re.fullmatch(r"/(?:reel|p|tv)/[A-Za-z0-9_-]+/?", parsed.path)):
            return None
    except ValueError:
        return None
    return "instagram.com", parsed.path.rstrip("/")


def _instagram(task: dict, credentials: dict, request) -> dict:
    account, token = _credential("instagram", str(task["account"]), credentials)
    if not _NUMERIC_ID.fullmatch(account):
        raise MetricsError("invalid_identifier")
    base, remote_id = _instagram_base(), str(task["remote_id"])
    media = request("GET", f"{base}/{remote_id}?fields=id,owner,permalink,like_count,comments_count", token)
    owner = media.get("owner")
    owner_id = str(owner.get("id") or "") if isinstance(owner, dict) else ""
    if str(media.get("id") or "") != remote_id or owner_id != account:
        raise MetricsError("ownership_unverified", 21600)
    actual_link, recorded_link = _instagram_link_identity(media.get("permalink")), _instagram_link_identity(task.get("url"))
    if actual_link is None or actual_link != recorded_link:
        raise MetricsError("publication_mismatch", 21600)
    metrics = {name: None for name in METRICS}
    warnings = []
    for name, field in {"likes": "like_count", "comments": "comments_count"}.items():
        metrics[name] = _count(media.get(field))
        if metrics[name] is None:
            warnings.append(f"{name}: not returned by the API")
    # Query separately: one unsupported metric must not erase available counts.
    for name, field in {"views": "views", "reach": "reach", "shares": "shares", "saves": "saved"}.items():
        try:
            payload = request("GET", f"{base}/{remote_id}/insights?metric={field}", token)
            metrics[name] = _insight_count(payload, field)
            if metrics[name] is None:
                warnings.append(f"{name}: not returned by the API")
        except MetricsError as error:
            if error.code not in {"permission_missing", "metric_unavailable"}:
                raise
            warnings.append(f"{name}: {error.code}")
    if all(value is None for value in metrics.values()):
        raise MetricsError("metric_unavailable")
    return {"metrics": metrics, "source": "instagram_graph_api", "warnings": warnings}


def collect_sync(task: dict, *, credentials: dict | None = None, request=None, oauth_request=None, persist_tokens=None) -> dict:
    """Collect one owned post, with dependency injection for offline tests."""
    platform = task.get("platform")
    if platform not in {"tiktok", "instagram"}:
        raise MetricsError("platform_unsupported", 21600)
    if not _NUMERIC_ID.fullmatch(str(task.get("remote_id") or "")) or not _ACCOUNT_ID.fullmatch(str(task.get("account") or "")):
        raise MetricsError("invalid_identifier", 21600)
    live = credentials is None
    credentials = toolbox.credentials() if live else credentials
    request = request or _request_json
    if platform == "tiktok":
        persist_tokens = toolbox.rotate_tiktok_tokens if live and persist_tokens is None else persist_tokens
        entry = _prepare_tiktok(task, credentials, live=live, oauth_request=oauth_request or _request_oauth, persist_tokens=persist_tokens)
        try:
            result = _tiktok(task, {"tiktok": entry}, request)
        except MetricsError as error:
            if error.code != "credentials_expired" or not _has_refresh(entry):
                raise
            entry = _prepare_tiktok(task, {"tiktok": entry}, live=live, oauth_request=oauth_request or _request_oauth,
                                    persist_tokens=persist_tokens, force_for_token=entry["access_token"])
            result = _tiktok(task, {"tiktok": entry}, request)
    else:
        result = _instagram(task, credentials, request)
    result["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


async def collect(task: dict) -> dict:
    # Slow remote reads cannot block the controller or a chat's event loop.
    return await asyncio.to_thread(collect_sync, task)
