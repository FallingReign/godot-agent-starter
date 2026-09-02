#!/usr/bin/env python3
"""Focused tests for portable project context and dispatch path policy."""
from __future__ import annotations

import contextlib
import hashlib
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


def _canonical_json(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _write_managed_install(root: Path) -> tuple[Path, str]:
    source_commit = "a" * 40
    install_manifest_content = b'{"schema":1}\n'
    contents = {
        "INSTALL-MANIFEST.json": install_manifest_content,
        "LICENSE": b"MIT\n",
        "kit.py": b"#!/usr/bin/env python3\n",
    }
    entries = []
    for relative, content in sorted(contents.items()):
        entries.append({
            "path": relative,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "mode": "0755" if relative.endswith(".py") else "0644",
        })
    manifest = {
        "schema": 2,
        "version": "0.3.0",
        "source": {"commit": source_commit, "dirty": False},
        "authority_evidence": {
            "identity_model": "portable-policy-audit",
            "project_receipt_trust": "not-applicable-no-project-state",
            "receipt_trust": "portable-policy",
        },
        "license_files": ["LICENSE"],
        "normalization": {
            "line_endings": "lf",
            "regular_mode": "0644",
            "executable_mode": "0755",
            "timestamps": "fixed",
        },
        "files": entries,
    }
    manifest_content = _canonical_json(manifest)
    archive_members = {
        relative: (content, 0o755 if relative.endswith(".py") else 0o644)
        for relative, content in contents.items()
    }
    archive_members["RELEASE-MANIFEST.json"] = (manifest_content, 0o644)
    release_sha = context.managed_launcher._canonical_archive_sha256(archive_members)
    core = root / ".agent-kit" / "releases" / release_sha
    core.mkdir(parents=True)
    for relative, content in sorted(contents.items()):
        target = core / relative
        target.write_bytes(content)
        target.chmod(0o755 if relative.endswith(".py") else 0o644)
    manifest_path = core / "RELEASE-MANIFEST.json"
    manifest_path.write_bytes(manifest_content)
    manifest_path.chmod(0o644)
    current = {
        "schema": 1,
        "kind": "agent-kit-install-state",
        "installation_id": "1" * 32,
        "install_schema": 1,
        "layout_schema": 1,
        "config_schema": 1,
        "active_release": {
            "kit_version": "0.3.0",
            "archive_sha256": release_sha,
            "release_manifest_sha256": hashlib.sha256(manifest_content).hexdigest(),
            "install_manifest_sha256": hashlib.sha256(
                install_manifest_content
            ).hexdigest(),
            "source_commit": source_commit,
            "core_path": f".agent-kit/releases/{release_sha}",
        },
        "previous_release": None,
        "managed_surfaces": [],
        "applied_migrations": [],
    }
    (root / ".agent-kit" / "current.json").write_bytes(_canonical_json(current))
    return core, release_sha


class ProjectRootDiscovery(unittest.TestCase):
    def test_nearest_marker_is_the_flat_project_and_core_root(self) -> None:
        with _scratch() as root:
            kit = root / "kit"
            project = kit / "projects" / "sample"
            project.mkdir(parents=True)
            (kit / "src").mkdir()
            _write_marker(kit)

            found = context.resolve_project_context(project)

            self.assertEqual(kit.resolve(), found.kit_root)
            self.assertEqual(kit.resolve(), found.project_root)
            self.assertEqual(kit.resolve(), found.core_root)
            self.assertEqual((kit / "src").resolve(), found.game_root)
            self.assertEqual((kit / ".kit" / "runtime").resolve(), found.runtime_root)
            self.assertEqual((kit / context.MARKER_NAME).resolve(), found.marker_path)
            self.assertEqual("flat", found.install_mode)

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
            (project / "src").mkdir()
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

    def test_configured_context_fails_closed_on_invalid_config(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            config = project / context.CONFIG_NAME
            config.write_text(
                '{"schema":1,"game_root":"../game","runtime_root":".kit/runtime"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(context.ProjectContextError, "game root"):
                context.load_configured_context(project)

    def test_configured_context_rejects_a_redirected_config(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            config = project / context.CONFIG_NAME
            config.write_text(
                '{"schema":1,"game_root":".","runtime_root":".kit/runtime"}\n',
                encoding="utf-8",
            )
            path_type = type(config)
            original = path_type.lstat

            def fake_lstat(path: Path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == config:
                    redirected = mock.Mock(wraps=info)
                    redirected.st_mode = info.st_mode
                    redirected.st_file_attributes = 0x0400
                    return redirected
                return info

            with mock.patch.object(
                path_type, "lstat", autospec=True, side_effect=fake_lstat
            ):
                with self.assertRaisesRegex(context.ProjectContextError, "regular file"):
                    context.load_configured_context(project)

    def test_configured_context_rejects_duplicate_json_keys(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            (project / context.CONFIG_NAME).write_text(
                '{"schema":1,"schema":1,"game_root":".",'
                '"runtime_root":".kit/runtime"}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                context.ProjectContextError, "not readable JSON"
            ):
                context.load_configured_context(project)

    def test_configured_context_rejects_a_hardlinked_config(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            source = project / "config-source.json"
            source.write_text(
                '{"schema":1,"game_root":".",'
                '"runtime_root":".kit/runtime"}\n',
                encoding="utf-8",
            )
            config = project / context.CONFIG_NAME
            try:
                os.link(source, config)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable on this host: {exc}")

            with self.assertRaisesRegex(context.ProjectContextError, "regular file"):
                context.load_configured_context(project)

    def test_configured_context_rejects_a_change_during_read(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            config = project / context.CONFIG_NAME
            config.write_text(
                '{"schema":1,"game_root":".",'
                '"runtime_root":".kit/runtime"}\n',
                encoding="utf-8",
            )
            original_read_bytes = Path.read_bytes
            changed = False

            def change_after_read(path: Path) -> bytes:
                nonlocal changed
                content = original_read_bytes(path)
                if path == config and not changed:
                    changed = True
                    path.write_bytes(content + b" ")
                return content

            with mock.patch.object(
                Path, "read_bytes", autospec=True, side_effect=change_after_read
            ):
                with self.assertRaisesRegex(
                    context.ProjectContextError, "changed while it was being read"
                ):
                    context.load_configured_context(project)

    def test_missing_or_invalid_marker_fails_at_the_nearest_candidate(self) -> None:
        with _scratch() as root:
            project = root / "project"
            project.mkdir()
            with mock.patch.object(
                context, "MARKER_NAME", ".missing-agent-kit.json"
            ), mock.patch.object(
                context.managed_launcher, "MARKER_NAME", ".missing-agent-kit.json"
            ):
                with self.assertRaisesRegex(
                    context.ProjectContextError, "no .missing-agent-kit.json"
                ):
                    context.resolve_project_context(project)

            _write_marker(root)
            _write_marker(project, {"kind": "wrong", "schema": 1})
            with self.assertRaisesRegex(context.ProjectContextError, "kit marker"):
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
            for game_root in ("../game", ".agent-kit/game", "tools/game"):
                with self.subTest(game_root=game_root):
                    with self.assertRaises(context.ProjectContextError):
                        context.resolve_project_context(project, game_layout=game_root)
            with self.assertRaises(context.ProjectContextError):
                context.resolve_project_context(
                    project, game_layout=str(project.parent / "outside-game")
                )
            with self.assertRaises(context.ProjectContextError):
                context.resolve_project_context(project, runtime_root=".")

    def test_existing_custom_nested_game_root_is_supported(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            custom = project / "games" / "prototype"
            custom.mkdir(parents=True)

            found = context.resolve_project_context(
                project, game_layout="games/prototype"
            )

            self.assertEqual(custom.resolve(), found.game_root)
            self.assertEqual("games/prototype", found.game_layout)
            self.assertEqual("games/prototype", found.git_pathspec)
            self.assertEqual(
                "scripts/main.gd",
                found.game_relative("games/prototype/scripts/main.gd"),
            )

    def test_managed_context_separates_project_core_game_and_runtime(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            game = project / "game"
            game.mkdir()
            core, release_sha = _write_managed_install(project)
            (project / context.CONFIG_NAME).write_text(
                json.dumps({
                    "schema": 1,
                    "game_root": "game",
                    "runtime_root": ".kit/runtime",
                }),
                encoding="utf-8",
            )

            found = context.resolve_project_context(project, game_layout="game")
            active = context.load_active_context(core, {
                context.managed_launcher.PROJECT_ROOT_ENV: str(project),
                context.managed_launcher.CORE_ROOT_ENV: str(core),
            })
            installation = context.resolve_active_installation(core, {
                context.managed_launcher.PROJECT_ROOT_ENV: str(project),
                context.managed_launcher.CORE_ROOT_ENV: str(core),
            })

            self.assertEqual(project, found.project_root)
            self.assertEqual(project, found.kit_root)
            self.assertEqual(core, found.core_root)
            self.assertEqual(core, found.code_root)
            self.assertEqual(game, found.game_root)
            self.assertEqual(project / ".kit" / "runtime", found.runtime_root)
            self.assertEqual("managed", found.install_mode)
            self.assertEqual(release_sha, found.release_sha256)
            self.assertEqual(project / ".agent-kit" / "current.json", found.current_path)
            self.assertEqual(found.project_root, active.project_root)
            self.assertEqual(found.core_root, active.core_root)
            self.assertEqual(found.game_root, active.game_root)
            self.assertEqual(project, installation.project_root)
            self.assertEqual(core, installation.core_root)

    def test_active_context_accepts_an_unbound_flat_core(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            (project / "game").mkdir()
            (project / context.CONFIG_NAME).write_text(
                json.dumps({
                    "schema": 1,
                    "game_root": "game",
                    "runtime_root": ".kit/runtime",
                }),
                encoding="utf-8",
            )

            found = context.load_active_context(project, {})

            self.assertEqual("flat", found.install_mode)
            self.assertEqual(project, found.project_root)
            self.assertEqual(project, found.core_root)

    def test_game_root_rejects_case_ambiguity_and_reserved_paths(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            (project / "Game").mkdir()

            with self.assertRaisesRegex(context.ProjectContextError, "unsafe casing"):
                context.resolve_project_context(project, game_layout="game")
            with self.assertRaisesRegex(context.ProjectContextError, "reserved kit path"):
                context.resolve_project_context(project, game_layout=".KIT/game")

    def test_game_root_rejects_a_reparse_component(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            game = project / "games"
            (game / "prototype").mkdir(parents=True)
            path_lstat = Path.lstat

            def redirected_lstat(path: Path, *args, **kwargs):
                info = path_lstat(path, *args, **kwargs)
                if path == game:
                    redirected = mock.Mock(wraps=info)
                    redirected.st_mode = info.st_mode
                    redirected.st_file_attributes = 0x0400
                    return redirected
                return info

            with mock.patch.object(
                Path, "lstat", autospec=True, side_effect=redirected_lstat
            ):
                with self.assertRaisesRegex(
                    context.ProjectContextError, "unredirected directories"
                ):
                    context.resolve_project_context(
                        project, game_layout="games/prototype"
                    )

    def test_resolved_redirect_cannot_escape_the_project(self) -> None:
        with _scratch() as root:
            project = root / "project"
            project.mkdir()
            _write_marker(project)
            (project / "src").mkdir()
            link = project / ".kit" / "linked"
            (link / "runtime").mkdir(parents=True)
            path_lstat = Path.lstat

            def redirected_lstat(path: Path, *args, **kwargs):
                info = path_lstat(path, *args, **kwargs)
                if path == link:
                    redirected = mock.Mock(wraps=info)
                    redirected.st_mode = info.st_mode
                    redirected.st_file_attributes = 0x0400
                    return redirected
                return info

            # Windows junction creation needs privileges the verifier cannot
            # assume.  Model the filesystem reparse attribute directly.
            with mock.patch.object(
                Path, "lstat", autospec=True, side_effect=redirected_lstat
            ):
                with self.assertRaisesRegex(
                    context.ProjectContextError, "unredirected directories"
                ):
                    context.resolve_project_context(
                        project, runtime_root=".kit/linked/runtime"
                    )

    def test_runtime_container_must_be_an_unredirected_directory(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            (project / "src").mkdir()
            (project / ".kit").write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(
                context.ProjectContextError, "unredirected director"
            ):
                context.resolve_project_context(project)

    def test_runtime_container_rejects_nonportable_casing(self) -> None:
        with _scratch() as project:
            _write_marker(project)
            (project / ".KIT").mkdir()

            with self.assertRaisesRegex(context.ProjectContextError, "unsafe casing"):
                context.resolve_project_context(project, game_layout=".")


class DispatchPathPolicy(unittest.TestCase):
    def test_forbidden_wins_and_unowned_is_distinct(self) -> None:
        with _scratch() as kit:
            _write_marker(kit)
            project = kit / "project"
            project.mkdir()
            found = context.resolve_project_context(project, game_layout=".")
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
            found = context.resolve_project_context(kit, game_layout=".")
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
