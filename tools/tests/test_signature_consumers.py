#!/usr/bin/env python3
"""The architecture and plan share exact, typed GDScript signatures."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
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


def _scratch_parent() -> Path:
    configured = os.environ.get("KIT_TEST_TMPDIR", "").strip()
    return Path(configured) if configured else ROOT / ".checklogs" / "tests"


class ArchitectureSignatureConsumer(unittest.TestCase):
    def setUp(self) -> None:
        parent = _scratch_parent()
        parent.mkdir(parents=True, exist_ok=True)
        self.scratch = parent / f"signature-consumers-{uuid.uuid4().hex}"
        self.scratch.mkdir()

    def tearDown(self) -> None:
        resolved = self.scratch.resolve()
        expected = _scratch_parent().resolve()
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

    @staticmethod
    def _changed_stat(info: os.stat_result, **changes: object) -> SimpleNamespace:
        fields = set(arch._PATH_IDENTITY_FIELDS)
        fields.add("st_file_attributes")
        values = {field: getattr(info, field, None) for field in fields}
        values.update(changes)
        return SimpleNamespace(**values)

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

    def test_architecture_scan_rejects_a_reparse_source_file(self) -> None:
        source = self._write("linked.gd", "extends RefCounted\n")
        source_path = os.path.normcase(os.path.abspath(source))
        real_lstat = Path.lstat
        marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))

        def simulated_reparse(path: Path) -> object:
            info = real_lstat(path)
            if os.path.normcase(os.path.abspath(path)) != source_path:
                return info
            attributes = int(getattr(info, "st_file_attributes", 0) or 0)
            return self._changed_stat(
                info,
                st_file_attributes=attributes | marker,
            )

        with mock.patch.object(Path, "lstat", simulated_reparse):
            with self.assertRaisesRegex(
                arch.ArchitectureScanError,
                r"architecture scan blocked: linked file is not allowed: linked\.gd",
            ):
                arch.scan_project(self.scratch)

    def test_architecture_scan_never_follows_a_symbolic_link_target(self) -> None:
        source = self._write("linked.gd", "extends RefCounted\n")
        source_path = os.path.normcase(os.path.abspath(source))
        real_lstat = Path.lstat

        def simulated_symlink(path: Path) -> os.stat_result:
            info = real_lstat(path)
            if os.path.normcase(os.path.abspath(path)) != source_path:
                return info
            values = list(info)
            values[stat.ST_MODE] = stat.S_IFLNK | stat.S_IMODE(info.st_mode)
            return os.stat_result(values)

        entry = SimpleNamespace(
            name=source.name,
            is_dir=mock.Mock(side_effect=AssertionError("link target was followed")),
        )

        class Entries:
            def __enter__(self) -> object:
                return iter((entry,))

            def __exit__(self, *_args: object) -> None:
                return None

        with mock.patch.object(Path, "lstat", simulated_symlink), \
                mock.patch.object(arch.os, "scandir", return_value=Entries()):
            with self.assertRaisesRegex(
                arch.ArchitectureScanError,
                r"architecture scan blocked: symbolic link is not allowed: linked\.gd",
            ):
                arch.scan_project(self.scratch)

        entry.is_dir.assert_not_called()

    def test_architecture_scan_rejects_a_hardlinked_file(self) -> None:
        linked = self._write("linked.gd", "extends RefCounted\n")
        linked_path = os.path.normcase(os.path.abspath(linked))
        real_lstat = Path.lstat

        def simulated_hardlink(path: Path) -> object:
            info = real_lstat(path)
            if os.path.normcase(os.path.abspath(path)) != linked_path:
                return info
            return self._changed_stat(info, st_nlink=2)

        with mock.patch.object(Path, "lstat", simulated_hardlink):
            with self.assertRaisesRegex(
                arch.ArchitectureScanError,
                r"architecture scan blocked: hard-linked file is not allowed:",
            ):
                arch.scan_project(self.scratch)

    def test_architecture_scan_ignores_an_unrelated_hardlinked_asset(self) -> None:
        linked = self._write("texture.png", "not architecture source\n")
        linked_path = os.path.normcase(os.path.abspath(linked))
        real_lstat = Path.lstat

        def simulated_hardlink(path: Path) -> object:
            info = real_lstat(path)
            if os.path.normcase(os.path.abspath(path)) != linked_path:
                return info
            return self._changed_stat(info, st_nlink=2)

        with mock.patch.object(Path, "lstat", simulated_hardlink):
            inventory = arch.scan_project(self.scratch)

        self.assertEqual((), inventory.sources)

    def test_architecture_scan_rejects_an_oversized_source_file(self) -> None:
        self._write("large.gd", "123456789")

        with mock.patch.object(arch, "MAX_ARCHITECTURE_FILE_BYTES", 8):
            with self.assertRaisesRegex(
                arch.ArchitectureScanError,
                r"source file exceeds 8 bytes: large\.gd",
            ):
                arch.scan_project(self.scratch)

    def test_architecture_scan_rejects_excessive_member_count(self) -> None:
        self._write("one.gd", "extends RefCounted\n")
        self._write("two.gd", "extends RefCounted\n")

        with mock.patch.object(arch, "MAX_ARCHITECTURE_SCAN_MEMBERS", 1):
            with self.assertRaisesRegex(
                arch.ArchitectureScanError,
                r"project contains more than 1 scan members",
            ):
                arch.scan_project(self.scratch)

    def test_architecture_scan_rejects_excessive_total_source_bytes(self) -> None:
        self._write("one.gd", "123456")
        self._write("two.gd", "abcdef")

        with mock.patch.object(arch, "MAX_ARCHITECTURE_TOTAL_BYTES", 10):
            with self.assertRaisesRegex(
                arch.ArchitectureScanError,
                r"source files exceed 10 total bytes",
            ):
                arch.scan_project(self.scratch)

    def test_architecture_scan_rejects_excessive_depth(self) -> None:
        self._write("one/two/source.gd", "extends RefCounted\n")

        with mock.patch.object(arch, "MAX_ARCHITECTURE_SCAN_DEPTH", 1):
            with self.assertRaisesRegex(
                arch.ArchitectureScanError,
                r"project nesting exceeds 1 levels:",
            ):
                arch.scan_project(self.scratch)

    def test_architecture_document_uses_one_project_scan(self) -> None:
        self._write("scripts/source.gd", "extends RefCounted\n")
        real_scan = arch.scan_project

        with mock.patch.object(arch, "scan_project", wraps=real_scan) as scanner:
            rendered = arch.render_document(
                self.scratch,
                {"module_depth": 2, "modules": {}},
                "# Architecture\n",
            )

        self.assertEqual(1, scanner.call_count)
        self.assertIn('m0["scripts"]', rendered)

    def test_architecture_document_writes_canonical_lf_after_windows_input(self) -> None:
        document = (
            "# Architecture\r\r\n\r\r\n"
            f"{arch.BEGIN}\r\nold\r\n{arch.END}\r\n"
        )
        rendered = arch.splice(document, "```mermaid\r\ngraph TD\r\n```")
        output = self.scratch / "ARCHITECTURE.md"

        arch._write_architecture_document(output, rendered)

        content = output.read_bytes()
        self.assertNotIn(b"\r", content)
        self.assertEqual(1, content.count(arch.BEGIN.encode("utf-8")))
        self.assertIn(b"```mermaid\ngraph TD\n```", content)

    def test_architecture_graph_rejects_amplification_limits(self) -> None:
        self._write(
            "a/one.gd",
            'extends RefCounted\nconst B = preload("res://b/source.gd")\n',
        )
        self._write(
            "a/two.gd",
            'extends RefCounted\nconst B = preload("res://b/source.gd")\n',
        )
        self._write(
            "b/source.gd",
            'extends RefCounted\nconst C = preload("res://c/source.gd")\n',
        )
        self._write("c/source.gd", "extends RefCounted\n")
        inventory = arch.scan_project(self.scratch)
        cases = (
            (
                "MAX_ARCHITECTURE_GRAPH_MODULES",
                2,
                r"architecture graph contains more than 2 modules",
            ),
            (
                "MAX_ARCHITECTURE_GRAPH_EDGES",
                1,
                r"architecture graph contains more than 1 dependency edges",
            ),
            (
                "MAX_ARCHITECTURE_EDGE_SOURCES",
                2,
                r"architecture graph contains more than 2 edge-source associations",
            ),
        )

        for setting, limit, message in cases:
            with self.subTest(setting=setting):
                with mock.patch.object(arch, setting, limit):
                    with self.assertRaisesRegex(arch.ArchitectureScanError, message):
                        arch.build_graph(1, scan=inventory)

    def test_architecture_scan_rejects_a_directory_reparse_point(self) -> None:
        linked = self.scratch / "linked-sources"
        linked.mkdir()
        linked_path = os.path.normcase(os.path.abspath(linked))
        real_lstat = Path.lstat
        marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))

        def simulated_reparse(path: Path) -> object:
            info = real_lstat(path)
            if os.path.normcase(os.path.abspath(path)) != linked_path:
                return info
            attributes = int(getattr(info, "st_file_attributes", 0) or 0)
            return self._changed_stat(
                info,
                st_file_attributes=attributes | marker,
            )

        with mock.patch.object(Path, "lstat", simulated_reparse):
            with self.assertRaisesRegex(
                arch.ArchitectureScanError,
                r"architecture scan blocked: linked directory is not allowed:",
            ):
                arch.scan_project(self.scratch)

    def test_architecture_scan_ignores_an_excluded_directory_reparse_point(self) -> None:
        linked = self.scratch / "addons"
        linked.mkdir()
        linked_path = os.path.normcase(os.path.abspath(linked))
        real_lstat = Path.lstat
        marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))

        def simulated_reparse(path: Path) -> object:
            info = real_lstat(path)
            if os.path.normcase(os.path.abspath(path)) != linked_path:
                return info
            attributes = int(getattr(info, "st_file_attributes", 0) or 0)
            return self._changed_stat(
                info,
                st_file_attributes=attributes | marker,
            )

        with mock.patch.object(Path, "lstat", simulated_reparse):
            inventory = arch.scan_project(self.scratch)
            self.assertEqual((), inventory.sources)

    def test_boundary_violation_is_bound_to_the_file_that_causes_it(self) -> None:
        self._write(
            "scripts/player.gd",
            "extends Node\nconst RULES = preload(\"res://custom/rules.gd\")\n",
        )
        self._write("custom/rules.gd", "extends RefCounted\n")
        rules = {
            "modules": {
                "scripts": {"may_depend_on": []},
                "custom": {"may_depend_on": []},
            },
            "forbid_autoload_use_in": [],
        }

        with mock.patch.object(arch, "PROJECT_DIR", self.scratch):
            edges, _classes, _autoloads, users, sources = arch.build_graph(2)
            issues = arch.violation_issues(edges, users, sources, rules)

        self.assertEqual(1, len(issues))
        self.assertEqual("dependency-not-permitted", issues[0]["code"])
        self.assertEqual("scripts/player.gd", issues[0]["path"])
        self.assertIn("scripts -> custom", issues[0]["message"])

    def test_mermaid_ids_are_unique_and_labels_are_safe(self) -> None:
        graph = arch.mermaid(
            {"foo-bar": {"foo_bar"}, "foo_bar": set()},
            {
                "modules": {
                    "foo-bar": {
                        "description": 'Player "input" & <feedback>\nnow',
                    },
                    "foo_bar": {"description": ""},
                }
            },
            fenced=False,
        )

        self.assertIn(
            'm0["foo-bar<br/><i>Player &quot;input&quot; &amp; '
            '&lt;feedback&gt; now</i>"]',
            graph,
        )
        self.assertIn('m1["foo_bar"]', graph)
        self.assertIn("m0 --> m1", graph)
        self.assertNotIn('foo_bar["', graph)


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
