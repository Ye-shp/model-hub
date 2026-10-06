"""What the user's message asks for, read with plain rules rather than left to the model's judgment."""
from __future__ import annotations

import re

from .continuity import current_request

# "use Claude", "have claude code build…", "ask Claude to…", "give this to claude", "Claude should…", "with Claude".
# Words like "about", "than" or "vs" between the verb and the name mean Claude is the topic, not the worker.
_CLAUDE = re.compile(
    r"\b(?:use|using|ask|have|let|get|call|delegate|route|hand|give|send|pass)\b"
    r"(?:\s+(?!about\b|than\b|vs\b|versus\b|like\b|on\b|of\b)[\w'/-]+){0,3}?\s+(?:to\s+)?claude\b"
    r"|\b(?:with|via|through|by|to|in)\s+claude\b"
    r"|\bclaude(?:\s+code)?\s*,?\s*(?:should|can|could|will|to|needs?\s+to|must|please|do|build|write|fix|make|handle|"
    r"review|take|code|implement|refactor|debug|draft|edit)\b",
    re.IGNORECASE)
_NOT_CLAUDE = re.compile(r"\b(?:don'?t|do\s+not|never|without|no)\b[^.!?\n]{0,25}\bclaude\b", re.IGNORECASE)

_SKIP_QUESTIONS = re.compile(
    r"\b(?:just\s+do\s+it|just\s+go|no\s+questions|don'?t\s+ask|do\s+not\s+ask|skip\s+(?:the\s+)?questions|"
    r"use\s+your\s+(?:best\s+)?judg(?:e)?ment|surprise\s+me|whatever\s+you\s+think)\b", re.IGNORECASE)
_CONTINUE = re.compile(r"^\s*(?:continue|keep\s+going|go\s+on|carry\s+on|resume|proceed|go\s+ahead|next(?:\s+phase)?|"
                       r"do\s+it|yes|yep|ok(?:ay)?|sure|status)\W*$", re.IGNORECASE)
AUTOMATIC = ("AUTOMATIC NEXT PHASE", "AUTOMATIC CONTINUATION")


def wants_claude(task: str) -> bool:
    """The user's current message explicitly asks for Claude (Claude Code) to do the work."""
    request = current_request(task or "")
    return bool(_CLAUDE.search(request)) and not _NOT_CLAUDE.search(request)


def is_automatic(task: str) -> bool:
    """A task the controller queued itself (next phase or continuation after a limit), not a new message."""
    return (task or "").lstrip().startswith(AUTOMATIC)


def may_ask_first(task: str) -> bool:
    """Whether a kickoff question round makes sense: a fresh request that doesn't say to just get on with it."""
    if is_automatic(task):
        return False
    request = current_request(task or "").strip()
    if len(request) < 12 or _CONTINUE.match(request) or _SKIP_QUESTIONS.search(request):
        return False
    return True
