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
# When a task hits its time or step limit, the next part starts by itself in the same chat (no "continue" needed),
# up to this many times in a row.
AUTO_CONTINUE = int(os.environ.get("COWORK_AUTO_CONTINUE", "4"))
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
- trend_research: "what's trending / what are people saying about X lately" — ranked posts across Reddit, X,
  YouTube, Hacker News, Polymarket, GitHub, Bluesky. Synthesise it; cite the posts and name the window. Set days to
  the window the user asks for: 2 (past 2 days / last 48 hours), 7 (past week), 14 (past 2 weeks) or 30 (past month);
  30 when they don't say, and the nearest of these for other spans (e.g. "today" → 2, "10 days" → 14).
- google_trends: is interest in a keyword rising or falling; compare 2-5 keywords.
- x_search / x_trends / x_user (owner only, needs X connected): live X posts, what's trending, an account's posts.
- study_profile: "what makes @creator work / analyse this account" — the profile's numbers, outliers, deep dives into
  the best posts and one playbook saved to the knowledge base. study_link does the same for a single post.
- instagram_profile / tiktok_profile: a specific creator's recent posts and numbers (raw, no analysis). TikTok blocks this server often;
  for TikTok research the phone (phone_collect) and your collected posts (recent_posts/topic_stats) are more reliable.
- For a content task, a good order is: research what's working (trend_research, collected posts, x_search) →
  analyze_video on 2-3 top examples → write the content. Run independent lookups in parallel with delegate_many.
- Posting (owner only): draft_post saves a draft; publish_post only works after the user's own message approves that
  draft's number ("approve post 7"). Never claim a queued, prepared or unconfirmed post was published.
- Native phone automation (owner only): configure_phone_automation uses the user's supplied Tailscale/SSH ADB address
  and logged-in Instagram/TikTok username. phone_automation_status shows setup readiness and the persistent queue.
  After specific draft approval, schedule_post queues its exact media/caption/account/time outside the chat timeout;
  cancel_scheduled_post cancels pending work. An uncertain result is held: never retry it to guess whether it posted.
  confirm_scheduled_post requires the owner's current message to affirm the numbered draft was published and provide
  its actual post link and ISO publication time. Existing memories, chats,
  playbooks and drafts remain in their current persistent storage. Instagram's existing API path remains available;
  configured TikTok publish_post uses the native queue. Post only to the owner's configured accounts."""
