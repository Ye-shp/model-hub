"""Constants and environment settings for Cowork."""
from __future__ import annotations

import os

PROFILES = {
    "fast": {"seconds": 1200, "turns": 30, "helper_turns": 12, "tokens": 8192, "effort": "low"},
    "balanced": {"seconds": 3600, "turns": 60, "helper_turns": 20, "tokens": 12288, "effort": "medium"},
    "deep": {"seconds": 7200, "turns": 100, "helper_turns": 30, "tokens": 16384, "effort": "medium"},
}
MAX_IMAGES = 8
SHARE_LIMIT = 100 * 1024**2
RESIDENTS = ("qwen-1", "qwen-2")  # one per GPU
# "auto" (default): each task's lead goes to the GPU with fewer leads right now, its helpers to the other one.
LEAD_MODEL = os.environ.get("COWORK_LEAD_MODEL", "auto")
HELPER_MODEL = os.environ.get("COWORK_HELPER_MODEL", "auto")
MAX_PARALLEL_HELPERS = int(os.environ.get("COWORK_MAX_PARALLEL_HELPERS", "4"))
MAX_CHAIN = {"owner": int(os.environ.get("COWORK_MAX_PHASES", "6")), "friend": int(os.environ.get("COWORK_FRIEND_MAX_PHASES", "2"))}
PLAN_FILE = "plan.md"
PLAN_LIMIT = 10_000

TOOLBOX = """Linux shell (Ubuntu 24.04) as an unprivileged user, with internet access. Installed: Python 3 (pandas, numpy,
matplotlib, openpyxl, python-docx, python-pptx, reportlab, pypdf, pillow, requests, beautifulsoup4, lxml), Node.js + npm,
ffmpeg, imagemagick, git, curl, jq, zip, yt-dlp, pandoc. `pip install <pkg>` and `npm install` work (user installs).
No sudo, no GPU."""

SOFT_CHARS = int(os.environ.get("COWORK_CONTEXT_SOFT_CHARS", "120000"))   # ~35K tokens: below this nothing changes
HARD_CHARS = int(os.environ.get("COWORK_CONTEXT_HARD_CHARS", "260000"))   # ~75K tokens: squeeze harder above this

ACTION_KINDS = ("tool", "delegate", "delegate-done", "escalation", "escalation-done", "artifact", "image-request",
                "memory", "phone", "next-phase", "partial")



RESEARCH_GUIDE = """RESEARCH, VIDEO AND SOCIAL TOOLS (free; pick them yourself whenever they fit)
- analyze_video: whenever the user shares or mentions a specific TikTok/Reel/Short/X video (link or upload) or wants
  to know why a video works. Returns hook, beats, CTA, pacing, sound and AI-tool fingerprint, from real measurements.
- trend_research: "what's trending / what are people saying about X lately" — ranked posts from the last 30 days
  across Reddit, X, YouTube, Hacker News, Polymarket, GitHub, Bluesky. Synthesise it; cite the posts.
- google_trends: is interest in a keyword rising or falling; compare 2-5 keywords.
- x_search / x_trends / x_user (owner only, needs X connected): live X posts, what's trending, an account's posts.
- study_profile: "what makes @creator work / analyse this account" — the profile's numbers, outliers, deep dives into
  the best posts and one playbook saved to the knowledge base. study_link does the same for a single post.
- instagram_profile / tiktok_profile: a specific creator's recent posts and numbers (raw, no analysis). TikTok blocks this server often;
  for TikTok research the phone (phone_collect) and your collected posts (recent_posts/topic_stats) are more reliable.
- For a content task, a good order is: research what's working (trend_research, collected posts, x_search) →
  analyze_video on 2-3 top examples → write the content. Run independent lookups in parallel with delegate_many.
- Posting (owner only): draft_post saves a draft; publish_post only works after the user's own message approves that
  draft's number ("approve post 7"). Never claim something was posted unless publish_post confirmed it. TikTok posts go
  through the phone (the media is sent to its gallery, then you post with the phone tools). Post only to the user's own
  connected accounts."""
