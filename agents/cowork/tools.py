"""The agent's tools. Each tool is built by a small factory that closes over a ToolContext."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
from dataclasses import dataclass, field

from agents import Agent, ModelSettings, Runner, function_tool

import coordination
import escalate
import sandbox
import web
import workspace as ws
from crew import IMAGE_SIZES, POST_COLUMNS, CallBudget

from .config import MAX_CHAIN, MAX_IMAGES, MAX_PARALLEL_HELPERS, PLAN_FILE, PROFILES, RESIDENTS, SHARE_LIMIT
from .context import RUN_CONFIG, describe
from .continuity import chain_depth, current_request
from .effort import AdaptiveModel
from .prompt import instructions


class StopTask(Exception):
    """Ends the run at the next model call; the task then writes a report (see run_job)."""


@dataclass
class ToolContext:
    """Everything the tools share for one task."""
    job: dict
    space: sandbox.Workspace
    client: object
    gate: asyncio.Semaphore
    budget: CallBudget
    state: dict
    images: dict
    log: object  # (kind, detail) -> None
    before: object  # (tool or model name) -> None; raises StopTask when the task must stop
    lead_model: str
    helper_model: str
    escalation: list = field(default_factory=list)
    phone: bool = False
    research_status: str = ""

    @property
    def project(self) -> str:
        return self.job["project"]

    @property
    def job_id(self) -> str:
        return self.job["id"]

    @property
    def profile(self) -> dict:
        return PROFILES[self.job["profile"]]


# ---- workspace ----
def _run_shell(ctx: ToolContext):
    @function_tool
    async def run_shell(command: str, timeout_seconds: int = 120) -> str:
        """Run a bash command in the workspace folder and return its exit code and output (stdout+stderr).
        Use it to run code, install packages, convert files, download things with curl, etc. Long-running
        commands: raise timeout_seconds (max 1800)."""
        ctx.budget.active()
        folder = str(ctx.space.dir)
        ctx.log("tool", "Running: " + describe(command.replace("cd " + folder + " && ", "").replace(folder + "/", "")))
        result = await ctx.space.run(command, timeout=max(5, min(timeout_seconds, 1800)))
        head = "Timed out and was stopped" if result["timed_out"] else f"Exit code {result['exit_code']}"
        return head + "\n" + sandbox.trim(result["output"] or "(no output)")
    return run_shell


def _list_files(ctx: ToolContext):
    @function_tool
    def list_files(path: str = ".", depth: int = 2) -> str:
        """List files and folders (with sizes) under a workspace path."""
        return ctx.space.listing(path, max(1, min(depth, 5)))
    return list_files


def _read_file(ctx: ToolContext):
    @function_tool
    def read_file(path: str, offset: int = 1, limit: int = 400) -> str:
        """Read a text file from the workspace with line numbers. offset is the first line (1-based)."""
        ctx.log("tool", f"Reading {path}")
        return ctx.space.read_text(path, offset, limit)
    return read_file


def _write_file(ctx: ToolContext):
    @function_tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a text file in the workspace (folders are created as needed)."""
        ctx.budget.active()
        ctx.log("tool", f"Writing {path}")
        return ctx.space.write_text(path, content)
    return write_file


def _edit_file(ctx: ToolContext):
    @function_tool
    def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Replace exact text in a workspace file. old_text must match exactly (read the file first)."""
        ctx.budget.active()
        ctx.log("tool", f"Editing {path}")
        return ctx.space.edit(path, old_text, new_text, replace_all)
    return edit_file


def _share_file(ctx: ToolContext):
    @function_tool
    def share_file(path: str) -> str:
        """Give a workspace file to the user as a deliverable (it appears as a download in the chat)."""
        ctx.budget.active()
        target = ctx.space.resolve(path)
        if not target.is_file():
            raise ValueError(f"{path} is not a file")
        size = target.stat().st_size
        if size > SHARE_LIMIT:
            raise ValueError("File is larger than 100 MB; compress or split it")
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        saved = ws.write_artifact(ctx.project, ctx.job_id, target.name, target.read_bytes(), media)
        ctx.log("artifact", json.dumps({"id": saved["id"], "name": saved["name"], "media_type": media}))
        return f"Shared {saved['name']} ({size:,} bytes)"
    return share_file


# ---- web ----
def _web_search(ctx: ToolContext):
    @function_tool
    async def web_search(query: str, max_results: int = 8) -> str:
        """Search the web. Returns titles, URLs and snippets; read promising pages with read_webpage."""
        ctx.budget.active()
        ctx.log("tool", f"Searching the web: {query[:120]}")
        return web.as_json(await web.search(query, max_results))
    return web_search


def _read_webpage(ctx: ToolContext):
    @function_tool
    async def read_webpage(url: str, offset: int = 0) -> str:
        """Read a web page or PDF as text. Long pages come in parts: pass next_offset to continue."""
        ctx.budget.active()
        ctx.log("tool", f"Reading {url[:150]}")
        return web.as_json(await web.fetch(url, offset))
    return read_webpage


# ---- project memory and collected posts ----
def _recall(ctx: ToolContext):
    @function_tool
    def recall(query: str = "") -> str:
        """Search saved project memory: the user's preferences, decisions and facts from earlier work."""
        return ws.bounded_json(ws.memories(ctx.project, query), limit=8000)
    return recall


def _remember(ctx: ToolContext):
    @function_tool
    def remember(title: str, content: str, kind: str = "fact") -> str:
        """Save a lasting note to project memory. kind: fact, decision, preference, checkpoint or question."""
        ctx.budget.active()
        identity = ws.save_note(ctx.project, title, content, kind, [f"job:{ctx.job_id}"])
        ctx.log("memory", f"Saved note: {title}")
        return f"Saved memory {identity}"
    return remember


def _search_knowledge(ctx: ToolContext):
    @function_tool
    def search_knowledge(query: str, limit: int = 5) -> str:
        """Search documents imported into this project."""
        return ws.bounded_json(ws.search(ctx.project, query, limit), limit=10000)
    return search_knowledge


def _search_posts(ctx: ToolContext):
    @function_tool
    def search_posts(query: str, limit: int = 15) -> str:
        """Find collected TikTok/Instagram posts by caption, on-screen text, topic or visual summary."""
        like = f"%{query}%"
        return ws.bounded_json(ws.query(
            f"SELECT {POST_COLUMNS} FROM posts WHERE project=? AND (caption LIKE ? OR on_screen_text LIKE ? OR topic LIKE ? "
            "OR visual_summary LIKE ?) ORDER BY id DESC LIMIT ?", (ctx.project, like, like, like, like, max(1, min(limit, 40)))), limit=9000)
    return search_posts


def _recent_posts(ctx: ToolContext):
    @function_tool
    def recent_posts(platform: str = "any", hours: int = 48, limit: int = 20) -> str:
        """Collected posts from the last N hours (collection time, not publication time). platform: tiktok, instagram or any."""
        return ws.bounded_json(ws.query(
            f"SELECT {POST_COLUMNS} FROM posts WHERE project=? AND datetime(collected_at)>=datetime('now',?) "
            "AND (?='any' OR platform=?) ORDER BY collected_at DESC LIMIT ?",
            (ctx.project, f"-{max(1, min(hours, 8760))} hours", platform, platform, max(1, min(limit, 50)))), limit=9000)
    return recent_posts


def _topic_stats(ctx: ToolContext):
    @function_tool
    def topic_stats(platform: str = "any", hours: int = 72) -> str:
        """Topic counts and engagement in the collected posts, excluding ads."""
        return ws.bounded_json(ws.query(
            "SELECT topic,platform,COUNT(*) AS posts,SUM(COALESCE(likes,0)) AS total_likes,AVG(likes) AS mean_likes "
            "FROM posts WHERE project=? AND is_ad=0 AND datetime(collected_at)>=datetime('now',?) AND (?='any' OR platform=?) "
            "GROUP BY topic,platform ORDER BY posts DESC LIMIT 30",
            (ctx.project, f"-{max(1, min(hours, 8760))} hours", platform, platform)), limit=9000)
    return topic_stats


# ---- plan ----
def _update_plan(ctx: ToolContext):
    @function_tool
    def update_plan(steps: list[str], active_index: int, completed_indices: list[int]) -> str:
        """Show the user your task list: 1-12 short steps, the zero-based index of the step in progress
        (-1 for none) and the indices already completed."""
        steps_now = coordination.update_plan(ctx.job_id, steps, active_index, completed_indices)
        pending = [s["title"] for s in steps_now if s["status"] != "completed"]
        reply = json.dumps(steps_now)
        if len(pending) >= 3 and ctx.state["delegations"] == 0:
            reply += ("\nReminder: the second GPU is idle. If any of these pending steps don't depend on each other, run "
                      "them now as helpers in one delegate_many call instead of one by one.")
        return reply
    return update_plan


def _queue_next_phase(ctx: ToolContext):
    @function_tool
    def queue_next_phase(brief: str) -> str:
        """For big projects: start the next phase automatically as a new task in this chat once this one finishes.
        Write and update plan.md first. brief: what the next phase must do, which plan.md phase it is, and which
        files to read first. Call at most once, near the end of your work."""
        ctx.budget.active()
        account = "owner" if ctx.space.is_owner else "friend"
        if ctx.state.get("next"):
            return "The next phase is already queued."
        if not (ctx.space.dir / PLAN_FILE).is_file():
            return f"Not queued: write {PLAN_FILE} first (goal, phases as a checklist, decisions, file map)."
        if chain_depth(ctx.job) >= MAX_CHAIN[account]:
            return (f"Not queued: {MAX_CHAIN[account]} phases already ran automatically in a row. Finish with a summary of "
                    "where the project stands; the user can reply 'continue' to start the next phase.")
        if not 20 <= len(brief.strip()) <= 6000:
            return "Not queued: give a brief of 20-6000 characters."
        ctx.state["next"] = brief.strip()
        ctx.log("tool", "Next phase will start after this task: " + describe(brief, 120))
        return "Queued: the next phase starts automatically when this task ends. Now finish this phase and write your reply."
    return queue_next_phase


# ---- images ----
def _generate_image(ctx: ToolContext):
    @function_tool
    async def generate_image(prompt: str, filename: str = "image.png", size: str = "1024x1024") -> str:
        """Generate an image with Qwen-Image and save it to the workspace (it is shared with the user automatically).
        size: 1024x1024, 1024x1536 (2:3), 1536x1024 (3:2), 1152x2048 (9:16 TikTok/Reels), 2048x1152 (16:9),
        or 2K finals 2048x2048, 1536x2752 (9:16), 2752x1536 (16:9). Quote any on-image text exactly."""
        ctx.budget.active()
        if ctx.images["count"] >= MAX_IMAGES:
            raise ValueError(f"Image limit for one task reached ({MAX_IMAGES})")
        if size not in IMAGE_SIZES:
            raise ValueError("Choose one of: " + ", ".join(sorted(IMAGE_SIZES)))
        ctx.images["count"] += 1
        ctx.log("image-request", f"Generating image {size}: {prompt[:100]}")
        response = await ctx.client.with_options(timeout=900).images.generate(model="flex", prompt=prompt, size=size, n=1,
                                                                               response_format="b64_json")
        if not response.data or not response.data[0].b64_json:
            raise RuntimeError("The image service returned no image")
        from PIL import Image
        image = Image.open(io.BytesIO(base64.b64decode(response.data[0].b64_json, validate=True)))
        image.load()
        output = io.BytesIO()
        image.save(output, format="PNG")
        name = filename if filename.lower().endswith(".png") else filename.rsplit(".", 1)[0] + ".png"
        target = ctx.space.write_bytes(name, output.getvalue())
        saved = ws.write_artifact(ctx.project, ctx.job_id, target.name, output.getvalue(), "image/png")
        ctx.log("artifact", json.dumps({"id": saved["id"], "name": saved["name"], "media_type": "image/png"}))
        return f"Saved and shared {ctx.space.relative(target)} ({size})"
    return generate_image


# ---- helpers ----
def settings(ctx: ToolContext, tokens: int, effort: str, parallel: bool) -> ModelSettings:
    return ModelSettings(max_tokens=tokens, parallel_tool_calls=parallel, include_usage=True,
                         extra_body={"reasoning_effort": effort})


def make_helper(ctx: ToolContext, model: str, helper_tools: list) -> Agent:
    profile = ctx.profile
    return Agent(name="helper", model=AdaptiveModel(model, ctx.client, ctx.gate, ctx.before),
                 tools=helper_tools,
                 model_settings=settings(ctx, profile["tokens"], "low" if profile["effort"] == "low" else "medium", False),
                 instructions=instructions(ctx.job, ctx.space, helper=True))


def _run_helper(ctx: ToolContext, helper_tools: list):
    from .runner import out_of_turns  # runner imports this module, so import it late
    profile = ctx.profile
    helpers = {ctx.helper_model: make_helper(ctx, ctx.helper_model, helper_tools)}

    async def run_helper(brief: str, model: str) -> str:
        if model not in helpers:
            helpers[model] = make_helper(ctx, model, helper_tools)
        ctx.state["delegations"] += 1
        ctx.log("delegate", f"Helper started on {model}: {brief[:140]}")
        try:
            result = await Runner.run(helpers[model], brief, max_turns=profile["helper_turns"], run_config=RUN_CONFIG,
                                      error_handlers=out_of_turns(ctx.client, ctx.gate, profile["tokens"], ctx.job_id))
            output = str(result.final_output)
        except StopTask:
            raise
        except Exception as error:  # one failed helper shouldn't sink the others
            output = f"Helper failed: {type(error).__name__}: {str(error)[:300]}"
        ctx.log("delegate-done", f"Helper finished: {brief[:80]}")
        return output
    return run_helper


def _delegate(ctx: ToolContext, run_helper):
    @function_tool
    async def delegate(brief: str) -> str:
        """Run one helper agent (on the other GPU) on a self-contained sub-task. It has the shell, files and web tools
        and shares this workspace. Returns the helper's report. For several independent pieces use delegate_many."""
        ctx.budget.active()
        return sandbox.trim(await run_helper(brief, ctx.helper_model), 12000)
    return delegate


def _delegate_many(ctx: ToolContext, run_helper):
    @function_tool
    async def delegate_many(briefs: list[str]) -> str:
        """Run 2-4 helper agents at the same time, one per brief, spread over both GPUs (you wait while they work).
        Each brief must be self-contained: goal, input files, the output file to write, what done looks like.
        Returns every helper's report."""
        ctx.budget.active()
        briefs = [b.strip() for b in briefs if b and b.strip()]
        if not briefs:
            raise ValueError("Give at least one brief")
        if len(briefs) > MAX_PARALLEL_HELPERS:
            raise ValueError(f"At most {MAX_PARALLEL_HELPERS} helpers at once; group the work or call again for the rest")
        # The lead waits during this call, so its GPU is free too: alternate helpers between the two GPUs.
        order = ([ctx.helper_model, ctx.lead_model] if ctx.lead_model in RESIDENTS and ctx.helper_model in RESIDENTS
                 else [ctx.helper_model])
        results = await asyncio.gather(*(run_helper(b, order[i % len(order)]) for i, b in enumerate(briefs)))
        limit = max(3000, 24000 // len(briefs))
        return "\n\n".join(f"### Helper {i + 1}: {b[:80]}\n{sandbox.trim(r, limit)}" for i, (b, r) in enumerate(zip(briefs, results)))
    return delegate_many


# ---- hand-off to frontier agents ----
def _hand_off(ctx: ToolContext):
    async def hand_off(kind: str, task: str) -> str:
        ctx.budget.active()
        label = "Claude Code" if kind == "claude" else "Codex"
        ctx.log("tool", f"Asking {label}: {task[:120]}")
        result = await escalate.run(kind, ctx.space, ctx.job_id, task)
        if not result.get("ok"):
            why = result.get("error") or ("it timed out" if result.get("timed_out") else
                                          f"it exited with code {result.get('exit_code')}")
            summary = (result.get("summary") or "").strip()
            ctx.state["stop"] = f"the {label} hand-off failed ({why})" + (f": {summary[:300]}" if summary and not result.get("error") else "")
            ctx.log("tool", f"{label} hand-off failed; stopping the task")
            return json.dumps(result, ensure_ascii=False) + "\n\nThis hand-off failed, so the task stops now and reports it."
        return json.dumps(result, ensure_ascii=False)
    return hand_off


def _ask_claude(ctx: ToolContext, hand_off):
    @function_tool
    async def ask_claude(task: str) -> str:
        """Hand a task to Claude Code (Anthropic's agent), which works in this same workspace folder with its own
        shell and file tools. Give a complete brief. Returns its summary; check the files it produced."""
        return await hand_off("claude", task)
    return ask_claude


def _ask_codex(ctx: ToolContext, hand_off):
    @function_tool
    async def ask_codex(task: str) -> str:
        """Hand a task to OpenAI Codex (ChatGPT's coding agent), which works in this same workspace folder with its
        own shell and file tools. Give a complete brief. Returns its summary; check the files it produced."""
        return await hand_off("codex", task)
    return ask_codex


def build_tools(ctx: ToolContext) -> list:
    """The lead agent's tools. Also fills in ctx.escalation, ctx.phone and ctx.research_status for the prompt."""
    job, space = ctx.job, ctx.space
    run_shell, list_files, read_file = _run_shell(ctx), _list_files(ctx), _read_file(ctx)
    write_file, edit_file, share_file = _write_file(ctx), _edit_file(ctx), _share_file(ctx)
    web_search, read_webpage = _web_search(ctx), _read_webpage(ctx)
    recall, remember = _recall(ctx), _remember(ctx)
    search_knowledge, search_posts = _search_knowledge(ctx), _search_posts(ctx)
    recent_posts, topic_stats = _recent_posts(ctx), _topic_stats(ctx)
    update_plan, queue_next_phase = _update_plan(ctx), _queue_next_phase(ctx)

    workspace_tools = [run_shell, list_files, read_file, write_file, edit_file]
    research_tools = [web_search, read_webpage, search_knowledge, search_posts]

    import research_tools as research_module
    research_list, ctx.research_status = research_module.build_tools(job, space, ctx.client, ctx.gate, ctx.log, ctx.budget,
                                                                     current_request(job.get("task") or ""), ctx.helper_model)
    # Helpers get the lookups and video analysis too (so several videos/topics can be researched in parallel),
    # never posting.
    helper_research = [t for t in research_list if t.name not in {"draft_post", "publish_post", "list_posts"}]

    run_helper = _run_helper(ctx, workspace_tools + research_tools + helper_research)
    delegate, delegate_many = _delegate(ctx, run_helper), _delegate_many(ctx, run_helper)

    tools = workspace_tools + [share_file] + research_tools + [recall, remember, recent_posts, topic_stats, update_plan,
                                                               queue_next_phase, delegate, delegate_many]
    if job["allow_images"]:
        tools.append(_generate_image(ctx))

    ctx.phone = False
    if space.is_owner:
        import phone_link
        if phone_link.connected():
            ctx.phone = True
            tools += phone_link.agent_tools(job, ctx.log, ctx.budget, current_request(job.get("task") or ""), ctx.client, ctx.gate)

    tools += research_list

    ctx.escalation = []
    if space.is_owner and job["allow_frontier"]:
        for kind in ("claude", "codex"):
            if escalate.available(kind) is None:
                ctx.escalation.append(kind)

    hand_off = _hand_off(ctx)
    if "claude" in ctx.escalation:
        tools.append(_ask_claude(ctx, hand_off))
    if "codex" in ctx.escalation:
        tools.append(_ask_codex(ctx, hand_off))
    return tools
