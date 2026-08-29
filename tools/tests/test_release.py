#!/usr/bin/env python3
"""Production-shaped tests for deterministic sanitized release archives."""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
REPOSITORY = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import release  # noqa: E402


def _scratch() -> Path:
    parent = REPOSITORY / ".checklogs"
    parent.mkdir(exist_ok=True)
    path = parent / f"release-test-{uuid.uuid4().hex}"
    path.mkdir()
    return path


def _remove_scratch(path: Path) -> None:
    resolved = path.resolve()
    parent = (REPOSITORY / ".checklogs").resolve()
    if resolved.parent != parent or not resolved.name.startswith("release-test-"):
        raise AssertionError(f"refusing to remove unexpected scratch path: {resolved}")

    def remove_readonly(function, target, _error) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(resolved, onerror=remove_readonly)


def _write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="")
    return path


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


def _fixture_repository(base: Path, *, license_file: bool = True,
                        version_file: bool = True) -> tuple[Path, str]:
    root = base / "source"
    root.mkdir()
    required = {
        ".agent-kit.json": '{"kind":"portable-agent-kit-root","schema":1}\r\n',
        "AGENTS.md": "# Agent rules\r\nNo secrets.\r\n",
        "ARCHITECTURE.md": (
            "# Architecture\r\n\r\n"
            "```mermaid\r\n"
            "graph TD\r\n"
            "    source_only_module_alpha --> source_only_module_beta\r\n"
            "```\r\n"
        ),
        "README.md": "# Test kit\r\nDeterministic release.\r\n",
        "bootstrap.py": "#!/usr/bin/env python3\r\nprint('bootstrap')\r\n",
        "check.py": "#!/usr/bin/env python3\r\nprint('check')\r\n",
        "dependencies.lock.json": '{"schema":1}\r\n',
        "kit.config.json": '{"schema":1,"game_root":"src","runtime_root":".kit/runtime"}\r\n',
        "kit": '#!/bin/sh\nexec python3 "$(dirname "$0")/kit.py" "$@"\n',
        "kit.cmd": '@echo off\r\npython "%~dp0kit.py" %*\r\n',
        "kit.py": "#!/usr/bin/env python3\r\nprint('usage: kit')\r\n",
        "tools/project_context.py": "# fixture project context\r\n",
        "tools/providers.py": "# fixture providers\r\n",
        "tools/release.py": "#!/usr/bin/env python3\r\n# fixture\r\n",
        "tools/runtime_paths.py": "# fixture runtime paths\r\n",
    }
    for relative, content in required.items():
        _write(root, relative, content)
    if license_file:
        _write(root, "LICENSE", "Approved fixture license text.\r\n")
    if version_file:
        _write(root, "VERSION", "1.2.3\r\n")

    # Every fixed reviewed surface must be present.  A release that silently
    # shrinks after a source deletion is not a complete kit.
    for relative in sorted(release.REQUIRED_KIT_FILES):
        if relative == "VERSION" and not version_file:
            continue
        if not (root / Path(relative)).exists():
            _write(root, relative, f"# Reviewed fixture: {relative}\r\n")

    # Tracked and untracked state that must never enter the archive.
    excluded = {
        "src/game.gd": "src-leak-marker",
        "docs/design/world.md": "design-leak-marker",
        "docs/retro/accepted.json": "accepted-leak-marker",
        "docs/retro/evidence/pack.json": "evidence-leak-marker",
        "docs/retro/queue/item.json": "queue-leak-marker",
        "docs/retro/runs/run.log": "run-leak-marker",
        "project.shape.json": '{"marker":"shape-leak-marker"}\n',
        "proposal.json": '{"marker":"proposal-leak-marker"}\n',
        "retro.config.json": "retro-config-leak-marker",
        "plan.html": "plan-leak-marker",
        "retro.html": "retro-html-leak-marker",
        ".env": "environment-secret-marker",
        "secrets/token.txt": "secret-directory-marker",
        "tools/not_reviewed.py": "tool-leak-marker",
        "__pycache__/cached.pyc": b"cache-leak-marker",
        ".pytest_cache/state": "pytest-leak-marker",
    }
    for relative, content in excluded.items():
        _write(root, relative, content)

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "release-test@example.invalid")
    _git(root, "config", "user.name", "Release Test")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "gc.auto", "0")
    _git(root, "config", "maintenance.auto", "false")
    _git(root, "add", "--all")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    return root, _git(root, "rev-parse", "HEAD")


def _member_contents(path: Path) -> dict[str, bytes]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as archive:
            return {info.filename: archive.read(info) for info in archive.infolist()}
    with tarfile.open(path, "r:*") as archive:
        out: dict[str, bytes] = {}
        for info in archive.getmembers():
            source = archive.extractfile(info)
            out[info.name] = source.read() if source is not None else b""
        return out


def _synthetic_smoke_archive(base: Path) -> Path:
    content = {
        ".agent-kit.json": b'{"kind":"portable-agent-kit-root","schema":1}\n',
        "AGENTS.md": b"# Agent rules\n",
        "README.md": b"# Synthetic kit\n",
        "VERSION": b"1.0.0\n",
        "LICENSE": b"Approved synthetic license.\n",
        "bootstrap.py": b"#!/usr/bin/env python3\n",
        "check.py": b"#!/usr/bin/env python3\n",
        "dependencies.lock.json": b'{"schema":1}\n',
        "kit.config.json": (
            b'{"schema":1,"game_root":"src","runtime_root":".kit/runtime"}\n'
        ),
        "kit": b'#!/bin/sh\nexec python3 "$(dirname "$0")/kit.py" "$@"\n',
        "kit.cmd": b'@echo off\npython "%~dp0kit.py" %*\n',
        "kit.py": (
            b"#!/usr/bin/env python3\n"
            b"import sys\n"
            b"from pathlib import Path\n"
            b"sys.path.insert(0, str(Path(__file__).parent / 'tools'))\n"
            b"import process_supervisor\n"
            b"print('usage: kit')\n"
        ),
        "tools/project_context.py": b"# synthetic project context\n",
        "tools/providers.py": b"# synthetic providers\n",
        "tools/release.py": b"# synthetic release tool\n",
        "tools/runtime_paths.py": b"# synthetic runtime paths\n",
    }
    for relative in sorted(release.REQUIRED_KIT_FILES):
        content.setdefault(relative, f"# synthetic {relative}\n".encode("utf-8"))
    content["ARCHITECTURE.md"] = release.CANONICAL_ARCHITECTURE
    content["arch.rules.json"] = release.CANONICAL_ARCH_RULES
    files = [
        release.ReleaseFile(
            name,
            value,
            0o755 if name.endswith(".py") or name == "kit" else 0o644,
        )
        for name, value in sorted(content.items())
    ]
    manifest = release._manifest(
        "1.0.0",
        ["LICENSE"],
        files,
        {"commit": "a" * 40, "dirty": False},
        {
            "receipt_trust": "portable-policy",
            "identity_model": "portable-policy-audit",
            "project_receipt_trust": "not-applicable-no-project-state",
        },
    )
    members = {item.path: (item.content, item.mode) for item in files}
    members[release.MANIFEST_PATH] = (release._canonical_json(manifest), 0o644)
    archive = base / "synthetic-smoke.zip"
    release._write_zip(archive, members)
    return archive


class ReleaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = _scratch()

    def tearDown(self) -> None:
        _remove_scratch(self.scratch)


class TestDeterministicBuild(ReleaseTestCase):
    def test_zip_is_byte_identical_sanitized_and_self_describing(self) -> None:
        root, commit = _fixture_repository(self.scratch)
        output_one = self.scratch / "out" / "kit-one.zip"
        output_two = self.scratch / "out" / "kit-two.zip"

        first = release.build_release(root, output_one)
        # Filesystem timestamps are deliberately irrelevant to archive bytes.
        os.utime(root / "README.md", (1_700_000_000, 1_700_000_000))
        second = release.build_release(root, output_two)

        self.assertEqual(output_one.read_bytes(), output_two.read_bytes())
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        self.assertEqual(first["source"], {"commit": commit, "dirty": False})
        self.assertEqual(first["authority_evidence"], {
            "receipt_trust": "portable-policy",
            "identity_model": "portable-policy-audit",
            "project_receipt_trust": "no-exact-authority-event",
        })

        inspected = release.inspect_archive(output_one)
        verified = release.verify_archive(output_one)
        self.assertTrue(verified["ok"])
        manifest = inspected["manifest"]
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(manifest["source"], {"commit": commit, "dirty": False})
        self.assertEqual(manifest["authority_evidence"], {
            "receipt_trust": "portable-policy",
            "identity_model": "portable-policy-audit",
            "project_receipt_trust": "no-exact-authority-event",
        })
        self.assertEqual(manifest["license_files"], ["LICENSE"])
        manifest_text = json.dumps(manifest, sort_keys=True)
        self.assertNotIn(str(root.resolve()).replace("\\", "/"), manifest_text)

        contents = _member_contents(output_one)
        expected = set(release.REQUIRED_KIT_FILES) | {
            "LICENSE", release.MANIFEST_PATH,
        }
        self.assertEqual(set(contents), expected)
        self.assertNotIn(b"\r", contents["README.md"])
        self.assertEqual(
            contents["ARCHITECTURE.md"], release.CANONICAL_ARCHITECTURE
        )
        self.assertEqual(
            contents["arch.rules.json"], release.CANONICAL_ARCH_RULES
        )
        combined = b"\n".join(contents.values())
        for marker in (
                b"src-leak-marker", b"design-leak-marker",
                b"accepted-leak-marker", b"evidence-leak-marker", b"queue-leak-marker",
                b"run-leak-marker", b"shape-leak-marker", b"proposal-leak-marker",
                b"retro-config-leak-marker", b"plan-leak-marker", b"retro-html-leak-marker",
                b"environment-secret-marker", b"secret-directory-marker",
                b"tool-leak-marker", b"cache-leak-marker", b"pytest-leak-marker"):
            self.assertNotIn(marker, combined)
        self.assertNotIn(b"source_only_module_alpha", combined)
        self.assertNotIn(b"source_only_module_beta", combined)

        with zipfile.ZipFile(output_one, "r") as archive:
            names = [info.filename for info in archive.infolist()]
            self.assertEqual(names, sorted(names))
            self.assertTrue(all(info.date_time == release.FIXED_ZIP_TIME
                                for info in archive.infolist()))
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED
                                for info in archive.infolist()))
            modes = {info.filename: stat.S_IMODE(info.external_attr >> 16)
                     for info in archive.infolist()}
        self.assertEqual(modes["README.md"], 0o644)
        self.assertEqual(modes["bootstrap.py"], 0o755)
        self.assertEqual(modes["kit"], 0o755)

    def test_compressed_tar_is_byte_identical_with_fixed_metadata(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        first = self.scratch / "out" / "kit-one.tar.gz"
        second = self.scratch / "out" / "kit-two.tgz"

        release.build_release(root, first)
        release.build_release(root, second)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.read_bytes()[4:8], b"\x00\x00\x00\x00")
        self.assertTrue(release.verify_archive(first)["ok"])
        with tarfile.open(first, "r:gz") as archive:
            members = archive.getmembers()
        self.assertEqual([member.name for member in members],
                         sorted(member.name for member in members))
        self.assertTrue(all(member.mtime == 0 and member.uid == 0 and member.gid == 0
                            and member.uname == "" and member.gname == ""
                            for member in members))

    def test_gzip_wrapper_uses_canonical_stored_deflate_blocks(self) -> None:
        self.assertEqual(
            "1f8b08000000000000ff010300fcff616263c241243503000000",
            release._stored_gzip(b"abc").hex(),
        )

    def test_project_architecture_rules_are_replaced_before_release(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        project_marker = "project_inventory_must_depend_on_project_combat"
        _write(
            root,
            "arch.rules.json",
            json.dumps({
                "module_depth": 2,
                "modules": {
                    "scripts/project_inventory": {
                        "description": project_marker,
                        "may_depend_on": ["scripts/project_combat"],
                    },
                },
            }) + "\n",
        )
        _git(root, "add", "arch.rules.json")
        _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "project rules")
        archive = self.scratch / "out" / "canonical-rules.zip"

        release.build_release(root, archive)

        contents = _member_contents(archive)
        self.assertEqual(
            release.CANONICAL_ARCH_RULES, contents["arch.rules.json"]
        )
        self.assertNotIn(project_marker.encode("utf-8"), b"\n".join(contents.values()))

    def test_dirty_source_is_inspectable_but_cannot_build_production_release(self) -> None:
        root, commit = _fixture_repository(self.scratch)
        _write(root, "private/operator-token.txt", "untracked-private-marker")
        output = self.scratch / "out" / "dirty.zip"

        with self.assertRaisesRegex(release.ReleaseError, "requires a clean source"):
            release.build_release(root, output)

        self.assertFalse(output.exists())
        source, _porcelain = release._git_state(root)
        self.assertEqual(source, {"commit": commit, "dirty": True})

    def test_dirty_manifest_is_visible_to_inspection_but_rejected_as_release(self) -> None:
        archive = _synthetic_smoke_archive(self.scratch)
        contents = _member_contents(archive)
        manifest = json.loads(contents[release.MANIFEST_PATH])
        manifest["source"]["dirty"] = True
        contents[release.MANIFEST_PATH] = release._canonical_json(manifest)
        members = {
            name: (content, 0o755 if name.endswith(".py") or name == "kit" else 0o644)
            for name, content in contents.items()
        }
        release._write_zip(archive, members)

        inspected = release.inspect_archive(archive)
        self.assertTrue(inspected["manifest"]["source"]["dirty"])
        with self.assertRaisesRegex(release.ReleaseError, "not production provenance"):
            release.verify_archive(archive)

    def test_archive_requires_explicit_portable_policy_authority_label(self) -> None:
        archive = _synthetic_smoke_archive(self.scratch)
        contents = _member_contents(archive)
        manifest = json.loads(contents[release.MANIFEST_PATH])
        for field, invalid in (
            ("receipt_trust", "local-audit-matched"),
            ("identity_model", "authenticated-human"),
            ("project_receipt_trust", "invalid"),
        ):
            with self.subTest(field=field):
                changed = dict(manifest)
                changed["authority_evidence"] = dict(manifest["authority_evidence"])
                changed["authority_evidence"][field] = invalid
                contents[release.MANIFEST_PATH] = release._canonical_json(changed)
                members = {
                    name: (
                        content,
                        0o755 if name.endswith(".py") or name == "kit" else 0o644,
                    )
                    for name, content in contents.items()
                }
                release._write_zip(archive, members)
                with self.assertRaisesRegex(
                    release.ReleaseError, "receipt_trust|identity model|receipt trust"
                ):
                    release.verify_archive(archive)

    def test_no_project_state_records_not_applicable_portable_policy(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        (root / "proposal.json").unlink()
        (root / "project.shape.json").unlink()
        _git(root, "add", "--all")
        _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "remove project state")

        report = release.build_release(root, self.scratch / "out" / "kit.zip")

        self.assertEqual(report["authority_evidence"], {
            "receipt_trust": "portable-policy",
            "identity_model": "portable-policy-audit",
            "project_receipt_trust": "not-applicable-no-project-state",
        })

    def test_present_invalid_project_receipt_blocks_before_exclusion(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        invalid = {
            "receipt_trust": "invalid",
            "receipt_reasons": ["private local receipt contradicts durable event"],
        }
        with mock.patch(
            "proposal_authority.exact_approval_state", return_value=invalid
        ):
            with self.assertRaisesRegex(
                release.ReleaseError,
                "pre-exclusion authority receipt is invalid.*contradicts",
            ):
                release.build_release(root, self.scratch / "out" / "kit.zip")

    def test_project_receipt_is_reauthenticated_after_input_collection(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        before = {
            "receipt_trust": "local-audit-matched",
            "receipt_reasons": ["private receipt matches"],
        }
        after = {
            "receipt_trust": "invalid",
            "receipt_reasons": ["private receipt changed during build"],
        }
        with mock.patch(
            "proposal_authority.exact_approval_state",
            side_effect=(before, after),
        ):
            with self.assertRaisesRegex(
                release.ReleaseError,
                "pre-exclusion authority receipt is invalid.*changed during build",
            ):
                release.build_release(root, self.scratch / "out" / "kit.zip")

    def test_partial_project_authority_state_fails_closed(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        (root / "proposal.json").unlink()
        _git(root, "add", "--all")
        _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "partial state")

        with self.assertRaisesRegex(
            release.ReleaseError, "must either both exist or both be absent"
        ):
            release.build_release(root, self.scratch / "out" / "kit.zip")


class TestSemanticIsolation(ReleaseTestCase):
    def test_project_name_cannot_leak_into_reviewed_kit_text(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        _write(
            root,
            "src/project.godot",
            '[application]\nconfig/name="Fixture Adventure"\n',
        )
        _write(root, "README.md", "# Test kit\nFixture Adventure setup notes.\n")

        with self.assertRaisesRegex(
            release.ReleaseError, "excluded project-derived content.*configured project name"
        ):
            release.collect_files(root)

    def test_concrete_game_path_cannot_leak_into_reviewed_kit_text(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        _write(
            root,
            "src/scripts/logic/project_only_rule.gd",
            "extends RefCounted\n",
        )
        _write(
            root,
            "README.md",
            "# Test kit\nSee scripts/logic/project_only_rule.gd for details.\n",
        )

        with self.assertRaisesRegex(
            release.ReleaseError, "excluded project-derived content.*game file path"
        ):
            release.collect_files(root)

    def test_design_statement_cannot_be_copied_into_reviewed_kit_text(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        statement = (
            "The fictional project resolves its opening choice through a unique ritual "
            "that no reusable kit document should repeat."
        )
        _write(root, "docs/design/story/opening.md", statement + "\n")
        _write(root, "README.md", "# Test kit\n" + statement + "\n")

        with self.assertRaisesRegex(
            release.ReleaseError, "excluded project-derived content.*design statement"
        ):
            release.collect_files(root)


class TestRequiredMetadata(ReleaseTestCase):
    def test_missing_license_refuses_release_without_inventing_one(self) -> None:
        root, _commit = _fixture_repository(self.scratch, license_file=False)
        with self.assertRaisesRegex(release.ReleaseError, "legal metadata is absent"):
            release.build_release(root, self.scratch / "out" / "missing-license.zip")

    def test_missing_or_invalid_version_refuses_release(self) -> None:
        missing = self.scratch / "missing"
        missing.mkdir()
        root, _commit = _fixture_repository(missing, version_file=False)
        with self.assertRaisesRegex(release.ReleaseError, "VERSION metadata is absent"):
            release.build_release(root, self.scratch / "out" / "missing-version.zip")

        invalid = self.scratch / "invalid"
        invalid.mkdir()
        invalid_root, _commit = _fixture_repository(invalid)
        _write(invalid_root, "VERSION", "not a version with spaces\n")
        with self.assertRaisesRegex(release.ReleaseError, "VERSION must be one line"):
            release.collect_files(invalid_root)

    def test_output_inside_source_tree_is_refused(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        with self.assertRaisesRegex(release.ReleaseError, "outside the source repository"):
            release.build_release(root, root / "dist" / "kit.zip")


class TestSourcePathSafety(ReleaseTestCase):
    def test_canonical_release_surface_sets_are_complete_and_exact(self) -> None:
        actual_tools = {
            path.relative_to(REPOSITORY).as_posix()
            for path in (REPOSITORY / "tools").glob("*.py")
        }
        actual_tests = {
            path.relative_to(REPOSITORY).as_posix()
            for path in (REPOSITORY / "tools" / "tests").glob("test_*.py")
        }
        actual_skills = {
            path.relative_to(REPOSITORY).as_posix()
            for path in (REPOSITORY / ".agents" / "skills").glob("*/SKILL.md")
        }
        actual_agents = {
            path.relative_to(REPOSITORY).as_posix()
            for path in (REPOSITORY / ".github" / "agents").glob("*.agent.md")
        }

        self.assertEqual(actual_tools, set(release.TOOL_FILES))
        self.assertTrue(actual_tests.issubset(release.VALIDATION_FILES))
        self.assertEqual(actual_skills, set(release.REQUIRED_SKILL_FILES))
        self.assertEqual(actual_agents, set(release.REQUIRED_AGENT_FILES))

    def test_unreviewed_skill_or_persona_is_not_pattern_allowlisted(self) -> None:
        self.assertFalse(release.is_allowlisted(
            ".agents/skills/unreviewed/SKILL.md"
        ))
        self.assertFalse(release.is_allowlisted(
            ".github/agents/unreviewed.agent.md"
        ))

    def test_allowlisted_source_symlink_is_refused_before_reading(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        readme = root / "README.md"
        path_type = type(readme)
        original = path_type.is_symlink

        def fake_is_symlink(path: Path) -> bool:
            return path == readme or original(path)

        with mock.patch.object(path_type, "is_symlink", fake_is_symlink):
            with self.assertRaisesRegex(release.ReleaseError, "source is a symlink"):
                release.collect_files(root)

    def test_allowlisted_source_reparse_point_is_refused_before_reading(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        docs = root / "docs"
        original = release._is_reparse_point

        def fake_is_reparse_point(path: Path) -> bool:
            return path == docs or original(path)

        with mock.patch.object(
            release, "_is_reparse_point", side_effect=fake_is_reparse_point
        ):
            with self.assertRaisesRegex(release.ReleaseError, "source is a reparse point"):
                release.collect_files(root)

    def test_missing_reviewed_surface_refuses_incomplete_release(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        (root / "tools" / "tests" / "test_engine_boundary.py").unlink()

        with self.assertRaisesRegex(release.ReleaseError, "required kit file.*test_engine_boundary"):
            release.collect_files(root)


class TestArchivePathSafety(ReleaseTestCase):
    def _zip_with(self, name: str, *, symlink: bool = False) -> Path:
        path = self.scratch / f"malicious-{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo(name, release.FIXED_ZIP_TIME)
            info.create_system = 3
            mode = stat.S_IFLNK | 0o777 if symlink else stat.S_IFREG | 0o644
            info.external_attr = mode << 16
            archive.writestr(info, b"../../escape")
        return path

    def _tar_with(self, name: str, *, symlink: bool = False) -> Path:
        path = self.scratch / f"malicious-{uuid.uuid4().hex}.tar"
        with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.mtime = 0
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "../../escape"
                archive.addfile(info)
            else:
                content = b"escape"
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return path

    def test_zip_and_tar_traversal_are_rejected_without_extraction(self) -> None:
        cases = [
            self._zip_with("../escape.txt"),
            self._zip_with("C:/escape.txt"),
            self._tar_with("../../escape.txt"),
            self._tar_with("/absolute/escape.txt"),
        ]
        for archive in cases:
            with self.subTest(archive=archive.name):
                with self.assertRaisesRegex(release.ReleaseError,
                                            "archive member path|absolute archive"):
                    release.inspect_archive(archive)
        self.assertFalse((self.scratch.parent / "escape.txt").exists())

    def test_zip_and_tar_symlinks_are_rejected(self) -> None:
        for archive in (
                self._zip_with("README.md", symlink=True),
                self._tar_with("README.md", symlink=True)):
            with self.subTest(archive=archive.name):
                with self.assertRaisesRegex(release.ReleaseError, "symlink"):
                    release.inspect_archive(archive)


class TestPrivateRuntimePolicy(ReleaseTestCase):
    def test_configured_private_runtime_is_the_only_source_local_scratch(self) -> None:
        root = self.scratch / "source"
        root.mkdir()
        _write(
            root,
            "kit.config.json",
            '{"schema":1,"runtime_root":".kit/runtime"}\n',
        )

        runtime = release._private_runtime_root(root)

        self.assertEqual((root / ".kit" / "runtime").resolve(), runtime)

    def test_traversing_private_runtime_fails_closed(self) -> None:
        root = self.scratch / "source"
        root.mkdir()
        _write(
            root,
            "kit.config.json",
            '{"schema":1,"runtime_root":"../outside"}\n',
        )

        with self.assertRaisesRegex(release.ReleaseError, "project-relative path"):
            release._private_runtime_root(root)

    def test_repository_local_runtime_outside_dot_kit_fails_closed(self) -> None:
        root = self.scratch / "source"
        root.mkdir()
        for configured in ("state/private", "src/runtime", "docs/private", ".kit"):
            with self.subTest(configured=configured):
                _write(
                    root,
                    "kit.config.json",
                    json.dumps({"schema": 1, "runtime_root": configured}) + "\n",
                )
                with self.assertRaisesRegex(release.ReleaseError, "descendant.*\\.kit"):
                    release._private_runtime_root(root)

    def test_private_runtime_container_cannot_be_a_file(self) -> None:
        root = self.scratch / "source"
        root.mkdir()
        _write(
            root,
            "kit.config.json",
            '{"schema":1,"runtime_root":".kit/runtime"}\n',
        )
        _write(root, ".kit", "not a private directory")

        with self.assertRaisesRegex(release.ReleaseError, "unredirected directory"):
            release._private_runtime_root(root)


class TestReleaseSmoke(ReleaseTestCase):
    def test_verified_archive_starts_its_platform_launcher(self) -> None:
        archive = _synthetic_smoke_archive(self.scratch)
        workspace = self.scratch / "smoke-workspace"

        report = release.smoke_archive(archive, workspace)

        self.assertTrue(report["ok"])
        self.assertEqual(
            "kit.cmd" if os.name == "nt" else "kit",
            report["smoke"]["launcher"],
        )
        self.assertTrue((workspace / "kit.py").is_file())
        self.assertTrue((workspace / "tools" / "process_supervisor.py").is_file())
        self.assertEqual(
            release.CANONICAL_ARCH_RULES,
            (workspace / "arch.rules.json").read_bytes(),
        )
        self.assertTrue(
            (workspace / "tools" / "tests" / "test_native_process_containment.py").is_file()
        )
        with self.assertRaisesRegex(release.ReleaseError, "already exists"):
            release.smoke_archive(archive, workspace)


class TestVerification(ReleaseTestCase):
    def test_manifest_cannot_authorize_project_generated_architecture(self) -> None:
        archive = _synthetic_smoke_archive(self.scratch)
        contents = _member_contents(archive)
        manifest = json.loads(contents[release.MANIFEST_PATH])
        contaminated = (
            b"# Architecture\n\n```mermaid\ngraph TD\n"
            b"project_inventory --> project_combat\n```\n"
        )
        entry = next(
            item for item in manifest["files"]
            if item["path"] == "ARCHITECTURE.md"
        )
        entry["bytes"] = len(contaminated)
        entry["sha256"] = release.hashlib.sha256(contaminated).hexdigest()
        contents["ARCHITECTURE.md"] = contaminated
        contents[release.MANIFEST_PATH] = release._canonical_json(manifest)
        members = {
            name: (
                content,
                0o755 if name.endswith(".py") or name == "kit" else 0o644,
            )
            for name, content in contents.items()
        }
        release._write_zip(archive, members)

        self.assertEqual(release.inspect_archive(archive)["format"], "zip")
        with self.assertRaisesRegex(
            release.ReleaseError, "canonical empty release template"
        ):
            release.verify_archive(archive)

    def test_manifest_cannot_authorize_project_architecture_rules(self) -> None:
        archive = _synthetic_smoke_archive(self.scratch)
        contents = _member_contents(archive)
        manifest = json.loads(contents[release.MANIFEST_PATH])
        contaminated = json.dumps({
            "module_depth": 2,
            "modules": {"scripts/project_combat": {"may_depend_on": []}},
        }).encode("utf-8") + b"\n"
        entry = next(
            item for item in manifest["files"]
            if item["path"] == "arch.rules.json"
        )
        entry["bytes"] = len(contaminated)
        entry["sha256"] = release.hashlib.sha256(contaminated).hexdigest()
        contents["arch.rules.json"] = contaminated
        contents[release.MANIFEST_PATH] = release._canonical_json(manifest)
        members = {
            name: (
                content,
                0o755 if name.endswith(".py") or name == "kit" else 0o644,
            )
            for name, content in contents.items()
        }
        release._write_zip(archive, members)

        self.assertEqual(release.inspect_archive(archive)["format"], "zip")
        with self.assertRaisesRegex(
            release.ReleaseError, "canonical genre-neutral release template"
        ):
            release.verify_archive(archive)

    def test_tampered_member_fails_hash_verification(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        original = self.scratch / "out" / "original.zip"
        tampered = self.scratch / "out" / "tampered.zip"
        release.build_release(root, original)
        inspected = release.inspect_archive(original)
        modes = {entry["path"]: int(entry["mode"], 8)
                 for entry in inspected["members"]}
        members = {name: (content, modes[name])
                   for name, content in _member_contents(original).items()}
        members["README.md"] = (b"tampered\n", modes["README.md"])
        release._write_zip(tampered, members)

        # Structural inspection remains useful, but verification catches content drift.
        self.assertEqual(release.inspect_archive(tampered)["format"], "zip")
        with self.assertRaisesRegex(release.ReleaseError, "size does not match|SHA-256"):
            release.verify_archive(tampered)

    def test_manifest_cannot_authorize_a_nonallowlisted_file(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        original = self.scratch / "out" / "original.zip"
        malicious = self.scratch / "out" / "manifest-leak.zip"
        release.build_release(root, original)
        contents = _member_contents(original)
        manifest = json.loads(contents[release.MANIFEST_PATH])
        leaked = b"manifest-authorized-secret"
        manifest["files"].append({
            "path": "secrets.txt",
            "bytes": len(leaked),
            "sha256": release.hashlib.sha256(leaked).hexdigest(),
            "mode": "0644",
        })
        manifest["files"] = sorted(manifest["files"], key=lambda entry: entry["path"])
        contents[release.MANIFEST_PATH] = release._canonical_json(manifest)
        members = {
            name: (content, 0o755 if name.endswith(".py") else 0o644)
            for name, content in contents.items()
        }
        members["secrets.txt"] = (leaked, 0o644)
        release._write_zip(malicious, members)

        with self.assertRaisesRegex(release.ReleaseError, "non-allowlisted path"):
            release.verify_archive(malicious)


if __name__ == "__main__":
    unittest.main(verbosity=2)
