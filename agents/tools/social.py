"""Free social lookups and posting for Qwen Cowork. Runs in the tools virtualenv as the controller, so the owner's
sign-ins (environment variables set by toolbox.social_env) never reach the chat sandbox.

    python social.py <command> '<json args>'      -> prints one JSON line: {"ok": true, ...} or {"ok": false, "error": ...}

Commands: x_search, x_trends, x_user, x_post, x_thread, reddit_thread, post_details, instagram_profile,
instagram_publish, tiktok_profile, google_trends.
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
    try:  # TikTok refuses plain requests from servers; pass as Chrome
        from yt_dlp.networking.impersonate import ImpersonateTarget
        options["impersonate"] = ImpersonateTarget.from_str("chrome")
    except Exception:
        pass
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


# ---------------------------------------------------------------------------------------------
# One post with its comments: an X thread, a Reddit thread, a TikTok/Reel/Short (used by study_link)
# ---------------------------------------------------------------------------------------------
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36")


def web_get(url: str, timeout: int = 25, accept: str = "*/*", tries: int = 3) -> tuple[int, str, str]:
    """(status, final url, body) as a real Chrome when curl_cffi is available (Reddit/TikTok refuse plain clients).
    Rate limits (429) are retried a couple of times with a short wait."""
    for attempt in range(tries):
        status, final, body = _web_get_once(url, timeout, accept)
        if status != 429 or attempt == tries - 1:
            return status, final, body
        time.sleep(4 * (attempt + 1))
    return status, final, body


def _web_get_once(url: str, timeout: int, accept: str) -> tuple[int, str, str]:
    headers = {"Accept": accept, "Accept-Language": "en-US,en;q=0.9"}
    try:
        from curl_cffi import requests as curl
        response = curl.get(url, impersonate="chrome", timeout=timeout, headers=headers, allow_redirects=True)
        return response.status_code, str(response.url), response.text
    except ImportError:
        pass
    request = urllib.request.Request(url, headers={**headers, "User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.geturl(), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, url, error.read().decode("utf-8", "replace")


def _clean_html(text: str) -> str:
    import html
    import re
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _x_row(t) -> dict:
    videos = getattr(getattr(t, "media", None), "videos", None) or []
    animated = getattr(getattr(t, "media", None), "animated", None) or []
    row = {"id": t.id, "url": t.url, "date": t.date.isoformat() if t.date else None,
           "user": t.user.username if t.user else None, "followers": getattr(t.user, "followersCount", None),
           "text": (t.rawContent or "")[:6000], "likes": t.likeCount, "reposts": t.retweetCount, "replies": t.replyCount,
           "quotes": t.quoteCount, "views": t.viewCount, "has_video": bool(videos or animated)}
    links = [getattr(link, "url", None) for link in (getattr(t, "links", None) or [])]
    if any(links):
        row["links"] = [x for x in links if x][:6]
    if getattr(t, "quotedTweet", None):
        quoted = t.quotedTweet
        row["quoted"] = {"user": quoted.user.username if quoted.user else None, "text": (quoted.rawContent or "")[:2000],
                         "url": quoted.url}
    return row


async def x_thread(url: str, replies: int = 40) -> dict:
    """An X post, the author's whole thread around it, and the most-liked replies from other people."""
    import re
    match = re.search(r"/status(?:es)?/(\d+)", url)
    if not match:
        raise ValueError("Not an X post link (expected …/status/<id>)")
    api = await x_api()
    tweet = await api.tweet_details(int(match.group(1)))
    if not tweet:
        raise LookupError("X returned nothing for this post (deleted, private, or the X sign-in expired)")
    root_id = getattr(tweet, "conversationId", None) or tweet.id
    root = tweet if root_id == tweet.id else (await api.tweet_details(root_id) or tweet)
    author = root.user.username if root.user else None
    chain, others, seen = [root], [], {root.id}
    thread_ids = {root.id}
    collected = []
    async for reply in api.tweet_replies(root.id, limit=max(10, min(replies * 3, 200))):
        collected.append(reply)
    for reply in sorted(collected, key=lambda r: r.date or 0):
        if reply.id in seen:
            continue
        seen.add(reply.id)
        mine = reply.user and reply.user.username == author
        if mine and (getattr(reply, "inReplyToTweetId", None) in thread_ids or getattr(reply, "inReplyToTweetId", None) is None):
            chain.append(reply)
            thread_ids.add(reply.id)
        elif not mine:
            others.append(reply)
    if tweet.id not in thread_ids and tweet.id != root.id:
        chain.append(tweet)
    others.sort(key=lambda r: (r.likeCount or 0), reverse=True)
    return {"platform": "x", "url": root.url, "author": author,
            "author_followers": getattr(root.user, "followersCount", None), "thread": [_x_row(t) for t in chain],
            "comments": [{"user": r.user.username if r.user else None, "text": (r.rawContent or "")[:1500],
                          "likes": r.likeCount, "replies": r.replyCount, "url": r.url} for r in others[:max(1, min(replies, 100))]],
            "has_video": any(_x_row(t)["has_video"] for t in chain)}


def _reddit_ref(url: str) -> tuple[str, str, str]:
    """(subreddit or "", post id, canonical url), following share links like reddit.com/r/x/s/abc or redd.it/abc."""
    import re
    pattern = r"(?:/r/([^/]+))?/comments/([A-Za-z0-9]+)"
    match = re.search(pattern, url)
    if not match:
        status, final, body = web_get(url, accept="text/html")
        match = re.search(pattern, final) or re.search(pattern, body or "")
    if not match:
        raise ValueError("Couldn't find the Reddit thread in that link")
    sub, post_id = match.group(1) or "", match.group(2)
    return sub, post_id, (f"https://www.reddit.com/r/{sub}/comments/{post_id}/" if sub else f"https://www.reddit.com/comments/{post_id}/")


def _reddit_json(sub: str, post_id: str, limit: int) -> dict | None:
    status, _, body = web_get(f"https://www.reddit.com/comments/{post_id}.json?limit={limit}&sort=top&raw_json=1",
                              accept="application/json")
    if status != 200:
        return None
    try:
        listing = json.loads(body)
        post = listing[0]["data"]["children"][0]["data"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    comments = []

    def walk(children, depth):
        for child in children or []:
            data = child.get("data") or {}
            if child.get("kind") != "t1" or data.get("author") in (None, "[deleted]") or data.get("body") in (None, "[removed]", "[deleted]"):
                continue
            comments.append({"user": data.get("author"), "text": data["body"][:1500], "likes": data.get("score"),
                             "depth": depth, "url": "https://www.reddit.com" + data.get("permalink", "")})
            replies = data.get("replies")
            if isinstance(replies, dict) and depth < 3:
                walk(replies.get("data", {}).get("children"), depth + 1)

    walk(listing[1]["data"]["children"] if len(listing) > 1 else [], 0)
    return {"title": post.get("title"), "author": post.get("author"), "subreddit": post.get("subreddit"),
            "text": (post.get("selftext") or "")[:12000], "link": post.get("url_overridden_by_dest") or None,
            "score": post.get("score"), "upvote_ratio": post.get("upvote_ratio"), "num_comments": post.get("num_comments"),
            "is_video": bool(post.get("is_video")), "comments": comments}


def _reddit_keyless(sub: str, post_id: str) -> dict:
    """The post from its RSS feed and the top comments from the shreddit endpoint (Reddit's .json is often 403)."""
    import re
    import xml.etree.ElementTree as ET
    atom = "{http://www.w3.org/2005/Atom}"
    post = {"title": None, "author": None, "subreddit": sub, "text": "", "comments": []}
    feed_comments = []
    feeds = [f"https://www.reddit.com/comments/{post_id}/.rss"] + ([f"https://www.reddit.com/r/{sub}/comments/{post_id}/.rss"] if sub else [])
    for feed in feeds:
        status, _, body = web_get(feed, accept="application/atom+xml")
        try:
            entries = list(ET.fromstring(body).iter(f"{atom}entry")) if status == 200 else []
        except ET.ParseError:
            entries = []
        if entries:
            first = entries[0]
            post["title"] = (first.findtext(f"{atom}title") or "").strip()
            post["author"] = (first.findtext(f"{atom}author/{atom}name") or "").strip().removeprefix("/u/")
            body_text = _clean_html(first.findtext(f"{atom}content") or "")
            post["text"] = re.sub(r"\s*submitted by\s+/u/\S+.*$", "", body_text, flags=re.S)[:12000]
            category = first.find(f"{atom}category")
            if not sub and category is not None:
                sub = post["subreddit"] = (category.get("term") or "").strip()
            for entry in entries[1:]:  # the feed lists comments too (without scores): a fallback for the shreddit route
                text = _clean_html(entry.findtext(f"{atom}content") or "")
                if text:
                    feed_comments.append({"user": (entry.findtext(f"{atom}author/{atom}name") or "").strip().removeprefix("/u/"),
                                          "text": text[:1500], "likes": None, "depth": 0})
            break
    status, page = 0, ""
    if sub:
        status, _, page = web_get(f"https://www.reddit.com/svc/shreddit/comments/r/{sub}/t3_{post_id}?sort=top", accept="text/html")
    if status == 200:
        import html as html_lib
        total = re.search(r'total-comments="(\d+)"', page)
        post["num_comments"] = int(total.group(1)) if total else None
        for tag in re.finditer(r"<shreddit-comment(?=[\s>])[^>]*>", page):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag.group(0)))
            thing, author = attrs.get("thingId", ""), attrs.get("author", "")
            if not thing or author in ("", "[deleted]", "[removed]"):
                continue
            anchor = page.find(f'id="{thing}-post-rtjson-content"')
            if anchor == -1:
                continue
            window = page[anchor:anchor + 12000]
            stop = re.search(r'id="t1_[A-Za-z0-9]+-(?:comment|post)-rtjson-content"', window[40:])
            window = window[:stop.start() + 40] if stop else window
            text = _clean_html(" ".join(re.findall(r"<p[^>]*>(.*?)</p>", window, re.S)))
            if not text:
                continue
            try:
                score = int(attrs.get("score") or 0)
            except ValueError:
                score = 0
            post["comments"].append({"user": html_lib.unescape(author), "text": text[:1500], "likes": score,
                                     "depth": int(attrs.get("depth") or 0) if (attrs.get("depth") or "0").isdigit() else 0,
                                     "url": "https://www.reddit.com" + html_lib.unescape(attrs.get("permalink", ""))})
    if not post["comments"]:
        post["comments"] = feed_comments
    if not post["title"] and not post["comments"]:
        raise RuntimeError("Reddit refused every keyless route from this server (HTTP 403). Paste the post text instead.")
    return post


def reddit_thread(url: str, comments: int = 60) -> dict:
    """A Reddit post and its top comments (with replies), no API key."""
    sub, post_id, canonical = _reddit_ref(url)
    data = _reddit_json(sub, post_id, max(10, min(comments, 200))) or _reddit_keyless(sub, post_id)
    data["comments"] = sorted(data.get("comments") or [], key=lambda c: c.get("likes") or 0, reverse=True)[:max(1, min(comments, 200))]
    if not canonical.count("/r/") and data.get("subreddit"):
        canonical = f"https://www.reddit.com/r/{data['subreddit']}/comments/{post_id}/"
    return {"platform": "reddit", "url": canonical, **data}


def _tiktok_id(url: str) -> str | None:
    import re
    match = re.search(r"/(?:video|photo)/(\d{8,})", url)
    if not match and re.search(r"//(vm|vt)\.tiktok\.com/|tiktok\.com/t/", url):
        _, final, _ = web_get(url, accept="text/html")
        match = re.search(r"/(?:video|photo)/(\d{8,})", final)
    return match.group(1) if match else None


def _tiktok_comments(video_id: str, limit: int) -> list[dict]:
    rows, cursor = [], 0
    while len(rows) < limit:
        status, _, body = web_get(f"https://www.tiktok.com/api/comment/list/?aid=1988&aweme_id={video_id}&count=50"
                                  f"&cursor={cursor}", accept="application/json")
        if status != 200 or not body.strip().startswith("{"):
            break
        data = json.loads(body)
        for c in data.get("comments") or []:
            rows.append({"user": (c.get("user") or {}).get("unique_id"), "text": (c.get("text") or "")[:1500],
                         "likes": c.get("digg_count"), "replies": c.get("reply_comment_total")})
        if not data.get("has_more") or not data.get("comments"):
            break
        cursor = data.get("cursor") or cursor + 50
    return rows


def post_details(url: str, comments: int = 60) -> dict:
    """A video post's caption, stats and top comments (TikTok, Instagram, YouTube, X video…) without downloading it."""
    import yt_dlp
    from yt_dlp.extractor.common import InfoExtractor
    limit = max(1, min(comments, 200))

    def capped(self, *args, **kwargs):  # yt-dlp would page through every comment; stop at the limit
        if not self.get_param("getcomments"):
            return None
        generator = self._get_comments(*args, **kwargs)

        def extractor():
            found = []
            try:
                for comment in generator:
                    found.append(comment)
                    if len(found) >= limit:
                        break
            except Exception:
                pass
            return {"comments": found}
        return extractor

    InfoExtractor.extract_comments = capped
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 30,
               "getcomments": True, "extractor_args": {"youtube": {"max_comments": [str(limit), "all", "all", "all"],
                                                                   "comment_sort": ["top"]}}}
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        options["impersonate"] = ImpersonateTarget.from_str("chrome")
    except Exception:
        pass
    info, error = {}, None
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next((e for e in info["entries"] if e), {}) or info
    except Exception as failure:
        error = f"{type(failure).__name__}: {str(failure)[:300]}"
    rows = [{"user": c.get("author"), "text": (c.get("text") or "")[:1500], "likes": c.get("like_count"),
             "is_reply": c.get("parent") not in (None, "root")} for c in info.get("comments") or []]
    note = None
    if not rows and "tiktok.com" in url:
        video_id = _tiktok_id(info.get("webpage_url") or url)
        if video_id:
            try:
                rows = _tiktok_comments(video_id, limit)
            except Exception as failure:
                note = f"TikTok comments: {type(failure).__name__}"
    rows.sort(key=lambda c: c.get("likes") or 0, reverse=True)
    if not rows:
        note = note or ("Comments unavailable: Instagram only shows them to a signed-in account" if "instagram.com" in url
                        else "Comments unavailable from this server")
    keep = ("title", "description", "uploader", "uploader_id", "channel", "upload_date", "duration", "view_count", "like_count",
            "comment_count", "repost_count", "save_count", "track", "artist", "webpage_url", "extractor")
    if not info and not rows:
        raise RuntimeError(error or "Nothing could be read from this link")
    return {"post": {k: info[k] for k in keep if info.get(k) not in (None, "", [])}, "comments": rows[:limit],
            "comments_note": note, "error": error}


COMMANDS = {"x_search": x_search, "x_trends": x_trends, "x_user": x_user, "x_post": x_post,
            "instagram_profile": instagram_profile, "instagram_publish": instagram_publish,
            "tiktok_profile": tiktok_profile, "google_trends": google_trends,
            "x_thread": x_thread, "reddit_thread": reddit_thread, "post_details": post_details}


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
