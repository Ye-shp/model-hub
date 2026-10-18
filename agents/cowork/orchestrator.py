"""GLM as the lead (orchestrator) of the owner's Cowork tasks; the Qwens on both GPUs do the work.

GLM is called directly (glm.py holds the key, the endpoint and the monthly meter), not through the gateway. If GLM
can't be used for a call (no key, budget used up, the API failing), the task carries on with Qwen leading, so a
GLM outage never stops a task.
"""
from __future__ import annotations

import dataclasses
import os

import openai
from agents import RunConfig
from agents.exceptions import ModelBehaviorError

try:
    import httpx2 as httpx
except ImportError:  # older openai releases use httpx
    import httpx

import glm
import hub
import workspace as ws

from .config import ORCHESTRATOR_HARD_CHARS, ORCHESTRATOR_SKILLS, ORCHESTRATOR_SOFT_CHARS
from .context import trim_items
from .effort import AdaptiveModel, effort_for


def enabled(job: dict, is_owner: bool) -> bool:
    """GLM leads the owner's tasks in the configured modes (Cowork by default) when it is connected and in budget."""
    return bool(is_owner and job.get("allow_frontier") and (job.get("skill") or "cowork") in ORCHESTRATOR_SKILLS
                and glm.available() is None)


def _context_filter(data):
    from agents.run_config import ModelInputData
    return ModelInputData(input=trim_items(list(data.model_data.input), ORCHESTRATOR_SOFT_CHARS, ORCHESTRATOR_HARD_CHARS),
                          instructions=data.model_data.instructions)


RUN_CONFIG = RunConfig(call_model_input_filter=_context_filter)


# GLM thinks without limit unless told otherwise (5,000+ tokens for one planning step, which ran past the output cap
# and left no reply). reasoning_effort keeps it to a few hundred: "high" for planning and reviewing, "low" right after
# it only looked at something (effort.py).
EFFORT = os.environ.get("GLM_REASONING_EFFORT", "high")


def _out_of_tokens(error: BaseException) -> bool:
    """The reply ran out of output tokens while still thinking: no text and no tool call."""
    return isinstance(error, ModelBehaviorError) and "finish_reason='length'" in str(error)


def _is_api_failure(error: BaseException) -> bool:
    if isinstance(error, (openai.APIError, httpx.HTTPError, TimeoutError)):
        return True
    return isinstance(error, RuntimeError) and str(error).startswith("Model stream ended")


class OrchestratorModel(AdaptiveModel):
    """GLM for every lead call while it works; Qwen (the least busy resident) once it doesn't."""

    def __init__(self, hub_client, gate, before_call, job_id: str, state: dict):
        super().__init__("qwen", hub_client, gate, before_call)
        self.job_id, self.state = job_id, state
        self.glm = hub.StreamingModel(glm.MODEL, glm.client(), gate, before_call)

    def _fall_back(self, reason: str) -> None:
        if not self.state.get("glm_off"):
            self.state["glm_off"] = reason
            ws.event(self.job_id, "tool", f"GLM is unavailable ({reason}); Qwen leads the rest of this task")

    async def get_response(self, system_instructions, input, model_settings, *args, **kwargs):
        if not self.state.get("glm_off"):
            reason = glm.available()
            if reason is not None:
                self._fall_back(reason)
        if self.state.get("glm_off"):
            return await super().get_response(system_instructions, input, model_settings, *args, **kwargs)
        # GLM gets its own output limit and thinking effort (no Qwen-specific settings).
        response, effort = None, effort_for(input, EFFORT)
        while response is None:
            settings = dataclasses.replace(model_settings, extra_body={"reasoning_effort": effort}, max_tokens=glm.MAX_TOKENS)
            try:
                response = await self.glm.get_response(system_instructions, input, settings, *args, **kwargs)
            except Exception as error:
                if _out_of_tokens(error) and effort != "low":
                    ws.event(self.job_id, "tool", "GLM ran out of room while thinking; asking again with less thinking")
                    effort = "low"
                    continue
                if _out_of_tokens(error):  # only this step goes to Qwen; GLM keeps leading the rest
                    ws.event(self.job_id, "tool", "GLM couldn't fit this step; Qwen takes this one step")
                elif _is_api_failure(error):
                    detail = getattr(error, "message", None) or str(error) or type(error).__name__
                    self._fall_back(f"{type(error).__name__}: {str(detail)[:200]}")
                else:
                    raise  # a stopped task, an exhausted call budget: not GLM's fault
                return await super().get_response(system_instructions, input, model_settings, *args, **kwargs)
        usage = response.usage
        cached = getattr(usage.input_tokens_details, "cached_tokens", 0) or 0
        spent = glm.record(usage.input_tokens, cached, usage.output_tokens)
        self.state["glm_cost"] = self.state.get("glm_cost", 0.0) + spent
        self.state["glm_calls"] = self.state.get("glm_calls", 0) + 1
        return response


def cost_note(state: dict) -> str | None:
    """One line for the task's status: what GLM cost on this task and so far this month."""
    if not state.get("glm_calls"):
        return None
    month = glm.this_month()
    return (f"GLM: {state['glm_calls']} calls, ${state.get('glm_cost', 0.0):.4f} on this task "
            f"(${month['usd']:.2f} of ${glm.budget():.2f} this month)")
