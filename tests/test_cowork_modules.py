"""Unit tests for the cowork package's submodules; each also pins the `import cowork` facade."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import cowork
import escalate
import sandbox
import store
import workspace as ws
from crew import CallBudget
from openai import AsyncOpenAI


def call(call_id, name="write_file", arguments="{}"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def output(call_id, text="ok"):
    return {"type": "function_call_output", "call_id": call_id, "output": text}


class ContextUnit(unittest.TestCase):
    def history(self, n, size):
        items = [{"role": "user", "content": "do the thing"}]
        for i in range(n):
            items.append(call(f"c{i}", arguments=json.dumps({"path": f"f{i}.md", "content": "x" * size})))
            items.append(output(f"c{i}", f"out{i} " + "y" * size))
        return items

    def test_small_list_is_returned_untouched(self):
        from cowork.context import trim_items
        small = self.history(3, 100)
        self.assertIs(trim_items(small), small)
        self.assertIs(cowork.trim_items, trim_items)

    def test_large_list_is_trimmed_without_mutating_the_input(self):
        from cowork.context import _size, trim_items
        big = self.history(40, 12000)
        original = json.dumps(big)
        trimmed = trim_items(big, soft=120000, hard=260000)
        self.assertEqual(json.dumps(big), original)
        self.assertLess(sum(_size(i) for i in trimmed), 130000)
        outputs = [i for i in trimmed if i.get("type") == "function_call_output"]
        self.assertTrue(outputs[0]["output"].startswith("[Older tool result trimmed"))
        self.assertEqual(outputs[-1]["output"], big[-1]["output"])
        self.assertEqual(cowork.trim_items(big, soft=120000, hard=260000), trimmed)
        self.assertIs(cowork._size, _size)

    def test_replayable_drops_unanswered_calls(self):
        from cowork.context import replayable
        items = [{"role": "user", "content": "x"}, call("a", "n"), output("a"), call("b", "n")]
        self.assertEqual([i.get("call_id") for i in replayable(items)], [None, "a", "a"])
        self.assertEqual(cowork.replayable(items), replayable(items))

    def test_run_config_uses_the_context_filter(self):
        from cowork.context import RUN_CONFIG, context_filter
        self.assertIs(RUN_CONFIG.call_model_input_filter, context_filter)
        self.assertIs(cowork.RUN_CONFIG, RUN_CONFIG)


class GpuUnit(unittest.TestCase):
    def setUp(self):
        from cowork.gpu import _leads
        _leads.clear()
        self.addCleanup(_leads.clear)

    def test_assign_gpus_alternates(self):
        from cowork.gpu import assign_gpus
        self.assertEqual(assign_gpus("a"), ("qwen-1", "qwen-2"))
        self.assertEqual(assign_gpus("b"), ("qwen-2", "qwen-1"))
        self.assertEqual(cowork.assign_gpus("c"), ("qwen-1", "qwen-2"))

    def test_release_gpus_removes_the_lead(self):
        from cowork.gpu import _leads, assign_gpus, release_gpus
        assign_gpus("a")
        self.assertIn("a", _leads)
        release_gpus("a")
        self.assertNotIn("a", _leads)
        self.assertIs(cowork._leads, _leads)
        cowork.release_gpus("never-assigned")  # harmless


class EffortUnit(unittest.TestCase):
    def test_empty_items_return_base(self):
        from cowork.effort import effort_for
        self.assertEqual(effort_for([], "medium"), "medium")
        self.assertEqual(cowork.effort_for([], "medium"), "medium")

    def test_low_and_none_are_never_raised(self):
        from cowork.effort import effort_for
        self.assertEqual(effort_for([call("1", "run_shell"), output("1")], "low"), "low")
        self.assertEqual(effort_for([], "none"), "none")

    def test_read_only_steps_think_less_but_shell_and_failures_keep_base(self):
        from cowork.effort import effort_for
        read = [call("1", "read_file"), output("1", "text")]
        self.assertEqual(effort_for(read, "medium"), "low")
        shell = [call("2", "run_shell"), output("2", "Exit code 0\nfine")]
        self.assertEqual(effort_for(shell, "medium"), "medium")
        shell += [call("3", "run_shell"), output("3", "Exit code 1\nTraceback")]
        self.assertEqual(cowork.effort_for(shell, "medium"), "medium")
        failed_read = [call("1", "read_file"), output("1", "An error occurred: no such file")]
        self.assertEqual(effort_for(failed_read, "medium"), "medium")


class PromptUnit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.space = sandbox.Workspace("owner", "t")
        self.job = {"id": "j" * 32, "project": "default", "profile": "fast", "allow_images": 1, "allow_frontier": 1}

    def test_instructions_include_the_plan_text(self):
        from cowork.prompt import instructions
        text = instructions(self.job, self.space, plan_text="PHASE ONE: gather data")
        self.assertIn("PROJECT PLAN (plan.md", text)
        self.assertIn("PHASE ONE: gather data", text)
        self.assertEqual(cowork.instructions(self.job, self.space, plan_text="PHASE ONE: gather data"), text)

    def test_no_escalation_omits_the_section(self):
        from cowork.prompt import instructions
        guest = instructions(self.job, self.space)
        self.assertNotIn("ESCALATION", guest)
        self.assertNotIn("ask_claude", guest)
        owner = instructions(self.job, self.space, escalation=["claude", "codex"])
        self.assertIn("ESCALATION", owner)
        self.assertIn("ask_claude", owner)
        self.assertIn("ask_codex", owner)

    def test_constants_are_shared_with_the_facade(self):
        from cowork import prompt
        self.assertIs(cowork.WRAP_UP, prompt.WRAP_UP)
        self.assertIs(cowork.TRUST, prompt.TRUST)
        self.assertIn("{reason}", prompt.STOP_WRAP_UP)


class ToolsUnit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.chmod(self.temp.name, 0o755)
        self.old = (store.DATA, sandbox.ROOT, dict(sandbox.USERS))
        store.DATA = Path(self.temp.name) / "data"
        sandbox.ROOT = Path(self.temp.name) / "cowork"
        if sandbox.IS_ROOT:
            sandbox.USERS.update(owner="nobody", guest="nobody", friend="nobody")
        ws.init()
        cowork._leads.clear()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        store.DATA, sandbox.ROOT = self.old[0], self.old[1]
        sandbox.USERS.clear()
        sandbox.USERS.update(self.old[2])
        cowork._leads.clear()
        self.temp.cleanup()

    def names(self, tier, project):
        from cowork.tools import ToolContext, build_tools
        job = {"id": "j" * 32, "project": project, "profile": "fast", "allow_frontier": 1, "allow_images": 0 if tier == "guest" else 1,
               "thread": "t"}
        space = sandbox.Workspace(tier, "t").prepare()
        client = AsyncOpenAI(api_key="t", base_url="https://fake.invalid/v1")
        ctx = ToolContext(job=job, space=space, client=client, gate=asyncio.Semaphore(1), budget=CallBudget(job, limit=10), state={"delegations": 0}, images={"count": 0},
                          log=lambda kind, detail: None, before=lambda name: None, lead_model="qwen-1", helper_model="qwen-2")
        with patch.object(escalate, "available", return_value=None):
            tools = build_tools(ctx)
        self.assertIs(cowork.build_tools, build_tools)
        return {t.name for t in tools}, ctx

    def test_owner_gets_the_full_toolset(self):
        names, ctx = self.names("owner", "default")
        self.assertTrue({"run_shell", "list_files", "read_file", "write_file", "edit_file", "share_file", "web_search",
                         "read_webpage", "recall", "remember", "search_knowledge", "search_posts", "recent_posts", "topic_stats",
                         "update_plan", "queue_next_phase", "delegate", "delegate_many", "generate_image", "ask_claude",
                         "ask_codex"} <= names)
        self.assertEqual(ctx.escalation, ["claude", "codex"])

    def test_guest_has_no_escalation_or_images_when_not_allowed(self):
        names, ctx = self.names("guest", "friends")
        self.assertNotIn("ask_claude", names)
        self.assertNotIn("ask_codex", names)
        self.assertNotIn("generate_image", names)
        self.assertIn("run_shell", names)
        self.assertEqual(ctx.escalation, [])


if __name__ == "__main__":
    unittest.main()
