import importlib.util
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


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.validation = load_module("validation.py", "dta_validation")

    def test_normalizes_lists_without_reordering(self):
        self.assertEqual(
            self.validation.normalize_name_list(["file_readonly", "file_readonly", "web"], "toolsets"),
            ["file_readonly", "web"],
        )

    def test_rejects_invalid_names_and_oversized_lists(self):
        with self.assertRaisesRegex(ValueError, "skills"):
            self.validation.normalize_name_list(["../secret"], "skills")
        with self.assertRaisesRegex(ValueError, "at most"):
            self.validation.normalize_name_list([f"skill-{i}" for i in range(9)], "skills")

    def test_name_is_human_readable_but_log_safe(self):
        self.assertEqual(self.validation.validate_display_name("  Relay reviewer  "), "Relay reviewer")
        with self.assertRaises(ValueError):
            self.validation.validate_display_name("**format injection**")


if __name__ == "__main__":
    unittest.main()
