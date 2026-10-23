"""Constants and environment settings for Cowork."""
from __future__ import annotations

import os

PROFILES = {
    # orchestrator_turns: steps for a GLM lead (glm.py). It only plans, assigns and reviews, so it needs far fewer
    # steps than a Qwen lead that does the work itself, and every step it takes is paid.
    "fast": {"seconds": 1200, "turns": 30, "helper_turns": 12, "tokens": 8192, "effort": "low", "orchestrator_turns": 15},
    "balanced": {"seconds": 3600, "turns": 60, "helper_turns": 20, "tokens": 12288, "effort": "medium",
                 "orchestrator_turns": 30},
    "deep": {"seconds": 7200, "turns": 100, "helper_turns": 30, "tokens": 16384, "effort": "medium",
             "orchestrator_turns": 45},
}
MAX_IMAGES = 8
SHARE_LIMIT = 100 * 1024**2
RESIDENTS = ("qwen-1", "qwen-2")  # one per GPU
# "auto" (default): each task's lead goes to the GPU with fewer leads right now, its helpers to the other one.
LEAD_MODEL = os.environ.get("COWORK_LEAD_MODEL", "auto")
HELPER_MODEL = os.environ.get("COWORK_HELPER_MODEL", "auto")
MAX_PARALLEL_HELPERS = int(os.environ.get("COWORK_MAX_PARALLEL_HELPERS", "4"))
# With GLM leading (glm.py) both GPUs are free for helpers: up to this many at once, spread over qwen-1 and qwen-2.
ORCHESTRATOR_HELPERS = int(os.environ.get("COWORK_ORCHESTRATOR_HELPERS", "6"))
# Which Cowork modes GLM leads ("chat", the quick-chat mode, stays on Qwen unless added here).
ORCHESTRATOR_SKILLS = {s.strip() for s in os.environ.get("COWORK_ORCHESTRATOR_SKILLS", "cowork").split(",") if s.strip()}
# A GLM lead's context is trimmed sooner than a Qwen lead's: every character it re-reads is paid for.
ORCHESTRATOR_SOFT_CHARS = int(os.environ.get("COWORK_ORCHESTRATOR_SOFT_CHARS", "60000"))
ORCHESTRATOR_HARD_CHARS = int(os.environ.get("COWORK_ORCHESTRATOR_HARD_CHARS", "120000"))
# Helper reports the GLM lead reads are cut to this many characters (details stay in the files helpers write).
ORCHESTRATOR_REPORT_CHARS = int(os.environ.get("COWORK_ORCHESTRATOR_REPORT_CHARS", "5000"))
# Automatic project phases in a row. 0 = no limit (the owner's default): a project keeps going phase after phase
# until it is finished or stops making progress (NO_PROGRESS_RUNS below).
MAX_CHAIN = {"owner": int(os.environ.get("COWORK_MAX_PHASES", "0")), "friend": int(os.environ.get("COWORK_FRIEND_MAX_PHASES", "2"))}
# When a task hits its time or step limit, the next part starts by itself in the same chat (no "continue" needed),
# up to this many times in a row. 0 = no limit (the default).
AUTO_CONTINUE = int(os.environ.get("COWORK_AUTO_CONTINUE", "0"))
# The safety net once the counts are unlimited: automatic continuations and phases stop after this many runs in a
# row that changed nothing (no file in the chat's folder added or changed, nothing shared).
NO_PROGRESS_RUNS = max(1, int(os.environ.get("COWORK_NO_PROGRESS_RUNS", "2")))
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
- frame_sheet (owner): when the user wants a video's character, look, outfits or shots captured to reuse later
  (a character sheet, a lookbook, "copy this person's style"). Frame by frame; saved to the knowledge base.
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
