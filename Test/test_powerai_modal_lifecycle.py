"""Regression checks for keeping the underlying page stable when PowerAI opens."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeStreamlit:
    def __init__(self):
        self.session_state = {}


class PowerAiModalLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_path = PROJECT_ROOT / "streamlit_app.py"
        cls.tree = ast.parse(
            cls.source_path.read_text(encoding="utf-8-sig"),
            filename=str(cls.source_path),
        )

    def test_open_callback_sets_only_the_modal_state(self):
        open_node = next(
            node
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_open_powerai_dialog"
        )
        fake_st = _FakeStreamlit()
        namespace = {
            "st": fake_st,
            "_POWERAI_PANEL_OPEN_KEY": "powerai_panel_open",
        }
        exec(compile(ast.Module(body=[open_node], type_ignores=[]), str(self.source_path), "exec"), namespace)

        namespace["_open_powerai_dialog"]()

        self.assertEqual(fake_st.session_state, {"powerai_panel_open": True})

    def test_launcher_does_not_issue_an_explicit_rerun(self):
        launcher = next(
            node
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "render_powerai_floating_button"
        )
        reruns = [
            node
            for node in ast.walk(launcher)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "st"
            and node.func.attr == "rerun"
        ]
        callbacks = [
            keyword.value.id
            for node in ast.walk(launcher)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "on_click" and isinstance(keyword.value, ast.Name)
        ]

        self.assertEqual(reruns, [])
        self.assertIn("_open_powerai_dialog", callbacks)


if __name__ == "__main__":
    unittest.main()
