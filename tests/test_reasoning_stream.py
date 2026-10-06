"""Reasoning survives the real SDK stream without changing results or crossing jobs."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))

from agents import Agent, ModelSettings, Runner, function_tool
from openai import AsyncOpenAI
import httpx2 as httpx
import cowork
import hub
import reasoning
import store
import workspace as ws
import sandbox


def chunk(delta, finish=None):
    return {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "qwen-1",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def line(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


def response(deltas, finish="stop"):
    events = [chunk(delta) for delta in deltas] + [chunk({}, finish),
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "qwen-1", "choices": [],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=b"".join(line(event) for event in events) + b"data: [DONE]\n\n")


def client(handler):
    return AsyncOpenAI(api_key="test", base_url="https://fake.invalid/v1", max_retries=0,
                       http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def agent(fake, tools=()):
    return Agent(name="test", model=hub.model("qwen-1", fake), instructions="Answer the user.", tools=list(tools),
                 model_settings=ModelSettings(max_tokens=200, include_usage=True))


class ReasoningStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = store.DATA
        store.DATA = Path(self.temp.name) / "data"
        ws.init()

    def tearDown(self):
        store.DATA = self.old_data
        self.temp.cleanup()

    def thoughts(self, job):
        return [row["detail"] for row in ws.query("SELECT detail FROM events WHERE job_id=? AND kind='reasoning' ORDER BY id", (job,))]

    def test_storage_imports_honor_the_configured_data_directory(self):
        folder = Path(self.temp.name) / "env-check"
        folder.mkdir()
        copy = folder / "hub.py"
        copy.write_text((ROOT / "agents" / "hub.py").read_text(encoding="utf-8"), encoding="utf-8")
        expected = folder / "private-data"
        (folder / ".env").write_text(f"HUB_DATA_DIR={expected}\n", encoding="utf-8")
        env = dict(os.environ)
        env.pop("HUB_DATA_DIR", None)
        script = ("import importlib.util,json,sys; sys.path.insert(0,sys.argv[1]); "
                  "s=importlib.util.spec_from_file_location('env_hub',sys.argv[2]); "
                  "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                  "import store; print(json.dumps(str(store.DATA)))")
        result = subprocess.run([sys.executable, "-c", script, str(ROOT / "agents"), str(copy)],
                                env=env, capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(Path(json.loads(result.stdout)), expected)

    async def test_sdk_reasoning_both_formats_keeps_answer_tools_and_usage(self):
        calls = []

        @function_tool
        def ping() -> str:
            """Check a local value."""
            calls.append("ping")
            return "pong"

        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return response([{"role": "assistant", "reasoning_content": "First thinking. "},
                    {"reasoning_content": "I should check."},
                    {"tool_calls": [{"index": 0, "id": "ping-1", "type": "function",
                                      "function": {"name": "ping", "arguments": "{}"}}]}], "tool_calls")
            return response([{"role": "assistant", "reasoning": "Second thinking."}, {"content": "Answer."}])

        async with client(handler) as fake:
            token = reasoning.begin("job-tools")
            try:
                result = await Runner.run(agent(fake, [ping]), "check", max_turns=3)
            finally:
                reasoning.end(token)
        text = "".join(self.thoughts("job-tools"))
        self.assertIn("First thinking. I should check.", text)
        self.assertIn("Second thinking.", text)
        self.assertIn("Qwen 1", text)
        self.assertEqual(result.final_output, "Answer.")
        self.assertEqual(calls, ["ping"])
        usage = result.context_wrapper.usage
        self.assertEqual((usage.requests, usage.input_tokens, usage.output_tokens, usage.total_tokens), (2, 20, 10, 30))

    async def test_concurrent_job_contexts_and_inherited_helpers_are_isolated(self):
        async def task(job, secret):
            async def handler(request):
                await asyncio.sleep(.01)
                return response([{"role": "assistant", "reasoning_content": secret}, {"content": "Done."}])
            async with client(handler) as fake:
                token = reasoning.begin(job)
                try:
                    results = await asyncio.gather(Runner.run(agent(fake), "lead"), Runner.run(agent(fake), "helper"))
                finally:
                    reasoning.end(token)
            return [result.final_output for result in results]
        self.assertEqual(await asyncio.gather(task("chat-a", "ONLY-A"), task("chat-b", "ONLY-B")),
                         [["Done.", "Done."], ["Done.", "Done."]])
        for job, own, other in [("chat-a", "ONLY-A", "ONLY-B"), ("chat-b", "ONLY-B", "ONLY-A")]:
            text = "".join(self.thoughts(job))
            self.assertEqual(text.count(own), 2)
            self.assertNotIn(other, text)
        # Resetting the context prevents later calls outside a job from being recorded.
        stray = reasoning.buffer("qwen-1")
        stray.append("OUTSIDE")
        stray.close()
        self.assertNotIn("OUTSIDE", "".join(self.thoughts("chat-a")))

    async def test_small_deltas_are_batched_live_and_large_chunks_are_bounded(self):
        token = reasoning.begin("bounded")
        try:
            with patch.object(reasoning, "FLUSH_SECONDS", .01):
                buffer = reasoning.buffer("qwen-1")
                for _ in range(100):
                    buffer.append("small ")
                self.assertEqual(self.thoughts("bounded"), [])
                await asyncio.sleep(.03)
                self.assertIn("small " * 100, "".join(self.thoughts("bounded")))
                buffer.append("L" * 5000)
                buffer.close()
        finally:
            reasoning.end(token)
        rows = self.thoughts("bounded")
        self.assertLess(len(rows), 10)
        self.assertTrue(all(len(row) <= reasoning.CHUNK_CHARACTERS for row in rows))
        self.assertEqual("".join(rows).count("L"), 5000)

    async def test_job_limit_is_shared_by_helpers_and_marks_truncation_once(self):
        token = reasoning.begin("limited")
        try:
            with patch.object(reasoning, "JOB_CHARACTERS", 3000):
                lead, helper = reasoning.buffer("qwen-1"), reasoning.buffer("qwen-2")
                lead.append("A" * 2500)
                lead.close()
                helper.append("B" * 8000)
                helper.close()
                late = reasoning.buffer("qwen-1")
                late.append("C" * 8000)
                late.close()
        finally:
            reasoning.end(token)
        rows = self.thoughts("limited")
        text = "".join(rows)
        self.assertEqual(text.count(reasoning.TRUNCATED), 1)
        self.assertEqual(len(text), 3000 + len(reasoning.TRUNCATED))
        self.assertNotIn("C", text)
        self.assertTrue(all(len(row) <= reasoning.CHUNK_CHARACTERS for row in rows))

    async def test_failed_sdk_stream_flushes_partial_thinking(self):
        class FailingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield line(chunk({"role": "assistant", "reasoning_content": "Partial before failure."}))
                raise httpx.ReadError("stream interrupted")
        async def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=FailingStream())
        async with client(handler) as fake:
            token = reasoning.begin("failed")
            try:
                with self.assertRaises(Exception):
                    await Runner.run(agent(fake), "test")
                # StreamingModel's finally flushes before the enclosing scope ends.
                self.assertIn("Partial before failure.", "".join(self.thoughts("failed")))
            finally:
                reasoning.end(token)

    async def test_cancelled_sdk_stream_flushes_and_closed_job_rejects_background_writes(self):
        waiting = asyncio.Event()
        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield line(chunk({"role": "assistant", "reasoning_content": "Partial before cancel."}))
                waiting.set()
                await asyncio.Event().wait()
        async def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=WaitingStream())
        async with client(handler) as fake:
            token = reasoning.begin("cancelled")
            background = reasoning.buffer("qwen-2")
            try:
                task = asyncio.create_task(Runner.run(agent(fake), "test"))
                await waiting.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIn("Partial before cancel.", "".join(self.thoughts("cancelled")))
            finally:
                reasoning.end(token)
            before = self.thoughts("cancelled")
            background.append("LATE BACKGROUND THOUGHT")
            background.close()
            self.assertEqual(self.thoughts("cancelled"), before)

    async def test_progress_write_failure_does_not_break_model_or_result(self):
        async def handler(request):
            return response([{"role": "assistant", "reasoning_content": "Thinking."}, {"content": "Done."}])
        async with client(handler) as fake:
            token = reasoning.begin("write-error")
            try:
                with patch.object(ws, "event", side_effect=RuntimeError("progress unavailable")):
                    result = await Runner.run(agent(fake), "test")
            finally:
                reasoning.end(token)
        self.assertEqual(result.final_output, "Done.")

    async def test_chat_job_enables_capture_and_resets_after_completion(self):
        import toolbox
        old_root, old_tools = sandbox.ROOT, toolbox.TOOLS_DIR
        sandbox.ROOT, toolbox.TOOLS_DIR = Path(self.temp.name) / "chats", Path(self.temp.name) / "tools"
        async def handler(request):
            return response([{"role": "assistant", "reasoning_content": "Chat job thinking."}, {"content": "Done."}])
        try:
            ws.create_job("default", "CURRENT REQUEST:\nhello", "chat", "fast", thread="chat-one")
            job = ws.claim_job()
            fake = client(handler)
            with patch.object(hub, "async_client", return_value=fake), patch("escalate.available", return_value="off"), \
                    patch("jev.available", return_value="off"):
                self.assertEqual(await cowork.run_job(job), "Done.")
            self.assertIn("Chat job thinking.", "".join(self.thoughts(job["id"])))
            outside = reasoning.buffer("qwen-1")
            outside.append("AFTER RUN")
            outside.close()
            self.assertNotIn("AFTER RUN", "".join(self.thoughts(job["id"])))
        finally:
            sandbox.ROOT, toolbox.TOOLS_DIR = old_root, old_tools


if __name__ == "__main__":
    unittest.main()
