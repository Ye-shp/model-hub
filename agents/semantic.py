"""Text embeddings for hybrid memory/knowledge search. Pure stdlib plus the gateway's OpenAI client.

`embed()` asks the gateway (`POST {HUB_URL}/v1/embeddings`, model `EMBED_MODEL`, default "qwen"). If that fails
for any reason (no HUB_URL, connection error, 404, bad shape) it falls back to `local_embed()`: a deterministic
hashing embedder (signed word and character-trigram features, 384 dimensions). The fallback is NOT a real
model: it only captures shared words and spelling overlap, not meaning. It exists so the code works offline and in CI.

    import semantic
    # Offline: leave HUB_URL unset (or call local_embed directly); vectors have LOCAL_DIM entries.
    vecs = semantic.local_embed(["rain tomorrow", "umbrella"])
    print(len(vecs[0]), semantic.cosine(vecs[0], vecs[1]))    # 384, a float in [-1, 1]
    # Endpoint: set HUB_URL, HUB_KEY and optionally EMBED_MODEL in agents/.env, then:
    vecs = semantic.embed(["rain tomorrow", "umbrella"])      # gateway vectors, else the local fallback
    # embed() never raises; None only for an empty list. Vectors of different dimensions compare as -1.0.
    # Store/query through workspace.save_note / ingest and workspace.memories / search.
    note = semantic.embed(["a note"])[0]
    print(semantic.cosine(note, note))                        # 1.0
"""
from __future__ import annotations

import hashlib
import math
import os
import re

LOCAL_DIM = 384
BATCH = 64


def _features(text: str) -> list[str]:
    words = re.findall(r"\w+", text.lower())
    feats = ["w:" + w for w in words]
    for w in words:
        padded = f"^{w}$"
        feats += ["t:" + padded[i:i + 3] for i in range(max(1, len(padded) - 2))]
    return feats or ["empty"]  # empty text still gets a fixed, non-zero vector


def _local_one(text: str) -> list[float]:
    vec = [0.0] * LOCAL_DIM
    for feat in _features(text):
        digest = hashlib.blake2b(feat.encode(), digest_size=8).digest()
        number = int.from_bytes(digest, "little")
        vec[number % LOCAL_DIM] += 1.0 if (number >> 63) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:  # signed hashes cancelled out exactly; fall back to a fixed unit vector
        vec[0], norm = 1.0, 1.0
    return [x / norm for x in vec]


def local_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic offline fallback embedding (weak: lexical overlap only)."""
    return [_local_one(t or "") for t in texts]


def _remote_embed(texts: list[str]) -> list[list[float]] | None:
    try:
        import hub
        client = hub.sync_client()
        model = os.environ.get("EMBED_MODEL", "qwen")
        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            batch = texts[start:start + BATCH]
            data = client.embeddings.create(model=model, input=batch).data
            vectors = [[float(x) for x in item.embedding] for item in sorted(data, key=lambda d: d.index)]
            if len(vectors) != len(batch) or not vectors[0] or len({len(v) for v in vectors}) != 1:
                return None
            out += vectors
        if len({len(v) for v in out}) != 1:
            return None
        return out
    except Exception:
        return None


def embed(texts: list[str]) -> list[list[float]] | None:
    """One vector per text: gateway embeddings if available, else the local fallback. Never raises."""
    try:
        if not texts:
            return None
        texts = [t if isinstance(t, str) else str(t) for t in texts]
        return _remote_embed(texts) or local_embed(texts)
    except Exception:
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]; -1.0 for empty, zero-length or mismatched-dimension vectors."""
    if not a or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return max(-1.0, min(1.0, dot / (na * nb)))
