#!/usr/bin/env python3
"""The architecture and plan share exact, typed GDScript signatures."""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

import arch  # noqa: E402
import gd_signature  # noqa: E402
import plan_html  # noqa: E402


class ArchitectureSignatureConsumer(unittest.TestCase):
    def setUp(self) -> None:
        parent = ROOT / ".checklogs" / "tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.scratch = parent / f"signature-consumers-{uuid.uuid4().hex}"
        self.scratch.mkdir()

    def tearDown(self) -> None:
        resolved = self.scratch.resolve()
        expected = (ROOT / ".checklogs" / "tests").resolve()
        if resolved.parent != expected or not resolved.name.startswith(
            "signature-consumers-"
        ):
            raise AssertionError(f"refusing to remove unexpected scratch: {resolved}")
        shutil.rmtree(resolved)

    def _write(self, relative: str, text: str) -> Path:
        path = self.scratch / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_file_tree_uses_the_canonical_multiline_signature(self) -> None:
        self._write(
            "scripts/logic/factory.gd",
            "class_name Factory\n"
            "extends RefCounted\n\n"
            "static func build(\n"
            "        values: Array[int] = [1, maxi(2, 3)],\n"
            "        options: Dictionary[String, int] = {\"a\": 1}\n"
            ") -> Dictionary[String, int]:\n"
            "    return options\n",
        )

        with mock.patch.object(arch, "PROJECT_DIR", self.scratch):
            tree = arch.file_tree()

        functions = tree["scripts/logic/factory.gd"]["functions"]
        self.assertEqual(1, len(functions))
        self.assertEqual("build", functions[0]["name"])
        self.assertEqual(
            "static func build(values: Array[int] = [1, maxi(2, 3)], "
            "options: Dictionary[String, int] = {\"a\": 1}) -> "
            "Dictionary[String, int]",
            functions[0]["signature"],
        )

    def test_file_tree_names_the_file_when_a_signature_is_malformed(self) -> None:
        self._write(
            "scripts/logic/broken.gd",
            "extends RefCounted\n\nfunc broken(value) -> void:\n    pass\n",
        )

        with mock.patch.object(arch, "PROJECT_DIR", self.scratch):
            with self.assertRaises(gd_signature.SignatureError) as caught:
                arch.file_tree()

        message = str(caught.exception)
        self.assertIn("scripts/logic/broken.gd", message)
        self.assertIn("every parameter must be", message)

    def test_file_tree_rejects_non_utf8_source_instead_of_replacing_it(self) -> None:
        path = self.scratch / "scripts" / "logic" / "binary.gd"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"extends RefCounted\n\xff\n")

        with mock.patch.object(arch, "PROJECT_DIR", self.scratch):
            with self.assertRaises(gd_signature.SignatureError) as caught:
                arch.file_tree()

        self.assertIn("scripts/logic/binary.gd", str(caught.exception))
        self.assertIn("source could not be read", str(caught.exception))

    def test_json_cli_fails_closed_with_a_machine_readable_diagnostic(self) -> None:
        self._write(
            "scripts/broken.gd",
            "extends RefCounted\n\nfunc broken(value: int):\n    pass\n",
        )
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(arch, "PROJECT_DIR", self.scratch), \
                mock.patch.object(sys, "argv", ["arch.py", "--json"]), \
                contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(errors):
            code = arch.main()

        self.assertEqual(1, code)
        payload = json.loads(output.getvalue())
        self.assertIn("architecture source error", payload["errors"][0])
        self.assertIn("scripts/broken.gd", payload["errors"][0])
        self.assertEqual("", errors.getvalue())


class PlanSignatureConsumer(unittest.TestCase):
    FILE = "scripts/logic/action.gd"

    @staticmethod
    def _proposal(signature: str) -> dict:
        return {
            "files": [{"path": PlanSignatureConsumer.FILE, "action": "new"}],
            "modules": [{"path": "scripts/logic"}],
            "functions": [{
                "file": PlanSignatureConsumer.FILE,
                "signature": signature,
                "action": "new",
            }],
        }

    @staticmethod
    def _tree(signature: str) -> dict:
        parsed = gd_signature.parse_proposal_signature(signature)
        return {
            PlanSignatureConsumer.FILE: {
                "functions": [{
                    "name": parsed.name,
                    "signature": parsed.canonical,
                    "private": parsed.name.startswith("_"),
                }]
            }
        }

    @staticmethod
    def _node(graph: str, label: str) -> str:
        match = re.search(
            r'^\s*(n\d+)\("' + re.escape(label) + r'"\)$', graph, re.MULTILINE
        )
        if match is None:
            raise AssertionError(f"node not found for {label!r}:\n{graph}")
        return match.group(1)

    @staticmethod
    def _assert_state(test: unittest.TestCase, graph: str,
                      node: str, state: str) -> None:
        test.assertRegex(graph, rf"(?m)^\s*class [^\n]*\b{node}\b[^\n]* {state}$")

    def test_same_name_with_a_different_signature_is_not_built(self) -> None:
        wanted = "func act(value: int) -> void"
        actual = "func act(value: String) -> void"

        graph = plan_html.hierarchy(
            self._proposal(wanted), self._tree(actual), {self.FILE}, 3
        )

        actual_node = self._node(graph, actual)
        wanted_node = self._node(graph, wanted)
        self._assert_state(self, graph, actual_node, "extra")
        self._assert_state(self, graph, wanted_node, "missing")

    def test_equivalent_formatting_matches_the_canonical_signature(self) -> None:
        proposed = "func act( value:int = maxi( 1, 2 ) )->void"
        canonical = "func act(value: int = maxi(1, 2)) -> void"

        graph = plan_html.hierarchy(
            self._proposal(proposed), self._tree(canonical), {self.FILE}, 3
        )

        node = self._node(graph, canonical)
        self._assert_state(self, graph, node, "built")
        self.assertEqual(1, graph.count(canonical))

    def test_an_invalid_name_only_proposal_never_marks_source_built(self) -> None:
        actual = "func act(value: int) -> void"
        graph = plan_html.hierarchy(
            self._proposal("func act(value)"), self._tree(actual), {self.FILE}, 3
        )

        node = self._node(graph, actual)
        self._assert_state(self, graph, node, "extra")

    def test_architecture_errors_stop_the_plan_consumer(self) -> None:
        process = SimpleNamespace(
            stdout=json.dumps({"errors": [
                "architecture source error: scripts/logic/broken.gd: line 2"
            ]}),
            stderr="",
            returncode=1,
        )
        with mock.patch.object(plan_html.subprocess, "run", return_value=process):
            with self.assertRaises(plan_html.ArchitectureGraphError) as caught:
                plan_html.module_graph()

        self.assertIn("scripts/logic/broken.gd", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
