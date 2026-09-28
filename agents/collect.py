"""Scroll TikTok or Instagram Reels on the USB phone and store what each post contains.

    python collect.py tiktok --posts 30
    python collect.py instagram --posts 30 --ui-text

Each screen is read by the abliterated Qwen through its vision encoder; results go to
agents/data/hub.db (posts table) with a screenshot per post for reference.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import time
from datetime import datetime

import hub
import store
from phone import APPS, Phone

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "is_post": {"type": "boolean", "description": "false for login walls, pop-ups, loading or live-stream screens"},
        "is_ad": {"type": "boolean"},
        "creator": {"type": ["string", "null"]},
        "caption": {"type": ["string", "null"]},
        "on_screen_text": {"type": ["string", "null"], "description": "text burned into the video frame"},
        "visual_summary": {"type": "string", "description": "one or two sentences on what the frame shows"},
        "topic": {"type": "string", "description": "short topic label, e.g. 'budget meal prep'"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "sound": {"type": ["string", "null"]},
        "likes": {"type": ["string", "null"]},
        "comments": {"type": ["string", "null"]},
        "shares": {"type": ["string", "null"]},
    },
    "required": ["is_post", "is_ad", "creator", "caption", "on_screen_text", "visual_summary", "topic", "hashtags", "sound", "likes", "comments", "shares"],
}

PROMPT = (
    "This is a screenshot of the {platform} vertical video feed on an Android phone. "
    "Read everything visible and fill in the JSON fields exactly as shown on screen: creator handle, caption, "
    "hashtags, sound/music name, and the like/comment/share counts as displayed (e.g. '12.3K'). "
    "Use null when something is not visible. Do not invent values."
)


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    return json.loads(text)


def extract(client, model: str, platform: str, png: bytes, ui_text: str) -> dict:
    prompt = PROMPT.format(platform=platform)
    if ui_text:
        prompt += "\n\nText the phone reports on this screen (may be partial):\n" + ui_text[:3000]
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, hub.image_part(png)]}],
        response_format={"type": "json_schema", "json_schema": {"name": "post", "schema": POST_SCHEMA}},
        max_tokens=1500,
        temperature=0.2,
        # Reading a screen needs no long deliberation; skipping thinking keeps each post to seconds.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return parse_json(response.choices[0].message.content)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("platform", choices=sorted(APPS))
    ap.add_argument("--posts", type=int, default=25, help="how many posts to scroll through")
    ap.add_argument("--model", default="qwen", help="gateway model id (default: least-busy resident)")
    ap.add_argument("--ui-text", action="store_true", help="also read the accessibility text (slower; often empty on video feeds)")
    ap.add_argument("--no-open", action="store_true", help="don't open the app; start from whatever feed is on screen")
    ap.add_argument("--min-wait", type=float, default=3.0)
    ap.add_argument("--max-wait", type=float, default=8.0)
    args = ap.parse_args()

    phone, client, db = Phone(), hub.sync_client(), store.connect()
    print(f"Phone: {phone.check()}")

    def open_feed() -> None:
        if args.platform == "instagram":
            phone.open_url("https://www.instagram.com/reels/")
        else:
            phone.open_app(args.platform)

    if not args.no_open:
        open_feed()
    packages = APPS[args.platform]
    shots = store.DATA / "shots" / args.platform
    shots.mkdir(parents=True, exist_ok=True)

    saved = duplicates = skipped = 0
    for i in range(1, args.posts + 1):
        time.sleep(random.uniform(args.min_wait, args.max_wait))  # watch time, like a person
        front = phone.foreground()
        if front and front not in packages:  # empty = couldn't tell; carry on
            print(f"  {front} is in front instead of {args.platform} (pop-up?). Pressing back.")
            phone.back()
            time.sleep(2)
            front = phone.foreground()
            if front and front not in packages and not args.no_open:
                open_feed()
            continue
        png = phone.screenshot()
        ui_text = phone.screen_text() if args.ui_text else ""
        started = time.time()
        try:
            post = extract(client, args.model, args.platform, png, ui_text)
        except Exception as error:  # keep scrolling even if one read fails
            print(f"[{i}] could not read this screen: {error}")
            phone.swipe_next()
            continue
        took = time.time() - started
        if not post.get("is_post"):
            skipped += 1
            print(f"[{i}] not a post ({post.get('visual_summary', '')[:60]}) — {took:.0f}s")
        else:
            path = shots / f"{datetime.now():%Y%m%d-%H%M%S}-{i}.png"
            path.write_bytes(png)
            post_id = store.save_post(db, args.platform, post, str(path))
            if post_id:
                saved += 1
                duplicates = 0
                print(f"[{i}] #{post_id} @{post.get('creator')} · {post.get('topic')} · {post.get('likes')} likes — {took:.0f}s")
            else:
                duplicates += 1
                path.unlink(missing_ok=True)
                print(f"[{i}] already collected — {took:.0f}s")
                if duplicates >= 5:
                    print("Five repeats in a row; the feed seems stuck. Stopping.")
                    break
        phone.swipe_next()
    print(f"Done: {saved} new posts, {skipped} non-post screens. Database: {store.DATA / 'hub.db'}")


if __name__ == "__main__":
    main()
