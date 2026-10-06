"""Qwen Cowork: type a goal, qwen-1 plans it and does the work with tools on this box.

Tools: a shell and files in the chat's own workspace, web search and reading, parallel helpers on
the other GPU, image generation, project memory and collected posts, the owner's phone (through the
phone bridge), and hand-off to Claude Code or Codex for work it can't do well. Every tool call is
logged as a job event, which the chat site shows as live status.

Long work: old tool output is trimmed before each model call so a long run doesn't overflow the
context; a task that stops early (time limit, failed hand-off) still writes a report; the next task
in the same chat gets a recap of what the stopped one did; and big projects keep plan.md in the chat
folder and can queue their next phase automatically.

This package was split out of a single cowork.py; every name below is still reachable as cowork.NAME.
"""
from __future__ import annotations

from .config import (ACTION_KINDS, AUTO_CONTINUE, HARD_CHARS, HELPER_MODEL, LEAD_MODEL, MAX_CHAIN, MAX_IMAGES, MAX_PARALLEL_HELPERS,  # noqa: F401
                     PLAN_FILE, PLAN_LIMIT, PROFILES, RESEARCH_GUIDE, RESIDENTS, SHARE_LIMIT, SOFT_CHARS, TOOLBOX)
from .gpu import _last, _leads, assign_gpus, release_gpus  # noqa: F401
from .effort import READ_ONLY, AdaptiveModel, _failed, _last_tool_names, effort_for  # noqa: F401
from .tier import tier_for  # noqa: F401
from .context import (LEVELS, RUN_CONFIG, _cut, _shrink_arguments, _shrink_output, _size, context_filter,  # noqa: F401
                      describe, replayable, trim_items)
from .continuity import _attempt_summary, chain_depth, current_request, read_plan, recap  # noqa: F401
from .prompt import STOP_WRAP_UP, TRUST, WRAP_UP, instructions  # noqa: F401
from .tools import StopTask, ToolContext, build_tools, make_helper, settings  # noqa: F401
from .intent import is_automatic, may_ask_first, wants_claude  # noqa: F401
from .runner import build, out_of_turns, run_job, wrap_up  # noqa: F401
