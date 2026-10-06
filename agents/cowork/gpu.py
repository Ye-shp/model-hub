"""Balancing task leads (and their helpers) across the two GPUs."""
from __future__ import annotations

from .config import HELPER_MODEL, LEAD_MODEL, RESIDENTS

_leads: dict[str, str] = {}  # job id -> resident leading it (all jobs run in this one controller process)
_last: list[str] = []        # the resident that led the most recent task, so ties alternate between the GPUs


def assign_gpus(job_id: str) -> tuple[str, str]:
    """(lead, helper) models for a new task: the lead goes to the GPU with fewer leads right now and, when both are
    equal (the usual case: one task at a time), to the one that didn't lead the previous task. Helpers get the other."""
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


def release_gpus(job_id: str) -> None:
    _leads.pop(job_id, None)
