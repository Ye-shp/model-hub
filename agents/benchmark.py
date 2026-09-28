"""Explicit, small live benchmark. Never runs automatically or uses frontier models."""
import argparse
import asyncio
import json
import time
from pathlib import Path

import hub


async def measure(client, number: int, thinking: bool, tokens: int):
    started, first_visible, first_any, usage, text = time.perf_counter(), None, None, None, ""
    stream = await client.chat.completions.create(model="qwen", stream=True, stream_options={"include_usage": True},
            messages=[{"role": "user", "content": "Give a concise plan for organizing a small research project into evidence collection, analysis, and a written report."}],
            max_tokens=tokens, extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
    async for chunk in stream:
        if chunk.usage:
            usage = chunk.usage
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta.content or getattr(delta, "reasoning_content", None):
                first_any = first_any or time.perf_counter()
            if delta.content:
                first_visible = first_visible or time.perf_counter()
                text += delta.content
    elapsed = time.perf_counter() - started
    return {"request": number, "seconds": round(elapsed, 2),
            "first_token_seconds": round(first_any - started, 2) if first_any else None,
            "first_answer_seconds": round(first_visible - started, 2) if first_visible else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "completion_tokens_per_second_end_to_end": round(usage.completion_tokens / elapsed, 2) if usage else None,
            "answer_characters": len(text)}


async def main_async(args):
    client = hub.async_client()
    try:
        rows = await asyncio.gather(*(measure(client, i + 1, args.thinking, args.tokens) for i in range(args.concurrency)), return_exceptions=True)
    finally:
        await client.close()
    report = {"concurrency": args.concurrency, "thinking": args.thinking,
              "results": [{"error": type(r).__name__} if isinstance(r, Exception) else r for r in rows],
              "note": "One short sample; not a context-capacity, reasoning-quality, or phone-throughput guarantee."}
    print(json.dumps(report, indent=2))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--concurrency", type=int, choices=[1, 2, 3, 4], default=1)
    ap.add_argument("--tokens", type=int, choices=[256, 512, 1024], default=512)
    ap.add_argument("--thinking", action="store_true")
    ap.add_argument("--output", type=Path)
    asyncio.run(main_async(ap.parse_args()))
