"""Bounded, live reasoning progress for the current Cowork job.

The observer belongs to an async task's context, rather than a module-global job
ID. Helpers inherit their parent's observer; concurrent chats inherit their own.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token

import workspace as ws

CHUNK_CHARACTERS = 1800  # workspace.event keeps at most 2,000 characters
BUFFER_CHARACTERS = 1600  # leaves room for a model label
FLUSH_SECONDS = 1.0
JOB_CHARACTERS = 100_000
TRUNCATED = "\n\n[Further thinking omitted from this display to keep the chat responsive.]\n\n"
REASONING_DELTAS = {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}


class Observer:
    def __init__(self, job: str):
        self.job = job
        self.characters = 0
        self.truncated = False
        self.closed = False
        self.active_stream = None
        self.buffers: set[Buffer] = set()

    def _write(self, text: str) -> None:
        try:
            ws.event(self.job, "reasoning", text)
        except Exception:
            # A progress display must not break the model call or its cleanup.
            pass

    def emit(self, stream: Buffer, text: str) -> None:
        if self.closed or self.truncated or not text:
            return
        prefix = ""
        if self.active_stream is not stream:
            self.active_stream = stream
            label = {"qwen": "Qwen", "qwen-1": "Qwen 1", "qwen-2": "Qwen 2"}.get(stream.model, "Model")
            prefix = f"\n\n**{label}:**\n\n"
        text = prefix + text
        remaining = max(0, JOB_CHARACTERS - self.characters)
        displayed = text[:remaining]
        for start in range(0, len(displayed), CHUNK_CHARACTERS):
            self._write(displayed[start:start + CHUNK_CHARACTERS])
        self.characters += len(displayed)
        if len(text) > remaining:
            self.truncated = True
            self._write(TRUNCATED)

    def close(self) -> None:
        for buffer in list(self.buffers):
            buffer.close()
        self.closed = True  # background helpers cannot append after the job ends


class Buffer:
    def __init__(self, observer: Observer | None, model: str):
        self.observer = observer
        self.model = model
        self.text = ""
        self.timer: asyncio.TimerHandle | None = None
        self.closed = False
        if observer and not observer.closed:
            observer.buffers.add(self)

    def append(self, text: str) -> None:
        if self.closed or not self.observer or self.observer.closed or self.observer.truncated:
            return
        if not isinstance(text, str) or not text:
            return
        # Consume oversized provider deltas in slices: never buffer the whole
        # stream or write a row per token.
        for start in range(0, len(text), BUFFER_CHARACTERS):
            self.text += text[start:start + BUFFER_CHARACTERS]
            while len(self.text) >= BUFFER_CHARACTERS:
                piece, self.text = self.text[:BUFFER_CHARACTERS], self.text[BUFFER_CHARACTERS:]
                self.observer.emit(self, piece)
                if self.observer.truncated:
                    self.text = ""
                    return
        if self.text and self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(FLUSH_SECONDS, self.flush)

    def flush(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        text, self.text = self.text, ""
        if self.observer:
            self.observer.emit(self, text)

    def close(self) -> None:
        if self.closed:
            return
        self.flush()  # keep the last partial thought even after an error/cancel
        self.closed = True
        if self.observer:
            self.observer.buffers.discard(self)


_current: ContextVar[Observer | None] = ContextVar("cowork_reasoning", default=None)


def begin(job: str) -> Token:
    return _current.set(Observer(job))


def end(token: Token) -> None:
    observer = _current.get()
    try:
        if observer:
            observer.close()
    finally:
        _current.reset(token)


def buffer(model: str) -> Buffer:
    return Buffer(_current.get(), model)
