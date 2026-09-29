"""Web search and page reading for the Cowork agent. No API key needed (DuckDuckGo/Bing via ddgs);
set BRAVE_API_KEY to use Brave Search instead."""
from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import os
import socket
from urllib.parse import urljoin, urlparse

try:
    import httpx2 as httpx
except ImportError:
    import httpx

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
MAX_BYTES = 8_000_000


async def search(query: str, max_results: int = 8) -> list[dict]:
    query, max_results = query.strip()[:400], max(1, min(max_results, 15))
    if not query:
        raise ValueError("Give a search query")
    brave = os.environ.get("BRAVE_API_KEY", "")
    if brave:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": max_results},
                                 headers={"X-Subscription-Token": brave, "Accept": "application/json"})
            r.raise_for_status()
            return [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": i.get("description", "")}
                    for i in r.json().get("web", {}).get("results", [])[:max_results]]

    def ddg():
        from ddgs import DDGS
        return [{"title": i.get("title", ""), "url": i.get("href", ""), "snippet": i.get("body", "")}
                for i in DDGS(timeout=20).text(query, max_results=max_results)]
    return await asyncio.to_thread(ddg)


def _public(host: str) -> None:
    """Refuse addresses on this machine or its private network (the hub's own services)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve {host}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError("Only public internet addresses can be fetched")


def _extract(html: str, url: str) -> tuple[str, str]:
    import trafilatura
    title = ""
    meta = trafilatura.extract_metadata(html)
    if meta and meta.title:
        title = meta.title
    text = trafilatura.extract(html, url=url, output_format="markdown", include_links=True, include_tables=True,
                               favor_recall=True) or ""
    if not text.strip():
        from html.parser import HTMLParser

        class Strip(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts, self.skip = [], 0

            def handle_starttag(self, tag, attrs):
                self.skip += tag in {"script", "style", "noscript"}

            def handle_endtag(self, tag):
                self.skip -= tag in {"script", "style", "noscript"} and self.skip > 0

            def handle_data(self, data):
                if not self.skip and data.strip():
                    self.parts.append(data.strip())
        stripper = Strip()
        stripper.feed(html)
        text = "\n".join(stripper.parts)
    return title, text


async def fetch(url: str, offset: int = 0, max_chars: int = 12000) -> dict:
    max_chars = max(1000, min(max_chars, 20000))
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
        for _ in range(6):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Use a plain http(s) URL")
            await asyncio.to_thread(_public, parsed.hostname)
            async with client.stream("GET", url) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                body = bytearray()
                async for block in response.aiter_bytes():
                    body += block
                    if len(body) > MAX_BYTES:
                        break
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                status = response.status_code
                encoding = response.encoding or "utf-8"
                break
        else:
            raise ValueError("Too many redirects")
    title = ""
    if kind == "application/pdf" or bytes(body[:5]) == b"%PDF-":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(bytes(body)))
        text = "\n\n".join(f"[page {n}]\n{page.extract_text() or ''}" for n, page in enumerate(reader.pages, 1))
    elif "html" in kind or bytes(body[:200]).lstrip().lower().startswith((b"<!doctype html", b"<html")):
        title, text = await asyncio.to_thread(_extract, bytes(body).decode(encoding, errors="replace"), url)
    elif kind.startswith("text/") or kind in {"application/json", "application/xml", "application/rss+xml"}:
        text = bytes(body).decode(encoding, errors="replace")
    else:
        return {"url": url, "status": status, "content_type": kind, "bytes": len(body),
                "content": "Binary content. To keep it, download it with curl in the shell."}
    total = len(text)
    part = text[offset:offset + max_chars]
    result = {"url": url, "status": status, "title": title, "content": part, "total_characters": total}
    if offset + max_chars < total:
        result["next_offset"] = offset + max_chars
    return result


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)
