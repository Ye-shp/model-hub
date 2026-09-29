"""Recognise the owner through Cloudflare Access, so console.handydandy.cc opens without pasting a key.

Cloudflare adds a signed token (Cf-Access-Jwt-Assertion) to every request that passed the Access login.
It is verified here against the team's published keys: its audience must be the console's Access app and
its email the owner's. A header alone proves nothing (anything on the box can send one); the signature does.
Configured with ACCESS_TEAM, CONSOLE_ACCESS_AUD and OWNER_EMAIL; without them only the owner key works.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time

try:
    import httpx2 as httpx
except ImportError:
    import httpx

TEAM = os.environ.get("ACCESS_TEAM", "")
AUDIENCE = os.environ.get("CONSOLE_ACCESS_AUD", "")
OWNER = os.environ.get("OWNER_EMAIL", "").strip().lower()
_keys: dict = {"at": 0.0, "keys": {}}
_lock = threading.Lock()


def configured() -> bool:
    return bool(TEAM and AUDIENCE and OWNER)


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _public_keys(refresh: bool = False) -> dict:
    with _lock:
        if refresh or time.time() - _keys["at"] > 3600 or not _keys["keys"]:
            response = httpx.get(f"https://{TEAM}.cloudflareaccess.com/cdn-cgi/access/certs", timeout=10)
            response.raise_for_status()
            _keys["keys"] = {k["kid"]: k for k in response.json().get("keys", []) if k.get("kty") == "RSA"}
            _keys["at"] = time.time()
        return _keys["keys"]


def verify(token: str, keys: dict | None = None, now: float | None = None) -> dict | None:
    """The token's claims if it is a valid owner token for the console, else None."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    try:
        header_part, claims_part, signature_part = token.split(".")
        header, claims = json.loads(_b64(header_part)), json.loads(_b64(claims_part))
        if header.get("alg") != "RS256":
            return None
        available = keys if keys is not None else _public_keys()
        if header.get("kid") not in available and keys is None:
            available = _public_keys(refresh=True)
        jwk = available.get(header.get("kid"))
        if not jwk:
            return None
        public = rsa.RSAPublicNumbers(int.from_bytes(_b64(jwk["e"]), "big"), int.from_bytes(_b64(jwk["n"]), "big")).public_key()
        public.verify(_b64(signature_part), f"{header_part}.{claims_part}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, KeyError, TypeError, InvalidSignature, httpx.HTTPError):
        return None
    now = now or time.time()
    audiences = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
    if AUDIENCE not in audiences or claims.get("iss") != f"https://{TEAM}.cloudflareaccess.com":
        return None
    if not (claims.get("exp", 0) > now >= claims.get("nbf", claims.get("iat", 0)) - 60):
        return None
    if (claims.get("email") or "").strip().lower() != OWNER:
        return None
    return claims
