"""Building the agent and running a task."""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack

from agents import Agent, ModelSettings, Runner, SQLiteSession

import hub
import sandbox
import store
import workspace as ws
from crew import CallBudget

from .config import PLAN_FILE, PROFILES
from .context import RUN_CONFIG, describe, replayable, trim_items
from .continuity import read_plan, recap
from .effort import AdaptiveModel
from .gpu import assign_gpus, release_gpus
from .prompt import STOP_WRAP_UP, WRAP_UP, instructions
from .tier import tier_for
from .tools import StopTask, ToolContext, build_tools, settings


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


def out_of_turns(client, gate, tokens: int, job_id: str):
    """When an agent runs out of turns, keep its work: one last tool-free call writes the report."""
    async def handler(data):
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
                                           connected=ctx.state.get("connector_notes", ""), jev=ctx.state.get("jev", False)))


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


async def run_job(job: dict, gate: asyncio.Semaphore | None = None) -> str:
    gate = gate or asyncio.Semaphore(4)
    profile = PROFILES[job["profile"]]
    space = sandbox.Workspace(tier_for(job), job.get("thread") or "console").prepare()
    client = hub.async_client()
    session = SQLiteSession(f"{job['id']}-attempt-{job['attempts']}", db_path=store.DATA / "sessions.db")
    state: dict = {}
    connections = AsyncExitStack()
    try:
        if space.is_owner:
            await open_connectors(job, space, state, connections)
        try:
            async with asyncio.timeout(profile["seconds"]) as timer:
                state["timer"] = timer  # ask_user moves the deadline by the time spent waiting for the user
                result = await Runner.run(build(job, client, gate, space, state), job["task"], max_turns=profile["turns"],
                                          session=session, run_config=RUN_CONFIG,
                                          error_handlers=out_of_turns(client, gate, profile["tokens"], job["id"]))
            usage = result.context_wrapper.usage
            ws.event(job["id"], "usage", json.dumps({"requests": usage.requests, "input_tokens": usage.input_tokens,
                                                     "output_tokens": usage.output_tokens}))
            answer = str(result.final_output)
        except Exception as error:
            if state.get("stop"):
                reason, header = state["stop"], f"⚠️ **Stopped: {state['stop']}.**"
            elif isinstance(error, TimeoutError):
                reason = f"the {profile['seconds'] // 60}-minute time limit was reached"
                header = (f"⏱ **Stopped at the {profile['seconds'] // 60}-minute time limit.** Reply **continue** to keep "
                          "going: the next task picks up from here.")
            else:
                raise
            ws.event(job["id"], "partial", reason)
            ws.event(job["id"], "tool", "Writing up what was done")
            return header + "\n\n" + await wrap_up(client, gate, profile, job, session, reason)
        if state.get("next"):
            nxt = ws.create_job(job["project"], "AUTOMATIC NEXT PHASE (queued by the previous task in this chat; the "
                                f"project plan is in {PLAN_FILE}).\n\nCURRENT REQUEST:\n" + state["next"], "cowork",
                                job["profile"], bool(job["allow_frontier"]), bool(job["allow_images"]), thread=job.get("thread"),
                                requested_by=job.get("requested_by"), parent=job["id"])
            ws.event(job["id"], "next-phase", nxt)
        return answer
    finally:
        release_gpus(job["id"])
        try:
            await connections.aclose()  # the owner's MCP servers for this task
        except Exception as error:
            print(f"[cowork] closing MCP servers failed: {type(error).__name__}", flush=True)
        session.close()
        await client.close()
