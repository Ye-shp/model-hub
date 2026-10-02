"""Which platform a link points to, and finding links in a message. Pure functions, no network."""
from __future__ import annotations

import re
from urllib.parse import urlparse

URL = re.compile(r"https?://[^\s<>\"'`]+", re.I)

# (platform key, label, host pattern, path pattern). First match wins.
RULES = [
    ("tiktok", "TikTok video", r"(^|\.)tiktok\.com$", r"/(@[^/]+/(video|photo)/\d+|t/|v/\d+)|^/[A-Za-z0-9]{6,}/?$"),
    ("tiktok", "TikTok video", r"^(vm|vt)\.tiktok\.com$", r""),
    ("instagram", "Instagram Reel", r"(^|\.)instagram\.com$", r"^/(reels?|tv)/"),
    ("instagram", "Instagram post", r"(^|\.)instagram\.com$", r"^/p/"),
    ("instagram", "Instagram story", r"(^|\.)instagram\.com$", r"^/stories/"),
    ("x", "X post", r"(^|\.)(x|twitter|fxtwitter|vxtwitter|fixupx)\.com$", r"/status(es)?/\d+"),
    ("reddit", "Reddit thread", r"(^|\.)reddit\.com$", r"/comments/|/r/[^/]+/s/"),
    ("reddit", "Reddit thread", r"^redd\.it$", r""),
    ("youtube", "YouTube Short", r"(^|\.)youtube\.com$", r"^/shorts/"),
    ("youtube", "YouTube video", r"(^|\.)youtube\.com$", r"^/(watch|live/)"),
    ("youtube", "YouTube video", r"^youtu\.be$", r""),
    ("linkedin", "LinkedIn post", r"(^|\.)linkedin\.com$", r"^/(posts|feed/update|pulse)/"),
    ("threads", "Threads post", r"(^|\.)threads\.(net|com)$", r"/post/"),
    ("facebook", "Facebook video", r"(^|\.)(facebook\.com|fb\.watch)$", r""),
]

VIDEO_PLATFORMS = {"tiktok", "instagram", "youtube", "facebook"}


def find_urls(text: str) -> list[str]:
    """Links in a message, in order, without duplicates or trailing punctuation."""
    found = []
    for match in URL.findall(text or ""):
        url = match.rstrip(".,;:!?)]}>*_")
        if url not in found:
            found.append(url)
    return found


def platform_of(url: str) -> tuple[str, str]:
    """(key, label) for a link, e.g. ("tiktok", "TikTok video"); ("web", "web page") when unrecognised."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return "web", "web page"
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("mobile.")
    path = parsed.path or "/"
    for key, label, host_rule, path_rule in RULES:
        if re.search(host_rule, host) and (not path_rule or re.search(path_rule, path)):
            return key, label
    if host.endswith(("tiktok.com", "instagram.com", "x.com", "twitter.com", "reddit.com", "youtube.com")):
        base = host.split(".")[-2]
        key = {"twitter": "x"}.get(base, base)
        return key, {"x": "X profile or page", "tiktok": "TikTok page", "instagram": "Instagram page",
                     "reddit": "Reddit page", "youtube": "YouTube page"}[key]
    return "web", "web page"


def describe(urls: list[str]) -> str:
    """'TikTok video', '2 links: X post, Reddit thread' …"""
    labels = [platform_of(u)[1] for u in urls]
    if len(labels) == 1:
        return labels[0]
    return f"{len(labels)} links: " + ", ".join(labels)
