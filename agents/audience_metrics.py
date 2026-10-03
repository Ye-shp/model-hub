"""Read first-party audience counts for the owner's connected accounts.

No scraping or publishing happens here. TikTok Display API verifies video ownership;
Instagram observations require a matching media owner. Tokens stay in HTTP headers,
redirects are refused, and provider bodies/URLs are never included in errors.

API references:
https://developers.tiktok.com/docs/en/tiktok-api-v2-video-query
https://developers.tiktok.com/docs/en/tiktok-api-v2-get-user-info
https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/insights/
https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/igmedia.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import toolbox

METRICS = ("views", "likes", "comments", "shares", "saves", "reach")
_NUMERIC_ID = re.compile(r"[0-9]{1,40}\Z")
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_.-]{1,200}\Z")
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
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, (str, int)):
        code = None
    if status == 429 or code in {4, 17, 32, 613, "rate_limit_exceeded"}:
        delay = int(retry_after) if retry_after and retry_after.isdigit() else 300
        return MetricsError("rate_limited", min(max(delay, 60), 21600))
    if status >= 500:
        return MetricsError("provider_unavailable")
    if status == 401 or code in {190, "access_token_invalid", "access_token_expired"}:
        return MetricsError("credentials_expired", 21600)
    if status == 403 or code in {10, 200, "scope_not_authorized", "scope_permission_missed"}:
        return MetricsError("permission_missing", 21600)
    if code == 100:
        return MetricsError("metric_unavailable", 21600)
    return MetricsError("provider_rejected")


def _request_json(method: str, url: str, token: str, body: dict | None = None) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"open.tiktokapis.com", "graph.instagram.com"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise MetricsError("unsafe_endpoint")
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
    )
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
        raise MetricsError("credentials_missing", 21600)
    identity = str(entry.get("account_id" if platform == "tiktok" else "user_id") or "")
    token = entry.get("access_token")
    if not _ACCOUNT_ID.fullmatch(identity) or not isinstance(token, str) or not token or any(c in token for c in "\r\n\x00"):
        raise MetricsError("invalid_credentials", 21600)
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


def collect_sync(task: dict, *, credentials: dict | None = None, request=None) -> dict:
    """Collect one owned post, with dependency injection for offline tests."""
    platform = task.get("platform")
    if platform not in {"tiktok", "instagram"}:
        raise MetricsError("platform_unsupported", 21600)
    if not _NUMERIC_ID.fullmatch(str(task.get("remote_id") or "")) or not _ACCOUNT_ID.fullmatch(str(task.get("account") or "")):
        raise MetricsError("invalid_identifier", 21600)
    result = {"tiktok": _tiktok, "instagram": _instagram}[platform](
        task, toolbox.credentials() if credentials is None else credentials, request or _request_json,
    )
    result["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


async def collect(task: dict) -> dict:
    # Slow remote reads cannot block the controller or a chat's event loop.
    return await asyncio.to_thread(collect_sync, task)
