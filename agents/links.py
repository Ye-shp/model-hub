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


# Profile pages: (platform, label, host pattern, path pattern capturing the handle)
PROFILE_RULES = [
    ("tiktok", "TikTok profile", r"(^|\.)tiktok\.com$", r"^/@([\w.-]+)/?$"),
    ("youtube", "YouTube channel", r"(^|\.)youtube\.com$", r"^/(@[\w.-]+|channel/[\w-]+|c/[\w.-]+)(/(shorts|videos|featured|streams))?/?$"),
    ("instagram", "Instagram profile", r"(^|\.)instagram\.com$", r"^/([A-Za-z0-9._]{1,30})(/(reels|tagged))?/?$"),
    ("x", "X profile", r"(^|\.)(x|twitter)\.com$", r"^/([A-Za-z0-9_]{1,15})/?$"),
]
NOT_HANDLES = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "about", "legal", "developer",
               "home", "search", "i", "settings", "notifications", "messages", "hashtag", "compose", "login", "signup",
               "share", "intent", "tos", "privacy", "jobs", "shorts", "watch", "results", "feed", "discover", "tag"}


def _host_path(url: str) -> tuple[str, str]:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return "", "/"
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("mobile.")
    return host, parsed.path or "/"


def profile_of(url: str) -> tuple[str, str, str] | None:
    """(platform, handle, canonical profile url) for a creator's profile/channel link, else None."""
    host, path = _host_path(url)
    for platform, _, host_rule, path_rule in PROFILE_RULES:
        match = re.search(path_rule, path) if re.search(host_rule, host) else None
        if not match:
            continue
        handle = match.group(1).lstrip("@")
        if handle.lower() in NOT_HANDLES:
            return None
        canonical = {"tiktok": f"https://www.tiktok.com/@{handle}",
                     "instagram": f"https://www.instagram.com/{handle}/",
                     "x": f"https://x.com/{handle}",
                     "youtube": f"https://www.youtube.com/{match.group(1)}"}[platform]
        if platform == "youtube" and not match.group(1).startswith("@"):
            handle = match.group(1)  # channel/ID or c/name: keep the path form
        return platform, handle, canonical
    return None


def platform_of(url: str) -> tuple[str, str]:
    """(key, label) for a link, e.g. ("tiktok", "TikTok video"); ("web", "web page") when unrecognised.
    Creator profiles come back as ("profile", "TikTok profile") and so on."""
    profile = profile_of(url)
    if profile:
        return "profile", next(label for key, label, _, _ in PROFILE_RULES if key == profile[0])
    host, path = _host_path(url)
    if not host:
        return "web", "web page"
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
