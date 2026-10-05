"""The controller's Tor page reader; daemon files stay outside Cowork sandboxes."""
from __future__ import annotations

import asyncio
from functools import lru_cache
import importlib.util
import os
from pathlib import Path
from urllib.parse import urlsplit

import web


@lru_cache(maxsize=1)
def _fetcher():
    path = Path(__file__).resolve().parents[1] / "tools" / "tor" / "fetch.py"
    spec = importlib.util.spec_from_file_location("hub_tor_fetcher", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The Tor toolkit is missing from this deployment")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def read_page(url: str, offset: int = 0, max_chars: int = 12000) -> dict:
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or not parsed.hostname.lower().endswith(".onion")
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Use a plain http(s) .onion URL")
    if offset < 0:
        raise ValueError("offset must be nonnegative")
    # Hub mode has a private socket. Never fall back to the standalone TCP proxy.
    if not os.environ.get("TOR_SOCKS_SOCKET"):
        raise RuntimeError("The Hub Tor reader is unavailable; check the Tor service deployment")
    result = await asyncio.to_thread(_fetcher().fetch, url, timeout=60, rotate=False,
                                    http_only=True, max_bytes=web.MAX_BYTES)
    if not 200 <= result["status"] < 300:
        raise RuntimeError(f"The Tor page returned HTTP {result['status']}")
    final_url = result.get("final_url") or url
    source = result.get("text", "")
    title, text = await asyncio.to_thread(web._extract, source, final_url)
    if not text.strip():
        text = source
    max_chars = max(1000, min(max_chars, 20000))
    response = {"url": url, "final_url": final_url, "status": result["status"], "title": title,
                "content": text[offset:offset + max_chars], "total_characters": len(text)}
    if offset + max_chars < len(text):
        response["next_offset"] = offset + max_chars
    return response
