"""Conversation identity and native thought delivery through the actual chat Pipe."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
from integrations.openwebui_cowork import Pipe, assistant_history

OWNER = {"role": "admin", "id": "owner", "email": "owner@example.test"}


class ChatPipeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pipe = Pipe()
        self.pipe.valves.OWNER_KEY = "test-owner-key-" + "k" * 32
        self.calls = []

        async def call(client, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            if path == "/api/thread/active":
                return {"job": None, "question": None}
            if path == "/api/jobs":
                return {"id": "new-job"}
            raise AssertionError(path)

        async def follow(*args):
            yield "Done."

        self.pipe._call = call
        self.pipe._follow = follow

    async def ask(self, text="Explain comets", body=None, **kwargs):
        body = body or {"messages": [{"role": "user", "content": text}]}
        return "".join([part async for part in self.pipe.pipe(body, __user__=OWNER, **kwargs)])

    def jobs(self):
        return [kwargs["json"] for _, path, kwargs in self.calls if path == "/api/jobs"]

    async def test_body_and_nested_metadata_ids_preserve_distinct_chats_in_both_modes(self):
        for mode in ("chat", "cowork"):
            self.pipe.valves.MODE = mode
            await self.ask(body={"chat_id": "chat-a", "messages": [{"role": "user", "content": "Explain comets"}]})
            await self.ask(body={"metadata": {"chat_id": "chat-b"},
                                 "messages": [{"role": "user", "content": "Explain comets"}]})
        self.assertEqual([job["thread"] for job in self.jobs()], ["chat-a", "chat-b", "chat-a", "chat-b"])
        self.assertEqual([job["skill"] for job in self.jobs()], ["chat", "chat", "cowork", "cowork"])

    async def test_missing_id_never_queries_a_shared_task_or_workspace(self):
        for metadata in ({}, {"session_id": "same-browser-session"}):
            result = await self.ask(__metadata__=metadata)
            self.assertIn("couldn't identify this conversation", result)
        self.assertEqual(self.calls, [])

    async def test_temporary_socket_ids_never_reuse_a_durable_workspace(self):
        for identity in ("temporary:same-browser-socket", "temporary:undefined", "local:same-browser-socket"):
            result = await self.ask(__chat_id__=identity)
            self.assertIn("turn it off or save this conversation", result)
        self.assertEqual(self.calls, [])

    async def test_pending_question_only_receives_answers_from_its_chat(self):
        original = self.pipe._call

        async def call(client, method, path, **kwargs):
            if path == "/api/thread/active" and kwargs["params"]["thread"] == "chat-a":
                self.calls.append((method, path, kwargs))
                return {"job": {"id": "waiting-a"}, "question": {"question": "Choose an option"}}
            if path == "/api/jobs/waiting-a/answer":
                self.calls.append((method, path, kwargs))
                return {"answered": True}
            return await original(client, method, path, **kwargs)

        self.pipe._call = call
        await self.ask("A new task in B", __chat_id__="chat-b")
        self.assertEqual([job["thread"] for job in self.jobs()], ["chat-b"])
        self.assertFalse(any(path.endswith("/answer") for _, path, _ in self.calls))
        await self.ask("Option two", __chat_id__="chat-a")
        answers = [kwargs["json"]["answer"] for _, path, kwargs in self.calls if path.endswith("/answer")]
        self.assertEqual(answers, ["Option two"])
        self.assertEqual(len(self.jobs()), 1)

    async def test_old_assistant_thoughts_stay_out_of_later_prompts(self):
        markers = (
            "<think>private prior thought</think>",
            "<thinking>private prior thought</thinking>",
            "<reason>private prior thought</reason>",
            "<reasoning>private prior thought</reasoning>",
            "<thought>private prior thought</thought>",
            "<|begin_of_thought|>private prior thought<|end_of_thought|>",
            '<details type="reasoning"><summary>Thought</summary>private prior thought</details>',
        )
        body = {"messages": [{"role": "assistant", "content": marker + "Earlier final reply."} for marker in markers]
                + [{"role": "user", "content": "Please explain the literal tag <think>example</think>."}]}
        await self.ask(body=body, __chat_id__="chat-history")
        task = self.jobs()[0]["task"]
        self.assertNotIn("private prior thought", task)
        self.assertIn("Earlier final reply.", task)
        self.assertIn("literal tag <think>example</think>", task)
        self.assertEqual(assistant_history("<think>unfinished private reasoning"), "")

    async def test_user_authored_details_are_preserved(self):
        body = {"messages": [{"role": "user", "content": "<details>My requirements</details>"},
                             {"role": "assistant", "content": "Got it."},
                             {"role": "user", "content": "Use those requirements."}]}
        await self.ask(body=body, __chat_id__="chat-user-markup")
        self.assertIn("<details>My requirements</details>", self.jobs()[0]["task"])

    def test_unusual_long_and_reserved_ids_cannot_alias_other_chats(self):
        cases = ("one/a", "one:a", "-one-a", "one-a-", "x" * 81, "x" * 80 + "y", "   ", None)
        threads = [Pipe._thread({}, value) for value in cases]
        self.assertEqual(len(set(threads[:-2])), len(cases) - 2)
        self.assertEqual(threads[-2:], [None, None])
        for thread in threads[:-2]:
            self.assertLessEqual(len(thread), 80)
            self.assertEqual(Pipe._thread({}, thread).startswith("id-sha256-"), True)
            self.assertNotEqual(Pipe._thread({}, thread), thread)
        self.assertEqual(Pipe._thread({}, "chat-standard"), "chat-standard")
        self.assertEqual(Pipe._thread({"chat_id": "metadata"}, "explicit", {"chat_id": "body"}), "explicit")


class NativeThinkingTests(unittest.IsolatedAsyncioTestCase):
    async def follow(self, stream=True):
        pipe = Pipe()
        statuses, polls = [], []
        thought = "A real model thought. " * 70

        async def poll(client, identity, after, emit):
            polls.append(after)
            return {"status": "running" if len(polls) == 1 else "completed", "plan": [],
                    "events": [{"id": 1, "kind": "reasoning", "detail": thought},
                               {"id": 2, "kind": "tool", "detail": "Checking a source"}] if len(polls) == 1 else [],
                    "result": "The final answer.", "artifacts": []}

        async def emit(event):
            statuses.append(event["data"]["description"])

        pipe._poll = poll
        pipe._deliver = AsyncMock(return_value="")
        pieces = []
        with patch("integrations.openwebui_cowork.asyncio.sleep", new=AsyncMock()):
            async for piece in pipe._follow(None, "job-a", emit, {"stream": stream}, (OWNER, None, {})):
                if not pieces and stream:
                    self.assertEqual(len(polls), 1)  # delivered while still running
                pieces.append(piece)
        return pieces, thought, statuses, polls

    async def test_thoughts_use_native_reasoning_delta_and_answer_remains_content(self):
        pieces, thought, statuses, polls = await self.follow()
        self.assertTrue(pieces[0].startswith("data: "))
        delta = json.loads(pieces[0][6:].strip())["choices"][0]["delta"]
        self.assertEqual(delta, {"reasoning_content": thought})
        self.assertEqual(pieces[-1], "The final answer.")
        self.assertNotIn(thought, statuses)
        self.assertIn("Checking a source", statuses)
        self.assertEqual(polls, [0, 2])

    async def test_nonstream_reply_does_not_contain_raw_sse(self):
        pieces, _, _, _ = await self.follow(stream=False)
        self.assertEqual(pieces, ["The final answer."])


if __name__ == "__main__":
    unittest.main()
