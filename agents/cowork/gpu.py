"""Balancing task leads (and their helpers) across the two GPUs."""
from __future__ import annotations

from .config import HELPER_MODEL, LEAD_MODEL, RESIDENTS

ORCHESTRATOR = "glm"  # a lead that runs off the box (glm.py): it takes no GPU, so both GPUs serve its helpers

_leads: dict[str, str] = {}  # job id -> resident leading it (all jobs run in this one controller process)
_last: list[str] = []        # the resident that led the most recent task, so ties alternate between the GPUs
_helpers: dict[str, int] = {}  # resident -> helpers running on it right now (all tasks)


def assign_gpus(job_id: str, orchestrated: bool = False) -> tuple[str, str]:
    """(lead, helper) models for a new task: the lead goes to the GPU with fewer leads right now and, when both are
    equal (the usual case: one task at a time), to the one that didn't lead the previous task. Helpers get the other.
    With GLM orchestrating, the lead is GLM and the helper is just the first choice: helpers go to both GPUs."""
    if orchestrated:
        return ORCHESTRATOR, least_busy(1)[0]
    if LEAD_MODEL != "auto":
        lead = LEAD_MODEL
    else:
        counts = {m: 0 for m in RESIDENTS}
        for model in _leads.values():
            counts[model] = counts.get(model, 0) + 1
        previous = _last[0] if _last else None
        lead = min(RESIDENTS, key=lambda m: (counts[m], m == previous, RESIDENTS.index(m)))
    helper = HELPER_MODEL if HELPER_MODEL != "auto" else next(m for m in RESIDENTS if m != lead) if lead in RESIDENTS else "qwen-2"
    _leads[job_id] = lead
    _last[:] = [lead]
    return lead, helper


def least_busy(count: int) -> list[str]:
    """Residents for `count` new helpers, each going to the GPU with the fewest leads and helpers at that moment."""
    load = {m: _helpers.get(m, 0) + 2 * sum(1 for lead in _leads.values() if lead == m) for m in RESIDENTS}
    picks = []
    for _ in range(count):
        model = min(RESIDENTS, key=lambda m: (load[m], RESIDENTS.index(m)))
        load[model] += 1
        picks.append(model)
    return picks


def helper_started(model: str) -> None:
    _helpers[model] = _helpers.get(model, 0) + 1


def helper_finished(model: str) -> None:
    _helpers[model] = max(0, _helpers.get(model, 0) - 1)


def release_gpus(job_id: str) -> None:
    _leads.pop(job_id, None)
