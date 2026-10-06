"""Building the agent and running a task."""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack

from agents import Agent, ModelSettings, Runner, SQLiteSession

import escalate
import hub
import reasoning
import sandbox
import store
import workspace as ws
from crew import CallBudget

from .config import AUTO_CONTINUE, PLAN_FILE, PROFILES
from .context import RUN_CONFIG, describe, replayable, trim_items
from .continuity import current_request, read_plan, recap
from .effort import AdaptiveModel
from .gpu import assign_gpus, release_gpus
from .intent import is_automatic, wants_claude
from .prompt import STOP_WRAP_UP, WRAP_UP, instructions
from .tier import tier_for
from .tools import StopTask, ToolContext, build_tools, cancel_background, settings


async def open_connectors(job: dict, space: sandbox.Workspace, state: dict, stack: AsyncExitStack) -> None:
    """The owner's MCP servers and APIs (connectors.py) as tools for this task, plus a note for the prompt."""
    import connectors

    def still_running():
        rows = ws.query("SELECT status FROM jobs WHERE id=?", (job["id"],))
        if not rows or rows[0]["status"] != "running":
            raise RuntimeError("This task is no longer running")

    def log(kind: str, detail: str):
        ws.event(job["id"], kind, detail)

    try:
        tools, notes = await connectors.open_mcp(stack, space, still_running, log)
        call_api, api_notes = connectors.api_tool(still_running, log)
    except Exception as error:  # never let a connector stop the task
        log("tool", f"Connected tools unavailable: {type(error).__name__}")
        return
    state["connector_tools"] = tools + ([call_api] if call_api else [])
    state["connector_notes"] = "\n".join(notes + api_notes)


def out_of_turns(client, gate, tokens: int, job_id: str, state: dict | None = None):
    """When an agent runs out of turns, keep its work: one last tool-free call writes the report. With state (the lead
    agent's), the task is marked so that it continues automatically (see run_job)."""
    async def handler(data):
        if state is not None:
            state["out_of_steps"] = True
        ws.event(job_id, "tool", "Out of steps; writing up what was done")
        writer = Agent(name="wrap-up", model=hub.model("qwen", client, gate), instructions="Write the final report requested.",
                       model_settings=ModelSettings(max_tokens=tokens, include_usage=True, extra_body={"reasoning_effort": "low"}))
        try:
            history = trim_items(replayable(list(data.run_data.history)))
            result = await Runner.run(writer, history + [{"role": "user", "content": WRAP_UP}], max_turns=1)
            return str(result.final_output)
        except Exception as error:  # still return something useful rather than losing the run
            return f"Stopped after using all steps (the write-up failed: {type(error).__name__})."
    return {"max_turns": handler}


# ---------------------------------------------------------------------------------------------
# The agent


# ---------------------------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------------------------
def make_agent(ctx: ToolContext, tools: list) -> Agent:
    profile = ctx.profile
    ws.event(ctx.job_id, "tool", f"Lead on {ctx.lead_model}, helpers on {ctx.helper_model}")
    return Agent(name="cowork", model=AdaptiveModel(ctx.lead_model, ctx.client, ctx.gate, ctx.before), tools=tools,
                 model_settings=settings(ctx, profile["tokens"], profile["effort"], True),
                 instructions=instructions(ctx.job, ctx.space, escalation=ctx.escalation, plan_text=read_plan(ctx.space),
                                           history=recap(ctx.job), phone=ctx.phone, research=ctx.research_status,
                                           connected=ctx.state.get("connector_notes", ""), jev=ctx.state.get("jev", False),
                                           claude_requested=wants_claude(ctx.job.get("task") or "")
                                           and not ctx.state.get("claude_done")))


def build(job: dict, client, gate: asyncio.Semaphore, space: sandbox.Workspace, state: dict | None = None) -> Agent:
    job_id = job["id"]
    budget = CallBudget(job, limit=PROFILES[job["profile"]]["turns"] * 4)
    state = state if state is not None else {}
    if "lead" not in state:
        state["lead"], state["helper"] = assign_gpus(job_id)
    state.setdefault("delegations", 0)

    def before(name: str):
        if state.get("stop"):
            raise StopTask(state["stop"])
        budget.before(name)

    def log(kind: str, detail: str):
        # Status lines read better without the long workspace path the model tends to repeat.
        folder = str(space.dir)
        ws.event(job_id, kind, detail.replace("cd " + folder + " && ", "").replace(folder + "/", "").replace(folder, "."))

    ctx = ToolContext(job=job, space=space, client=client, gate=gate, budget=budget, state=state, images={"count": 0},
                      log=log, before=before, lead_model=state["lead"], helper_model=state["helper"])
    return make_agent(ctx, build_tools(ctx))


async def wrap_up(client, gate, profile: dict, job: dict, session, reason: str) -> str:
    """One tool-free call that turns what a stopped run did into a report for the user."""
    try:
        items = replayable(list(await session.get_items()))
    except Exception:
        items = []
    items = trim_items(items, soft=60000, hard=120000)
    writer = Agent(name="wrap-up", model=hub.model("qwen", client, gate), instructions="Write the report requested.",
                   model_settings=ModelSettings(max_tokens=min(profile["tokens"], 8192), include_usage=True,
                                                extra_body={"reasoning_effort": "low"}))
    try:
        if not items:
            raise ValueError("nothing recorded")
        async with asyncio.timeout(300):
            result = await Runner.run(writer, items + [{"role": "user", "content": STOP_WRAP_UP.format(reason=reason)}], max_turns=1)
        return str(result.final_output)
    except Exception as error:
        events = ws.query("SELECT kind,detail FROM events WHERE job_id=? ORDER BY id", (job["id"],))
        done = [e["detail"] for e in events if e["kind"] in ("tool", "artifact", "delegate")][-15:]
        return (f"(The summary couldn't be written: {type(error).__name__}.) Last actions:\n" +
                "\n".join("- " + describe(d, 140) for d in done))


def auto_continues(job: dict) -> int:
    """How many automatic continuations in a row led up to this task."""
    count, current = 0, job
    while current and str(current.get("task") or "").startswith("AUTOMATIC CONTINUATION") and count < 50:
        count += 1
        rows = ws.query("SELECT * FROM jobs WHERE id=?", (current.get("parent"),)) if current.get("parent") else []
        current = rows[0] if rows else None
    return count


def queue_continuation(job: dict, reason: str) -> str | None:
    """After a time or step limit, start the next part in the same chat by itself (the owner's Cowork tasks only).
    The new task gets a recap of this one (continuity.recap) and the same request."""
    if job.get("skill") != "cowork" or tier_for(job) != "owner":
        return None
    done = auto_continues(job)
    if done >= AUTO_CONTINUE:
        return None
    nxt = ws.create_job(job["project"], f"AUTOMATIC CONTINUATION {done + 1} of up to {AUTO_CONTINUE} (the previous task in "
                        f"this chat stopped because {reason}; carry on from where it stopped).\n\nCURRENT REQUEST:\n"
                        + current_request(job.get("task") or ""), "cowork", job["profile"], bool(job["allow_frontier"]),
                        bool(job["allow_images"]), thread=job.get("thread"), requested_by=job.get("requested_by"),
                        parent=job["id"])
    ws.event(job["id"], "next-phase", nxt)
    return nxt


BRIEF_INLINE_BYTES = 60_000


WHY_CLAUDE = {
    "asked": "The user explicitly asked for Claude in their current request. Do the part of the request they assigned to "
             "Claude; when they gave Claude the whole request, do all of it.",
    "code": "This request is mainly a software job, so the assistant handed it to you. Do all of it: build, run and test "
            "the code, and fix what fails.",
}


def handoff_brief(job: dict, space: sandbox.Workspace, extra: str, why: str = "asked") -> str:
    """Everything Claude Code needs when the request goes to it: the chat so far, the request, the plan."""
    parts = [job.get("task") or ""]
    if extra:
        parts.append(extra.strip())
    plan = read_plan(space)
    if plan:
        parts.append(f"PROJECT PLAN ({PLAN_FILE} in this folder):\n{plan}")
    earlier = recap(job)
    if earlier:
        parts.append(earlier)
    parts.append(WHY_CLAUDE[why] + " Files the user attached are in uploads/. Save deliverables as files in this folder.")
    brief = "\n\n".join(parts)
    if len(brief.encode("utf-8")) <= BRIEF_INLINE_BYTES:
        return brief
    # A command-line argument can't hold a long chat: the whole brief goes in a file, the request stays inline.
    space.write_text(".claude-brief.md", brief)
    return ("The full brief for this request (the conversation so far, the request, the project plan and earlier work) "
            "is in .claude-brief.md in this folder: read it first.\n\nCURRENT REQUEST:\n"
            + current_request(job.get("task") or "")[-20000:] + "\n\n" + parts[-1])


async def direct_claude(job: dict, space: sandbox.Workspace, state: dict, extra: str) -> dict | None:
    """Hand the request to Claude Code first when the user asked for Claude or (decided by Jev) it's mainly a coding
    job, rather than leaving that to Qwen's judgment. Without Jev, only an explicit request for Claude counts."""
    task = job.get("task") or ""
    if not (space.is_owner and job.get("allow_frontier")) or is_automatic(task):
        return None
    from . import judge
    if judge.enabled(job):
        why, _ = await judge.claude_route(job)
    else:
        why = "asked" if wants_claude(task) else None
    state["claude_route"] = why
    if why is None:
        return None
    if escalate.available("claude") is not None:
        if why == "asked":
            ws.event(job["id"], "tool", "You asked for Claude, but " + escalate.available("claude"))
        return None
    ws.event(job["id"], "tool", "Handing your request to Claude Code " +
             ("(you asked for Claude)" if why == "asked" else "(it's mainly a coding job)"))
    result = await escalate.run("claude", space, job["id"], handoff_brief(job, space, extra, why))
    state["claude_done"] = True
    return result


def shared_files(job_id: str) -> list[str]:
    return [r["name"] for r in ws.query("SELECT name FROM artifacts WHERE job_id=? ORDER BY created_at,rowid", (job_id,))]


async def continue_after(job: dict, reason: str, report: str) -> bool:
    """Queue the next part after a limit, unless Jev reads the report as finished (or blocked on the user)."""
    from . import judge
    done = await judge.finished(job, report)
    if done is not None and done >= 0.8:
        ws.event(job["id"], "tool", "The work looks finished; not continuing automatically")
        return False
    return bool(queue_continuation(job, reason))


def log_usage(job_id: str, result) -> None:
    usage = result.context_wrapper.usage
    ws.event(job_id, "usage", json.dumps({"requests": usage.requests, "input_tokens": usage.input_tokens,
                                          "output_tokens": usage.output_tokens}))


async def run_job(job: dict, gate: asyncio.Semaphore | None = None) -> str:
    gate = gate or asyncio.Semaphore(4)
    profile = PROFILES[job["profile"]]
    space = sandbox.Workspace(tier_for(job), job.get("thread") or "console").prepare()
    client = hub.async_client()
    session = SQLiteSession(f"{job['id']}-attempt-{job['attempts']}", db_path=store.DATA / "sessions.db")
    state: dict = {}
    connections = AsyncExitStack()
    task_input = job["task"]
    reasoning_token = reasoning.begin(job["id"])
    try:
        if space.is_owner:
            await open_connectors(job, space, state, connections)
        state["lead"], state["helper"] = assign_gpus(job["id"])

        def still_running():
            rows = ws.query("SELECT status FROM jobs WHERE id=?", (job["id"],))
            if not rows or rows[0]["status"] != "running":
                raise RuntimeError("This task is no longer running")

        # Questions before starting a new request (decided on the helper GPU; the clock hasn't started yet).
        if job.get("skill") == "cowork" and job.get("attempts", 1) == 1:
            from .kickoff import clarify
            task_input += await clarify(job, client, gate, state["helper"], still_running)
        # The user asked for Claude: it does that work first; Qwen then checks, shares and reports.
        handed = await direct_claude(job, space, state, task_input[len(job["task"]):])
        if handed is not None:
            if not handed.get("ok"):
                why = handed.get("error") or ("it timed out" if handed.get("timed_out") else
                                              f"it exited with code {handed.get('exit_code')}")
                ws.event(job["id"], "partial", f"the Claude Code hand-off failed ({why})")
                return (f"⚠️ **Claude Code couldn't do this: {why}.**\n\n" + (handed.get("summary") or "").strip()[:3000]
                        + "\n\nSend the request again to retry with Claude, or tell me to do it myself instead.")
            task_input += ("\n\nCLAUDE CODE HAS ALREADY WORKED ON THIS REQUEST (" +
                           ("you asked for Claude" if state.get("claude_route") == "asked" else "it's mainly a coding job") +
                           ", so it was handed over first). Its report:\n" + (handed.get("summary") or "(no summary)") +
                           "\n\nNow check the files it made (list_files, open or run them), share the deliverables "
                           "with share_file, finish any part of the request the user did NOT give to Claude, and reply. "
                           "If its work is broken or incomplete, send the fixes back to it with ask_claude rather than "
                           "redoing its part yourself.")
        try:
            async with asyncio.timeout(profile["seconds"]) as timer:
                state["timer"] = timer  # ask_user and hand-offs move the deadline by the time spent waiting
                agent = build(job, client, gate, space, state)
                handlers = out_of_turns(client, gate, profile["tokens"], job["id"], state)
                result = await Runner.run(agent, task_input, max_turns=profile["turns"], session=session,
                                          run_config=RUN_CONFIG, error_handlers=handlers)
                log_usage(job["id"], result)
                answer = str(result.final_output)
                # Jev checks the reply against the request; one fix round when it falls short (Cowork only).
                if job.get("skill") == "cowork" and not state.get("next") and not state.get("out_of_steps"):
                    from . import judge
                    gap = await judge.reply_gap(job, answer, shared_files(job["id"]))
                    if gap:
                        ws.event(job["id"], "tool", "Quality check: the reply falls short; finishing the missing parts")
                        result = await Runner.run(agent, gap, max_turns=max(10, profile["turns"] // 3), session=session,
                                                  run_config=RUN_CONFIG, error_handlers=handlers)
                        log_usage(job["id"], result)
                        answer = str(result.final_output)
        except Exception as error:
            if state.get("stop"):
                reason, header = state["stop"], f"⚠️ **Stopped: {state['stop']}.**"
                ws.event(job["id"], "partial", reason)
            elif isinstance(error, TimeoutError):
                minutes = profile["seconds"] // 60
                reason = f"the {minutes}-minute time limit was reached"
                ws.event(job["id"], "partial", reason)
                ws.event(job["id"], "tool", "Writing up what was done")
                report = await wrap_up(client, gate, profile, job, session, reason)
                if await continue_after(job, reason, report):
                    header = f"⏱ **Reached the {minutes}-minute limit for one task; the next part has started automatically.**"
                else:
                    header = (f"⏱ **Stopped at the {minutes}-minute time limit.** Reply **continue** to keep going: the "
                              "next task picks up from here.")
                return header + "\n\n" + report
            else:
                raise
            ws.event(job["id"], "tool", "Writing up what was done")
            return header + "\n\n" + await wrap_up(client, gate, profile, job, session, reason)
        if state.get("out_of_steps") and not state.get("next"):
            reason = f"it used all {profile['turns']} steps"
            ws.event(job["id"], "partial", reason)
            if await continue_after(job, reason, answer):
                answer = "🔁 **Used all steps for one task; the next part has started automatically.**\n\n" + answer
            return answer
        if state.get("next"):
            nxt = ws.create_job(job["project"], "AUTOMATIC NEXT PHASE (queued by the previous task in this chat; the "
                                f"project plan is in {PLAN_FILE}).\n\nCURRENT REQUEST:\n" + state["next"], "cowork",
                                job["profile"], bool(job["allow_frontier"]), bool(job["allow_images"]), thread=job.get("thread"),
                                requested_by=job.get("requested_by"), parent=job["id"])
            ws.event(job["id"], "next-phase", nxt)
        return answer
    finally:
        reasoning.end(reasoning_token)
        cancel_background(state)
        release_gpus(job["id"])
        try:
            await connections.aclose()  # the owner's MCP servers for this task
        except Exception as error:
            print(f"[cowork] closing MCP servers failed: {type(error).__name__}", flush=True)
        session.close()
        await client.close()
