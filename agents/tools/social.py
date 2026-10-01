"""Free social lookups and posting for Qwen Cowork. Runs in the tools virtualenv as the controller, so the owner's
sign-ins (environment variables set by toolbox.social_env) never reach the chat sandbox.

    python social.py <command> '<json args>'      -> prints one JSON line: {"ok": true, ...} or {"ok": false, "error": ...}

Commands: x_search, x_trends, x_user, x_post, instagram_profile, instagram_publish, tiktok_profile, google_trends.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from itertools import islice
from pathlib import Path

STATE = Path(os.environ.get("SOCIAL_STATE_DIR", "/tmp/social-state"))


def need(*names: str):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise PermissionError({"AUTH_TOKEN": "X isn't connected. The owner can send `/connect x <username> auth_token=… ct0=…` in a "
                                             "Cowork chat.",
                               "IG_ACCESS_TOKEN": "Instagram publishing isn't connected. The owner can send `/connect instagram "
                                                  "<user id> <access token>` in a Cowork chat."}.get(missing[0], f"Missing {missing[0]}"))


# ---------------------------------------------------------------------------------------------
# X (twscrape for reading, twikit for posting) — uses the owner's own X session cookies
# ---------------------------------------------------------------------------------------------
async def x_api():
    need("AUTH_TOKEN", "CT0")
    from twscrape import API
    STATE.mkdir(parents=True, exist_ok=True)
    api = API(str(STATE / "twscrape.db"))
    username = os.environ.get("X_USERNAME") or "owner"
    cookies = f"auth_token={os.environ['AUTH_TOKEN']}; ct0={os.environ['CT0']}"
    existing = {a.username: a for a in await api.pool.get_all()}
    if username in existing and existing[username].cookies.get("auth_token") != os.environ["AUTH_TOKEN"]:
        await api.pool.delete_accounts(username)
        existing.pop(username)
    if username not in existing:
        await api.pool.add_account(username, "-", f"{username}@invalid.local", "-", cookies=cookies)
    return api


def tweet_row(t) -> dict:
    return {"id": t.id, "url": t.url, "date": t.date.isoformat() if t.date else None, "user": t.user.username if t.user else None,
            "followers": getattr(t.user, "followersCount", None), "text": t.rawContent[:600], "likes": t.likeCount,
            "reposts": t.retweetCount, "replies": t.replyCount, "quotes": t.quoteCount, "views": t.viewCount,
            "media": [m.url for m in (getattr(t.media, "photos", []) or [])][:4] + [v.thumbnailUrl for v in (getattr(t.media, "videos", []) or [])][:2]
            if getattr(t, "media", None) else []}


async def x_search(query: str, limit: int = 30, mode: str = "Top") -> dict:
    api = await x_api()
    product = {"top": "Top", "latest": "Latest", "media": "Media"}.get(mode.lower(), "Top")
    rows = [tweet_row(t) async for t in api.search(query, limit=max(1, min(limit, 100)), kv={"product": product})]
    return {"query": query, "mode": product, "results": rows}


async def x_trends(category: str = "trending") -> dict:
    api = await x_api()
    category = category if category in {"trending", "news", "sport", "entertainment"} else "trending"
    rows = []
    async for trend in api.trends(category):
        rows.append({"name": trend.name, "context": (getattr(trend, "trend_metadata", None) and
                                                     getattr(trend.trend_metadata, "domain_context", None)),
                     "posts": getattr(getattr(trend, "trend_metadata", None), "meta_description", None)})
        if len(rows) >= 40:
            break
    return {"category": category, "trends": rows}


async def x_user(username: str, limit: int = 20) -> dict:
    api = await x_api()
    user = await api.user_by_login(username.lstrip("@"))
    if not user:
        raise LookupError(f"No X user @{username}")
    rows = [tweet_row(t) async for t in api.user_tweets(user.id, limit=max(1, min(limit, 100)))]
    return {"user": {"username": user.username, "name": user.displayname, "followers": user.followersCount,
                     "following": user.friendsCount, "posts": user.statusesCount, "bio": user.rawDescription[:400]},
            "posts": rows}


async def x_post(text: str, media: list[str] | None = None) -> dict:
    need("AUTH_TOKEN", "CT0")
    from twikit import Client
    client = Client("en-US")
    client.set_cookies({"auth_token": os.environ["AUTH_TOKEN"], "ct0": os.environ["CT0"]})
    media_ids = []
    for path in (media or [])[:4]:
        media_ids.append(await client.upload_media(path, wait_for_completion=path.lower().endswith((".mp4", ".mov"))))
    tweet = await client.create_tweet(text=text, media_ids=media_ids or None)
    username = os.environ.get("X_USERNAME", "i")
    return {"id": tweet.id, "url": f"https://x.com/{username}/status/{tweet.id}"}


# ---------------------------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------------------------
def instagram_profile(username: str, limit: int = 12) -> dict:
    import instaloader

    class NoWait(instaloader.RateController):
        def sleep(self, seconds):  # instaloader would otherwise wait out Instagram's rate limit for many minutes
            raise RuntimeError("Instagram is rate-limiting this server right now; try again later or use the phone")

    loader = instaloader.Instaloader(download_pictures=False, download_videos=False, download_video_thumbnails=False,
                                     save_metadata=False, compress_json=False, quiet=True, max_connection_attempts=1,
                                     request_timeout=20, rate_controller=lambda context: NoWait(context))
    profile = instaloader.Profile.from_username(loader.context, username.lstrip("@"))
    posts = []
    for post in islice(profile.get_posts(), max(1, min(limit, 40))):
        posts.append({"url": f"https://www.instagram.com/p/{post.shortcode}/", "date": post.date_utc.isoformat(),
                      "type": post.typename, "is_video": post.is_video, "views": post.video_view_count if post.is_video else None,
                      "likes": post.likes, "comments": post.comments, "caption": (post.caption or "")[:600],
                      "hashtags": post.caption_hashtags[:20]})
    return {"user": {"username": profile.username, "followers": profile.followers, "following": profile.followees,
                     "posts": profile.mediacount, "bio": (profile.biography or "")[:400], "verified": profile.is_verified},
            "posts": posts}


GRAPH = os.environ.get("IG_GRAPH_BASE", "https://graph.instagram.com/v23.0")
RUPLOAD = os.environ.get("IG_RUPLOAD_BASE", "https://rupload.facebook.com/ig-api-upload/v23.0")


def _graph(method: str, url: str, params: dict | None = None, data: bytes | None = None, headers: dict | None = None) -> dict:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, data=data if data is not None else (b"" if method == "POST" else None),
                                     method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")[:600]
        raise RuntimeError(f"Instagram API HTTP {error.code}: {body}") from None


def _container_ready(container: str, token: str, wait: int = 600):
    deadline = time.time() + wait
    while time.time() < deadline:
        state = _graph("GET", f"{GRAPH}/{container}", {"fields": "status_code,status", "access_token": token})
        code = state.get("status_code")
        if code == "FINISHED":
            return
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram couldn't process the media: {state.get('status')}")
        time.sleep(5)
    raise TimeoutError("Instagram is still processing the media")


def _upload_video(upload_url: str, path: str, token: str):
    data = Path(path).read_bytes()
    result = _graph("POST", upload_url, data=data,
                    headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))})
    if not result.get("success"):
        raise RuntimeError(f"Upload failed: {result}")


def instagram_publish(kind: str, caption: str, media: list[str], public_urls: list[str] | None = None) -> dict:
    """kind: reel (one video), image (one image, needs public_urls), carousel (2-10 items; images need public_urls),
    story (one video or image)."""
    need("IG_USER_ID", "IG_ACCESS_TOKEN")
    user, token = os.environ["IG_USER_ID"], os.environ["IG_ACCESS_TOKEN"]
    public_urls = public_urls or [None] * len(media)

    def item(path: str, url: str | None, extra: dict) -> str:
        is_video = path.lower().endswith((".mp4", ".mov", ".m4v"))
        if is_video:
            made = _graph("POST", f"{GRAPH}/{user}/media", {**extra, "media_type": extra.get("media_type", "REELS"),
                                                             "upload_type": "resumable", "access_token": token})
            _upload_video(made.get("uri") or f"{RUPLOAD}/{made['id']}", path, token)
        else:
            if not url:
                raise RuntimeError("Instagram needs images at a public web address; this hub can't serve one yet "
                                   "(it comes with the next hub image). Post a video/reel instead, or post from the phone.")
            made = _graph("POST", f"{GRAPH}/{user}/media", {**extra, "image_url": url, "access_token": token})
        _container_ready(made["id"], token)
        return made["id"]

    if kind == "carousel":
        if not 2 <= len(media) <= 10:
            raise ValueError("A carousel needs 2-10 items")
        children = [item(p, u, {"is_carousel_item": "true", **({"media_type": "VIDEO"} if p.lower().endswith((".mp4", ".mov")) else {})})
                    for p, u in zip(media, public_urls)]
        container = _graph("POST", f"{GRAPH}/{user}/media", {"media_type": "CAROUSEL", "children": ",".join(children),
                                                              "caption": caption, "access_token": token})["id"]
        _container_ready(container, token)
    elif kind == "story":
        extra = {"media_type": "STORIES"}
        container = item(media[0], public_urls[0], extra)
    elif kind in {"reel", "image"}:
        extra = {"caption": caption, **({"media_type": "REELS"} if kind == "reel" else {})}
        container = item(media[0], public_urls[0], extra)
    else:
        raise ValueError("kind must be reel, image, carousel or story")
    published = _graph("POST", f"{GRAPH}/{user}/media_publish", {"creation_id": container, "access_token": token})
    link = _graph("GET", f"{GRAPH}/{published['id']}", {"fields": "permalink", "access_token": token}).get("permalink")
    return {"id": published["id"], "url": link}


# ---------------------------------------------------------------------------------------------
# TikTok (public profile pages through yt-dlp) and Google Trends
# ---------------------------------------------------------------------------------------------
def tiktok_profile(username: str, limit: int = 15) -> dict:
    import yt_dlp
    url = f"https://www.tiktok.com/@{username.lstrip('@')}"
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "playlistend": max(1, min(limit, 40)),
               "ignoreerrors": True, "socket_timeout": 30}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    videos = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        videos.append({"url": entry.get("webpage_url"), "date": entry.get("upload_date"), "views": entry.get("view_count"),
                       "likes": entry.get("like_count"), "comments": entry.get("comment_count"),
                       "shares": entry.get("repost_count"), "duration": entry.get("duration"),
                       "sound": " - ".join(x for x in (entry.get("track"), entry.get("artist")) if x) or None,
                       "caption": (entry.get("description") or entry.get("title") or "")[:500]})
    if not videos:
        raise RuntimeError("TikTok returned nothing for this profile (TikTok often blocks data-center servers). "
                           "Use the phone (phone_collect) for TikTok research instead.")
    return {"user": username.lstrip("@"), "videos": videos}


def google_trends(keywords: list[str], timeframe: str = "today 1-m", geo: str = "") -> dict:
    from pytrends.request import TrendReq
    keywords = [k for k in keywords if k][:5]
    if not keywords:
        raise ValueError("Give 1-5 keywords")
    trends = TrendReq(hl="en-US", tz=300, timeout=(10, 30))  # pytrends' own retries break with urllib3 2
    trends.build_payload(keywords, timeframe=timeframe, geo=geo)
    over_time = trends.interest_over_time()
    series = {}
    if not over_time.empty:
        for keyword in keywords:
            if keyword in over_time:
                points = over_time[keyword]
                series[keyword] = {"latest": int(points.iloc[-1]), "peak": int(points.max()), "average": round(float(points.mean()), 1),
                                   "weekly": [int(v) for v in points.iloc[::max(1, len(points) // 12)]]}
    related = {}
    try:
        for keyword, tables in (trends.related_queries() or {}).items():
            related[keyword] = {kind: (table.head(10).to_dict("records") if table is not None else [])
                                for kind, table in (tables or {}).items()}
    except Exception as error:  # Google often refuses this part
        related = {"error": f"{type(error).__name__}"}
    return {"keywords": keywords, "timeframe": timeframe, "geo": geo or "worldwide", "interest": series, "related": related}


COMMANDS = {"x_search": x_search, "x_trends": x_trends, "x_user": x_user, "x_post": x_post,
            "instagram_profile": instagram_profile, "instagram_publish": instagram_publish,
            "tiktok_profile": tiktok_profile, "google_trends": google_trends}


def main():
    command, args = sys.argv[1], json.loads(sys.argv[2] if len(sys.argv) > 2 else "{}")
    try:
        function = COMMANDS[command]
        result = function(**args)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
    except Exception as error:
        message = str(error)
        for secret in (os.environ.get("AUTH_TOKEN"), os.environ.get("CT0"), os.environ.get("IG_ACCESS_TOKEN")):
            if secret:
                message = message.replace(secret, "[redacted]")
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {message[:900]}"}))


if __name__ == "__main__":
    main()
