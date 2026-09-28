"""A lead Qwen agent that delegates to specialist sub-agents, all running on your abliterated models.

    python crew.py "What is trending on TikTok in what we collected today? Draft 3 original video scripts."
    python crew.py --show-drafts

Lead (qwen-1, deeper thinking) plans and delegates. Sub-agents use "qwen" (whichever resident
is free), so they run in parallel across both GPUs. If FRONTIER_MODEL is set in agents/.env,
the lead can also ask that model for a second opinion (it then sees the text it is sent).
"""
from __future__ import annotations

import argparse
import asyncio
import json

from agents import Agent, ModelSettings, Runner, function_tool

import hub
import store


def rows(sql: str, params: tuple = ()) -> list[dict]:
    db = store.connect()
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    finally:
        db.close()


def compact(items: list[dict], limit: int = 12000) -> str:
    text = json.dumps(items, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " …(truncated; ask for fewer results)"


POST_COLUMNS = "id, platform, creator, caption, on_screen_text, visual_summary, topic, hashtags, sound, likes, comments, shares, is_ad, collected_at"


@function_tool
def recent_posts(platform: str = "any", hours: int = 48, limit: int = 40) -> str:
    """Posts collected from the phone in the last `hours`, newest first. platform: tiktok, instagram or any."""
    sql = f"SELECT {POST_COLUMNS} FROM posts WHERE collected_at >= datetime('now', ?)"
    params: list = [f"-{int(hours)} hours"]
    if platform != "any":
        sql += " AND platform = ?"
        params.append(platform)
    sql += " ORDER BY collected_at DESC LIMIT ?"
    params.append(min(int(limit), 100))
    return compact(rows(sql, tuple(params)))


@function_tool
def search_posts(query: str, limit: int = 20) -> str:
    """Find collected posts whose caption, on-screen text, topic, hashtags or summary contain `query`."""
    like = f"%{query}%"
    return compact(rows(
        f"SELECT {POST_COLUMNS} FROM posts WHERE caption LIKE ? OR on_screen_text LIKE ? OR topic LIKE ? OR hashtags LIKE ? OR visual_summary LIKE ? "
        "ORDER BY collected_at DESC LIMIT ?", (like, like, like, like, like, min(int(limit), 60))))


@function_tool
def topic_stats(platform: str = "any", hours: int = 72) -> str:
    """Topics ranked by how often they appeared and their total likes (ads excluded)."""
    sql = ("SELECT topic, platform, COUNT(*) AS posts, SUM(COALESCE(likes,0)) AS total_likes, MAX(likes) AS best_likes "
           "FROM posts WHERE is_ad = 0 AND collected_at >= datetime('now', ?)")
    params: list = [f"-{int(hours)} hours"]
    if platform != "any":
        sql += " AND platform = ?"
        params.append(platform)
    sql += " GROUP BY topic, platform ORDER BY posts DESC, total_likes DESC LIMIT 40"
    return compact(rows(sql, tuple(params)))


@function_tool
def save_draft(platform: str, title: str, hook: str, script: str, caption: str, hashtags: list[str], source_post_ids: list[int], notes: str = "") -> str:
    """Save a finished post draft (tiktok or instagram) with the ids of the collected posts that inspired it."""
    db = store.connect()
    try:
        cur = db.execute(
            "INSERT INTO drafts (platform, title, hook, script, caption, hashtags, source_post_ids, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (platform, title, hook, script, caption, json.dumps(hashtags), json.dumps(source_post_ids), notes, store.now()))
        db.commit()
        return f"Saved draft #{cur.lastrowid}"
    finally:
        db.close()


@function_tool
def list_drafts(status: str = "draft", limit: int = 20) -> str:
    """Saved drafts, newest first."""
    return compact(rows("SELECT * FROM drafts WHERE status = ? ORDER BY id DESC LIMIT ?", (status, min(int(limit), 50))))


def light(effort: str) -> ModelSettings:
    return ModelSettings(extra_body={"reasoning_effort": effort})


def build_team() -> Agent:
    analyst = Agent(
        name="trend_analyst",
        model=hub.model("qwen"),
        model_settings=light("low"),
        tools=[recent_posts, search_posts, topic_stats],
        instructions=(
            "You analyse posts collected from TikTok and Instagram. Use the tools; never guess numbers. "
            "Report recurring topics, hooks and formats with the post ids that support each finding, and say "
            "how many posts each finding rests on. Ignore ads unless asked."),
    )
    writer = Agent(
        name="writer",
        model=hub.model("qwen"),
        model_settings=light("medium"),
        tools=[search_posts, save_draft],
        instructions=(
            "You write original short-form video posts. Given a brief, write a strong first-2-seconds hook, "
            "a script with shot directions, a caption and 3-8 hashtags. Take inspiration from the collected posts "
            "but never copy their wording; cite their ids in source_post_ids. Save every finished draft with save_draft."),
    )
    critic = Agent(
        name="critic",
        model=hub.model("qwen"),
        model_settings=light("low"),
        tools=[list_drafts],
        instructions="You review drafts bluntly: hook strength, originality versus the sources, clarity, length for the platform. Give concrete fixes.",
    )
    tools = [
        analyst.as_tool("trend_analyst", "Finds patterns in the collected posts. Give it a specific question."),
        writer.as_tool("writer", "Writes and saves original post drafts. Give it a brief: platform, topic, angle, supporting post ids."),
        critic.as_tool("critic", "Reviews saved drafts and suggests fixes."),
        list_drafts,
    ]
    if hub.FRONTIER_MODEL:
        advisor = Agent(name="frontier_advisor", model=hub.model(hub.FRONTIER_MODEL),
                        instructions="Give a concise expert second opinion on the plan or text you are shown.")
        tools.append(advisor.as_tool("frontier_advisor", f"Second opinion from {hub.FRONTIER_MODEL}. Costs money; use sparingly."))
    return Agent(
        name="lead",
        model=hub.model("qwen-1"),
        model_settings=light("medium"),
        tools=tools,
        instructions=(
            "You lead a small content team. Break the user's goal into steps, delegate each step to the right "
            "sub-agent with a precise brief, check their results, and finish with a short report: what was found, "
            "which drafts were saved (ids) and what to do next. Delegate independent steps in parallel when you can."),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", nargs="?", help="what you want the team to do")
    ap.add_argument("--show-drafts", action="store_true")
    ap.add_argument("--max-turns", type=int, default=30)
    args = ap.parse_args()
    if args.show_drafts:
        for d in rows("SELECT id, platform, title, hook, caption, created_at FROM drafts ORDER BY id DESC LIMIT 20"):
            print(f"#{d['id']} [{d['platform']}] {d['title']}\n   hook: {d['hook']}\n   caption: {d['caption']}\n")
        return
    if not args.task:
        ap.error("give a task, or use --show-drafts")
    result = asyncio.run(Runner.run(build_team(), args.task, max_turns=args.max_turns))
    print(result.final_output)


if __name__ == "__main__":
    main()
