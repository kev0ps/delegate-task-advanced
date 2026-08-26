import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SkillContextTests(unittest.TestCase):
    def setUp(self):
        self.skills = load_module("skill_context.py", "dta_skill_context")

    def test_appends_framed_skill_context(self):
        def dispatch(_name, args, **_kwargs):
            return json.dumps({"success": True, "content": f"content:{args['name']}"})

        context, names = self.skills.build_skill_context(
            "base", ["alpha", "beta"], dispatch
        )
        self.assertEqual(names, ["alpha", "beta"])
        self.assertIn("base", context)
        self.assertIn("BEGIN EXPLICIT SKILLS", context)
        self.assertIn("content:alpha", context)

    def test_retries_skill_view_without_task_id_when_parent_dedup_hides_content(self):
        calls = []

        def dispatch(_name, args, **kwargs):
            calls.append((args, kwargs))
            if kwargs.get("task_id") == "parent-task":
                return json.dumps({
                    "success": True,
                    "status": "unchanged",
                    "dedup": True,
                    "content_returned": False,
                })
            return json.dumps({"success": True, "content": "# Injected skill"})

        context, names = self.skills.build_skill_context(
            None,
            ["alpha"],
            dispatch,
            dispatch_kwargs={"task_id": "parent-task", "session_id": "session"},
        )

        self.assertEqual(names, ["alpha"])
        self.assertIn("# Injected skill", context)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1], {"session_id": "session"})

    def test_fails_loudly_on_malformed_or_oversized_skill_payload(self):
        def malformed(*_args, **_kwargs):
            return "not-json"
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.skills.build_skill_context(None, ["alpha"], malformed)

        def huge(*_args, **_kwargs):
            return json.dumps({"success": True, "content": "x" * 25000})
        with self.assertRaisesRegex(ValueError, "limit"):
            self.skills.build_skill_context(None, ["alpha"], huge, max_chars=100)


if __name__ == "__main__":
    unittest.main()
