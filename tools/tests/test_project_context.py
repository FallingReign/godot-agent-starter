#!/usr/bin/env python3
"""Focused tests for portable project context and dispatch path policy."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Iterator
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import project_context as context  # noqa: E402


@contextlib.contextmanager
def _scratch() -> Iterator[Path]:
    configured = os.environ.get("KIT_TEST_TMPDIR")
    candidates = ([Path(configured)] if configured else []) + [
        TOOLS.parent / ".checklogs" / "tests", Path(tempfile.gettempdir()), Path("/tmp")
    ]
    root = None
    for candidate in candidates:
        probe = candidate / f"context-probe-{uuid.uuid4().hex}"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe.mkdir()
            (probe / "write-check").write_text("ok", encoding="utf-8")
            shutil.rmtree(probe)
            root = candidate
            break
        except OSError:
            shutil.rmtree(probe, ignore_errors=True)
    if root is None:
        raise RuntimeError("no writable project-context test scratch directory")
    directory = (root / f"project-context-{uuid.uuid4().hex}").resolve()
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.parent == root.resolve():
            shutil.rmtree(directory, ignore_errors=True)


def _write_marker(root: Path, value: dict | None = None) -> Path:
    marker = root / context.MARKER_NAME
    marker.write_text(
        json.dumps(value if value is not None else context.marker_document()),
        encoding="utf-8",
    )
    return marker


class ProjectRootDiscovery(unittest.TestCase):
    def test_nearest_marker_distinguishes_all_roots(self) -> None:
        with _scratch() as root:
            kit = root / "kit"
            project = kit / "projects" / "sample"
            project.mkdir(parents=True)
            _write_marker(kit)

            found = context.resolve_project_context(project)

            self.assertEqual(kit.resolve(), found.kit_root)
            self.assertEqual(project.resolve(), found.project_root)
            self.assertEqual((project / "src").resolve(), found.game_root)
            self.assertEqual((project / ".kit" / "runtime").resolve(), found.runtime_root)
            self.assertEqual((kit / context.MARKER_NAME).resolve(), found.marker_path)

    def test_game_root_dot_and_custom_private_runtime(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            found = context.resolve_project_context(
                project, game_layout=".", runtime_root=".kit/agent-runtime"
            )
            self.assertEqual(project, found.game_root)
            self.assertEqual((project / ".kit" / "agent-runtime").resolve(),
                             found.runtime_root)
            self.assertEqual(".", found.git_pathspec)
            self.assertEqual("scripts/main.gd", found.game_relative("scripts/main.gd"))
            self.assertEqual("src/player.gd", found.game_relative("src/player.gd"))

    def test_repository_paths_map_into_src_layout_without_case_or_prefix_guessing(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            found = context.resolve_project_context(project, game_layout="src")

            self.assertEqual("src", found.git_pathspec)
            self.assertEqual("scripts/main.gd", found.game_relative("src/scripts/main.gd"))
            self.assertEqual("Scripts/Main.gd", found.game_relative("src/Scripts/Main.gd"))
            self.assertIsNone(found.game_relative("README.md"))
            self.assertIsNone(found.game_relative("src"))
            self.assertIsNone(found.game_relative("src/../check.py"))

    def test_configured_context_is_the_shared_root_layout_contract(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            (project / context.CONFIG_NAME).write_text(
                json.dumps({
                    "schema": 1,
                    "game_root": ".",
                    "runtime_root": ".kit/private-runtime",
                    "providers": {},
                }),
                encoding="utf-8",
            )

            found = context.load_configured_context(project)

            self.assertEqual(project, found.kit_root)
            self.assertEqual(project, found.game_root)
            self.assertEqual(".", found.game_layout)
            self.assertEqual(
                (project / ".kit" / "private-runtime").resolve(), found.runtime_root
            )

    def test_configured_context_fails_closed_on_invalid_or_linked_config(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            config = project / context.CONFIG_NAME
            config.write_text('{"schema":1,"game_root":"game"}\n', encoding="utf-8")
            with self.assertRaisesRegex(context.ProjectContextError, "game_root"):
                context.load_configured_context(project)

            config.write_text(
                '{"schema":1,"game_root":".","runtime_root":".kit/runtime"}\n',
                encoding="utf-8",
            )
            path_type = type(config)
            original = path_type.is_symlink

            def fake_is_symlink(path: Path) -> bool:
                return path == config or original(path)

            with mock.patch.object(path_type, "is_symlink", fake_is_symlink):
                with self.assertRaisesRegex(context.ProjectContextError, "regular file"):
                    context.load_configured_context(project)

    def test_missing_or_invalid_marker_fails_at_the_nearest_candidate(self) -> None:
        with _scratch() as root:
            project = root / "project"
            project.mkdir()
            with mock.patch.object(
                context, "MARKER_NAME", ".missing-agent-kit.json"
            ):
                with self.assertRaisesRegex(
                    context.ProjectContextError, "no .missing-agent-kit.json"
                ):
                    context.resolve_project_context(project)

            _write_marker(root)
            _write_marker(project, {"kind": "wrong", "schema": 1})
            with self.assertRaisesRegex(context.ProjectContextError, "unsupported"):
                context.resolve_project_context(project)

    def test_layout_runtime_traversal_and_outside_paths_are_rejected(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            outside = project.parent / "outside-runtime"
            for runtime in (
                "../outside", outside, ".", ".kit", "state/runtime", "src/runtime"
            ):
                with self.subTest(runtime=runtime):
                    with self.assertRaises(context.ProjectContextError):
                        context.resolve_project_context(project, runtime_root=runtime)
            with self.assertRaises(context.ProjectContextError):
                context.resolve_project_context(project, game_layout="game")
            with self.assertRaises(context.ProjectContextError):
                context.resolve_project_context(project, runtime_root=".")

    def test_resolved_redirect_cannot_escape_the_project(self) -> None:
        with _scratch() as root:
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            _write_marker(project)
            link = project / ".kit" / "linked"
            path_resolve = Path.resolve

            def redirected_resolve(path: Path, *args, **kwargs) -> Path:
                try:
                    remainder = path.relative_to(link)
                except ValueError:
                    return path_resolve(path, *args, **kwargs)
                return outside / remainder

            # Windows directory symlinks require privileges that a release
            # verifier must not assume.  Model the canonical result directly:
            # _resolve_within must reject it regardless of which filesystem
            # alias (symlink, junction, mount) produced that result.
            with mock.patch.object(
                Path, "resolve", autospec=True, side_effect=redirected_resolve
            ):
                with self.assertRaisesRegex(context.ProjectContextError, "escapes"):
                    context.resolve_project_context(
                        project, runtime_root=".kit/linked/runtime"
                    )

    def test_runtime_container_must_be_an_unredirected_directory(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            (project / ".kit").write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(
                context.ProjectContextError, "unredirected directory"
            ):
                context.resolve_project_context(project)


class DispatchPathPolicy(unittest.TestCase):
    def test_forbidden_wins_and_unowned_is_distinct(self) -> None:
        with _scratch() as kit:
            _write_marker(kit)
            project = kit / "project"
            project.mkdir()
            found = context.resolve_project_context(project)
            policy = found.path_policy(
                owned=("tools", "docs"), forbidden=("tools/private", "docs/design")
            )

            self.assertEqual("owned", policy.classify("tools/worker.py"))
            self.assertEqual("forbidden", policy.classify("tools/private/token"))
            self.assertEqual("forbidden", policy.classify("docs/design/game.md"))
            self.assertEqual("unowned", policy.classify("project/src/main.gd"))
            self.assertFalse(policy.allows("docs/design/game.md"))

    def test_policy_rejects_traversal_outside_and_empty_ownership(self) -> None:
        with _scratch() as kit:
            _write_marker(kit)
            found = context.resolve_project_context(kit)
            with self.assertRaisesRegex(context.ProjectContextError, "at least one"):
                found.path_policy(owned=())
            policy = found.path_policy(owned=("tools",))
            with self.assertRaises(context.ProjectContextError):
                policy.classify("../outside")
            with self.assertRaises(context.ProjectContextError):
                policy.classify(kit.parent / "outside")

    def test_violations_and_status_are_sorted_and_deterministic(self) -> None:
        with _scratch() as kit:
            _write_marker(kit)
            found = context.resolve_project_context(kit, game_layout=".")
            policy = found.path_policy(
                owned=("tools", "docs", "tools"), forbidden=("docs/design",)
            )
            values = ("outside.txt", "docs/design/a.md", "tools/a.py")
            self.assertEqual(
                ("docs/design/a.md: forbidden", "outside.txt: unowned"),
                policy.violations(reversed(values)),
            )
            first = found.to_json(policy)
            second = found.to_json(policy)
            self.assertEqual(first, second)
            decoded = json.loads(first)
            self.assertEqual(sorted(decoded["path_policy"]["owned_roots"]),
                             decoded["path_policy"]["owned_roots"])
            self.assertNotIn("\\", first)


class CommandLineStatus(unittest.TestCase):
    def test_main_emits_one_deterministic_serializable_status(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            outputs = []
            for _ in range(2):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    code = context.main([
                        "--project", str(project), "--game-root", ".",
                        "--owned", "tools", "--forbidden", "tools/private",
                    ])
                self.assertEqual(0, code)
                outputs.append(stream.getvalue())
            self.assertEqual(outputs[0], outputs[1])
            value = json.loads(outputs[0])
            self.assertEqual(context.SCHEMA, value["schema"])
            self.assertEqual(context.MARKER_KIND, value["marker"]["kind"])


if __name__ == "__main__":
    unittest.main()
