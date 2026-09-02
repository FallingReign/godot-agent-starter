#!/usr/bin/env python3
"""Production-shaped tests for deterministic sanitized release archives."""
from __future__ import annotations

import hashlib
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
import process_supervisor  # noqa: E402


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
    executable = process_supervisor.resolve_ordinary_executable(
        "git", excluded_roots=(root, REPOSITORY)
    )
    completed = subprocess.run(
        [executable, "-C", str(root), *arguments],
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
    source_members = {
        item.path: release.ArchiveMember(item.path, item.content, item.mode)
        for item in files
    }
    files.extend(release._generated_install_release_files("1.0.0", source_members))
    files.sort(key=lambda item: item.path)
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


def _historic_0_2_archive(base: Path) -> Path:
    content = {
        path: f"# historic {path}\n".encode("utf-8")
        for path in release.LEGACY_0_2_0_REQUIRED_KIT_FILES
    }
    content.update({
        ".agent-kit.json": b'{"kind":"portable-agent-kit-root","schema":1}\n',
        "ARCHITECTURE.md": release.CANONICAL_ARCHITECTURE,
        "LICENSE": b"Approved historic license.\n",
        "VERSION": b"0.2.0\n",
        "arch.rules.json": release.CANONICAL_ARCH_RULES,
        "kit": b"#!/bin/sh\nexit 0\n",
    })
    files = [
        release.ReleaseFile(
            name,
            value,
            0o755 if name.endswith(".py") or name == "kit" else 0o644,
        )
        for name, value in sorted(content.items())
    ]
    manifest = release._manifest(
        "0.2.0",
        ["LICENSE"],
        files,
        {"commit": release.LEGACY_0_2_0_SOURCE_COMMIT, "dirty": False},
        {
            "receipt_trust": "portable-policy",
            "identity_model": "portable-policy-audit",
            "project_receipt_trust": "not-applicable-no-project-state",
        },
    )
    members = {item.path: (item.content, item.mode) for item in files}
    members[release.MANIFEST_PATH] = (release._canonical_json(manifest), 0o644)
    archive = base / "historic-0.2.0.zip"
    release._write_zip(archive, members)
    return archive


def _extract_exact(path: Path, destination: Path) -> None:
    report = release.inspect_archive(path)
    modes = {
        item["path"]: int(item["mode"], 8)
        for item in report["members"]
    }
    for relative, content in _member_contents(path).items():
        target = _write(destination, relative, content)
        target.chmod(modes[relative])


def _make_directory_redirect(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            [
                process_supervisor.windows_command_processor(),
                "/d", "/c", "mklink", "/J", str(link), str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr or completed.stdout)
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_directory_redirect(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink(missing_ok=True)


class ReleaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = _scratch()

    def tearDown(self) -> None:
        _remove_scratch(self.scratch)


class TestDeterministicBuild(ReleaseTestCase):
    def test_clean_source_checkout_has_the_built_release_identity(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        output = self.scratch / "out" / "kit.zip"

        source_report, source_members = release.read_verified_source_checkout(root)
        built_report = release.build_release(root, output)

        self.assertEqual("source", source_report["format"])
        self.assertEqual(
            built_report["archive_sha256"], source_report["archive_sha256"]
        )
        self.assertEqual(
            set(_member_contents(output)), set(source_members)
        )

    def test_controller_reader_accepts_matching_source_and_extracted_release(
        self,
    ) -> None:
        root, _commit = _fixture_repository(self.scratch)
        output = self.scratch / "out" / "kit.zip"
        built = release.build_release(root, output)
        extracted = self.scratch / "extracted"
        extracted.mkdir()
        _extract_exact(output, extracted)

        source_report, _source_members = release.read_verified_controller(root)
        directory_report, _directory_members = release.read_verified_controller(
            extracted
        )

        self.assertEqual("source", source_report["format"])
        self.assertEqual("directory", directory_report["format"])
        self.assertEqual(built["archive_sha256"], source_report["archive_sha256"])
        self.assertEqual(
            built["archive_sha256"], directory_report["archive_sha256"]
        )

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
        install_manifest = inspected["install_manifest"]
        self.assertIsInstance(install_manifest, dict)
        self.assertEqual(install_manifest["kit_version"], "1.2.3")
        self.assertEqual(install_manifest["core_layout"],
                         "versioned-by-archive-sha256")
        surface_paths = {
            item["path"]
            for item in [
                *install_manifest["owned_files"],
                *install_manifest["managed_blocks"],
            ]
        }
        self.assertIn(".agent-kit/launcher.py", surface_paths)
        self.assertIn(".github/workflows/agent-kit-ci.yml", surface_paths)
        self.assertNotIn("README.md", surface_paths)
        self.assertNotIn("LICENSE", surface_paths)
        self.assertNotIn("VERSION", surface_paths)
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
            "LICENSE", release.MANIFEST_PATH, *release.GENERATED_INSTALL_FILES,
        }
        self.assertEqual(set(contents), expected)
        self.assertNotIn("tools/tests/test_lifecycle_e2e.py", contents)
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

    def test_all_containers_share_one_lifecycle_identity_and_keep_raw_digests(
            self,
    ) -> None:
        root, _commit = _fixture_repository(self.scratch)
        outputs = [
            self.scratch / "out" / "kit.zip",
            self.scratch / "out" / "kit.tar",
            self.scratch / "out" / "kit.tgz",
        ]

        reports = [release.build_release(root, output) for output in outputs]
        directory = self.scratch / "extracted"
        directory.mkdir()
        _extract_exact(outputs[0], directory)
        directory_report, _members = release.read_verified_directory(directory)

        identities = {
            report["archive_sha256"]
            for report in [*reports, directory_report]
        }
        self.assertEqual(1, len(identities))
        raw_digests = []
        for output, report in zip(outputs, reports, strict=True):
            expected = hashlib.sha256(output.read_bytes()).hexdigest()
            self.assertEqual(expected, report["container_sha256"])
            raw_digests.append(expected)
        self.assertEqual(3, len(set(raw_digests)))
        self.assertIsNone(directory_report["container_sha256"])

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
        with self.assertRaisesRegex(release.ReleaseError, "requires a clean source"):
            release.read_verified_source_checkout(root)

        self.assertFalse(output.exists())
        source, _porcelain = release._git_state(root)
        self.assertEqual(source, {"commit": commit, "dirty": True})

    def test_source_checkout_change_during_collection_is_refused(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        original = release.collect_files

        def collect_then_change(source: Path) -> tuple[
            str, list[str], list[release.ReleaseFile]
        ]:
            result = original(source)
            _write(source, "README.md", "# Changed during collection\n")
            return result

        with mock.patch.object(
            release, "collect_files", side_effect=collect_then_change
        ):
            with self.assertRaisesRegex(
                release.ReleaseError, "source repository changed"
            ):
                release.read_verified_source_checkout(root)

    def test_git_metadata_child_ignores_inherited_process_controls(self) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def completed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environment = kwargs.get("env")
            self.assertIsInstance(environment, dict)
            calls.append((command, dict(environment)))
            stdout = "a" * 40 + "\n" if "rev-parse" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with mock.patch.object(
            release.process_supervisor,
            "resolve_ordinary_executable",
            return_value="trusted-git",
        ), mock.patch.object(
            release.subprocess, "run", side_effect=completed
        ), mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": "attacker-repository",
                "GIT_EXTERNAL_DIFF": "attacker-diff",
                "GIT_CONFIG_GLOBAL": "attacker-config",
                "GIT_PAGER": "attacker-pager",
            },
        ):
            source, _fingerprint = release._git_state(self.scratch)

        self.assertEqual({"commit": "a" * 40, "dirty": False}, source)
        self.assertEqual(2, len(calls))
        for command, environment in calls:
            self.assertEqual("trusted-git", command[0])
            self.assertIn("--no-pager", command)
            self.assertIn("core.fsmonitor=false", command)
            self.assertIn(f"core.hooksPath={os.devnull}", command)
            self.assertIn("diff.external=", command)
            self.assertIn("diff.trustExitCode=false", command)
            self.assertIn(f"core.attributesFile={os.devnull}", command)
            self.assertFalse(
                [
                    key
                    for key in environment
                    if key.upper().startswith("GIT_")
                    and key
                    not in {
                        "GIT_CONFIG_GLOBAL",
                        "GIT_CONFIG_NOSYSTEM",
                        "GIT_OPTIONAL_LOCKS",
                        "GIT_TERMINAL_PROMPT",
                    }
                ]
            )
            self.assertEqual(os.devnull, environment["GIT_CONFIG_GLOBAL"])
            self.assertEqual("1", environment["GIT_CONFIG_NOSYSTEM"])
            self.assertEqual("0", environment["GIT_OPTIONAL_LOCKS"])
            self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])

    def test_git_metadata_child_disables_repository_fsmonitor(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        sentinel = root / "fsmonitor-ran.txt"
        monitor = f'echo fsmonitor-ran > "{sentinel.as_posix()}"'
        _git(root, "config", "core.fsmonitor", monitor)

        _git(root, "status", "--porcelain=v1", "--untracked-files=all")
        self.assertTrue(sentinel.is_file(), "fixture did not exercise fsmonitor")
        sentinel.unlink()

        source, _fingerprint = release._git_state(root)

        self.assertFalse(source["dirty"])
        self.assertFalse(sentinel.exists())

    def test_build_refuses_a_redirected_output_parent(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        external = self.scratch / "external-output"
        external.mkdir()
        redirected = self.scratch / "redirected-output"
        _make_directory_redirect(redirected, external)
        try:
            with self.assertRaisesRegex(
                release.ReleaseError, "unredirected|redirected components"
            ):
                release.build_release(root, redirected / "kit.zip")
            self.assertEqual([], list(external.iterdir()))
        finally:
            _remove_directory_redirect(redirected)

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


class TestManagedInstallContract(ReleaseTestCase):
    def _run_generated_launcher(
        self, project: Path, launcher_content: bytes
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["KIT_PYTHON"] = sys.executable
        if os.name == "nt":
            stable = _write(
                project,
                "kit.cmd",
                release._managed_windows_launcher(launcher_content),
            )
            command = [
                process_supervisor.windows_command_processor(),
                "/d", "/c", str(stable),
            ]
        else:
            stable = _write(
                project,
                "kit",
                release._managed_unix_launcher(launcher_content),
            )
            stable.chmod(0o755)
            command = [str(stable)]
        return subprocess.run(
            command,
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

    def test_generated_launcher_executes_only_exact_regular_bytes(self) -> None:
        source = (
            b"from pathlib import Path\n"
            b"Path(__file__).parent.parent.joinpath('executed').write_text('yes')\n"
        )

        regular = self.scratch / "regular-project"
        (regular / ".agent-kit").mkdir(parents=True)
        _write(regular, ".agent-kit/launcher.py", source)
        shadow = (
            "from pathlib import Path\n"
            "Path(__file__).parent.joinpath('shadow-ran').write_text('bad')\n"
        )
        _write(regular, "sitecustomize.py", shadow)
        _write(regular, "base64.py", shadow)
        _write(regular, ".agent-kit/hashlib.py", shadow)
        self.assertEqual(0, self._run_generated_launcher(regular, source).returncode)
        self.assertEqual("yes", (regular / "executed").read_text(encoding="utf-8"))
        self.assertFalse((regular / "shadow-ran").exists())
        self.assertFalse((regular / ".agent-kit" / "shadow-ran").exists())

        changed = self.scratch / "changed-project"
        (changed / ".agent-kit").mkdir(parents=True)
        _write(
            changed,
            ".agent-kit/launcher.py",
            source + b"Path(__file__).parent.parent.joinpath('changed').write_text('bad')\n",
        )
        outcome = self._run_generated_launcher(changed, source)
        self.assertEqual(3, outcome.returncode, outcome.stdout + outcome.stderr)
        self.assertFalse((changed / "executed").exists())
        self.assertFalse((changed / "changed").exists())

        linked = self.scratch / "linked-project"
        (linked / ".agent-kit").mkdir(parents=True)
        external = _write(self.scratch, "external-launcher.py", source)
        os.link(external, linked / ".agent-kit" / "launcher.py")
        outcome = self._run_generated_launcher(linked, source)
        self.assertEqual(3, outcome.returncode, outcome.stdout + outcome.stderr)
        self.assertFalse((linked / "executed").exists())

    def test_every_stable_launcher_disables_bytecode_writes(self) -> None:
        launcher_content = b"print('managed launcher')\n"
        for rendered in (
            release._managed_unix_launcher(launcher_content),
            release._managed_windows_launcher(launcher_content),
            release._flat_unix_launcher(launcher_content),
            release._flat_windows_launcher(launcher_content),
        ):
            with self.subTest(platform="windows" if b"@echo off" in rendered else "unix"):
                self.assertIn(b"-B -I -S -c", rendered)

    def test_generated_launcher_refuses_redirected_launcher_folder(self) -> None:
        source = (
            b"from pathlib import Path\n"
            b"Path(__file__).parent.parent.joinpath('executed').write_text('bad')\n"
        )
        project = self.scratch / "redirected-project"
        project.mkdir()
        external = self.scratch / "external-agent-kit"
        external.mkdir()
        _write(external, "launcher.py", source)
        redirected = project / ".agent-kit"
        if os.name == "nt":
            created = subprocess.run(
                [
                    process_supervisor.windows_command_processor(),
                    "/d", "/c", "mklink", "/J", str(redirected), str(external),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            self.assertEqual(0, created.returncode, created.stdout + created.stderr)
        else:
            redirected.symlink_to(external, target_is_directory=True)

        outcome = self._run_generated_launcher(project, source)

        self.assertEqual(3, outcome.returncode, outcome.stdout + outcome.stderr)
        self.assertFalse((project / "executed").exists())
        self.assertFalse((self.scratch / "executed").exists())

    def test_source_launchers_embed_the_exact_flat_launcher_identity(self) -> None:
        launcher_content = (REPOSITORY / "tools" / "managed_launcher.py").read_bytes()
        self.assertEqual(
            release._flat_unix_launcher(launcher_content),
            (REPOSITORY / "kit").read_bytes(),
        )
        self.assertEqual(
            release._flat_windows_launcher(launcher_content),
            (REPOSITORY / "kit.cmd").read_bytes().replace(b"\r\n", b"\n"),
        )

        copied = self.scratch / "source-launcher-copy"
        copied.mkdir()
        sentinel_source = (
            b"from pathlib import Path\n"
            b"Path(__file__).parent.parent.joinpath('executed').write_text('bad')\n"
        )
        _write(copied, "tools/managed_launcher.py", sentinel_source)
        environment = dict(os.environ)
        environment["KIT_PYTHON"] = sys.executable
        if os.name == "nt":
            stable = _write(copied, "kit.cmd", (REPOSITORY / "kit.cmd").read_bytes())
            command = [
                process_supervisor.windows_command_processor(),
                "/d", "/c", str(stable),
            ]
        else:
            stable = _write(copied, "kit", (REPOSITORY / "kit").read_bytes())
            stable.chmod(0o755)
            command = [str(stable)]
        outcome = subprocess.run(
            command,
            cwd=copied,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

        self.assertEqual(3, outcome.returncode, outcome.stdout + outcome.stderr)
        self.assertFalse((copied / "executed").exists())

        environment.pop("KIT_PYTHON", None)
        environment["PATH"] = str(copied) + os.pathsep + environment.get("PATH", "")
        if os.name == "nt":
            _write(
                copied,
                "py.bat",
                "@echo off\r\necho bad>path-python-ran\r\nexit /b 0\r\n",
            )
        else:
            fake_python = _write(
                copied,
                "python3",
                "#!/bin/sh\necho bad > path-python-ran\nexit 0\n",
            )
            fake_python.chmod(0o755)
        outcome = subprocess.run(
            command,
            cwd=copied,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        self.assertEqual(3, outcome.returncode, outcome.stdout + outcome.stderr)
        self.assertFalse((copied / "path-python-ran").exists())

    def test_source_launcher_refuses_a_dangling_managed_redirect(self) -> None:
        project = self.scratch / "dangling-source-launcher"
        project.mkdir()
        if os.name == "nt":
            stable = _write(project, "kit.cmd", (REPOSITORY / "kit.cmd").read_bytes())
        else:
            stable = _write(project, "kit", (REPOSITORY / "kit").read_bytes())
            stable.chmod(0o755)
        target = self.scratch / "removed-agent-kit-target"
        target.mkdir()
        redirected = project / ".agent-kit"
        if os.name == "nt":
            created = subprocess.run(
                [
                    process_supervisor.windows_command_processor(),
                    "/d", "/c", "mklink", "/J", str(redirected), str(target),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            self.assertEqual(0, created.returncode, created.stdout + created.stderr)
            target.rmdir()
            command = [
                process_supervisor.windows_command_processor(),
                "/d", "/c", str(stable),
            ]
        else:
            target.rmdir()
            redirected.symlink_to(target, target_is_directory=True)
            command = [str(stable)]
        environment = dict(os.environ)
        environment["KIT_PYTHON"] = sys.executable

        try:
            outcome = subprocess.run(
                command,
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        finally:
            if os.name == "nt":
                os.rmdir(redirected)
            elif redirected.is_symlink():
                redirected.unlink(missing_ok=True)

        self.assertEqual(3, outcome.returncode, outcome.stdout + outcome.stderr)
        self.assertIn("managed kit change is incomplete", outcome.stderr)

    def test_provider_bridges_keep_project_core_and_game_roots_distinct(self) -> None:
        archive = _synthetic_smoke_archive(self.scratch)
        contents = _member_contents(archive)
        agents = contents["install/agents.block.md"].decode("utf-8")
        copilot = contents["install/copilot.block.md"].decode("utf-8")

        for root_name in ("PROJECT_ROOT", "CORE_ROOT", "GAME_ROOT"):
            self.assertIn(f"`{root_name}`", agents)
            self.assertIn(f"`{root_name}`", copilot)
        self.assertIn("active_release.core_path", agents)
        self.assertIn("game_root` in `kit.config.json", agents)
        self.assertIn("@../AGENTS.md", copilot)
        for relative in (
            "AGENTS.md",
            "docs/DESIGN.md",
            "docs/GATE.md",
            "docs/GDSCRIPT.md",
            "docs/LIFECYCLE.md",
            "docs/RULES.md",
            "docs/SCENES.md",
            "docs/WORKFLOW.md",
        ):
            self.assertIn(relative, contents)

    def test_historic_0_2_archive_remains_verifiable_without_install_support(self) -> None:
        archive = _historic_0_2_archive(self.scratch)

        inspected = release.inspect_archive(archive)
        verified = release.verify_archive(archive)

        self.assertIsNone(inspected["install_manifest"])
        self.assertIsNone(verified["install_schema"])
        self.assertEqual(verified["version"], "0.2.0")

    def test_exact_historic_container_keeps_its_bound_identity(self) -> None:
        canonical = _historic_0_2_archive(self.scratch)
        inspected = release.inspect_archive(canonical)
        modes = {
            item["path"]: int(item["mode"], 8)
            for item in inspected["members"]
        }
        repacked = self.scratch / "historic-repacked.zip"
        with zipfile.ZipFile(repacked, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in sorted(_member_contents(canonical).items()):
                info = zipfile.ZipInfo(name, (2001, 2, 3, 4, 5, 6))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | modes[name]) << 16
                archive.writestr(info, content, compress_type=zipfile.ZIP_STORED)
        raw_digest = hashlib.sha256(repacked.read_bytes()).hexdigest()

        with mock.patch.object(
            release, "LEGACY_0_2_0_ARCHIVE_SHA256", raw_digest
        ):
            verified = release.verify_archive(repacked)

        self.assertEqual(raw_digest, verified["archive_sha256"])
        self.assertEqual(raw_digest, verified["container_sha256"])

    def test_generated_install_source_cannot_be_reauthorized_by_release_manifest(self) -> None:
        archive = _synthetic_smoke_archive(self.scratch)
        contents = _member_contents(archive)
        manifest = json.loads(contents[release.MANIFEST_PATH])
        changed = contents["install/agents.block.md"] + b"hidden change\n"
        entry = next(
            item for item in manifest["files"]
            if item["path"] == "install/agents.block.md"
        )
        entry["bytes"] = len(changed)
        entry["sha256"] = release.hashlib.sha256(changed).hexdigest()
        contents["install/agents.block.md"] = changed
        contents[release.MANIFEST_PATH] = release._canonical_json(manifest)
        members = {
            name: (
                content,
                0o755 if name.endswith(".py") or name == "kit" else 0o644,
            )
            for name, content in contents.items()
        }
        release._write_zip(archive, members)

        with self.assertRaisesRegex(release.ReleaseError,
                                    "managed install source is not canonical"):
            release.verify_archive(archive)

    def test_release_at_or_after_0_3_requires_complete_install_support(self) -> None:
        archive = _synthetic_smoke_archive(self.scratch)
        contents = _member_contents(archive)
        manifest = json.loads(contents[release.MANIFEST_PATH])
        manifest["files"] = [
            item for item in manifest["files"]
            if item["path"] not in release.GENERATED_INSTALL_FILES
        ]
        for path in release.GENERATED_INSTALL_FILES:
            contents.pop(path, None)
        contents[release.MANIFEST_PATH] = release._canonical_json(manifest)
        members = {
            name: (
                content,
                0o755 if name.endswith(".py") or name == "kit" else 0o644,
            )
            for name, content in contents.items()
        }
        release._write_zip(archive, members)

        with self.assertRaisesRegex(release.ReleaseError,
                                    "missing required kit files|missing managed install support"):
            release.verify_archive(archive)

    def test_pinned_legacy_contract_is_complete_and_excludes_project_state(self) -> None:
        manifest = release._install_manifest_document("0.3.0")

        self.assertEqual([], release._definition_errors(manifest))
        self.assertEqual(
            "b5a2f7dafd4a21db099d30f295caceea3a20d4303e9b885d1e0fe24873f5e4dc",
            release.LEGACY_0_2_0_ARCHIVE_SHA256,
        )
        self.assertEqual(30, len(release.LEGACY_0_2_0_SURFACE_SHA256))
        self.assertEqual(83, len(release.LEGACY_0_2_0_RETIRED_SHA256))
        retired = set(release.LEGACY_0_2_0_RETIRED_SHA256)
        self.assertFalse(retired & {
            "README.md", "LICENSE", "VERSION", ".gate.sha256",
            "kit.config.json", "ARCHITECTURE.md", "arch.rules.json",
        })
        self.assertFalse(any(path.startswith(("src/", "docs/retro/", "docs/design/"))
                             for path in retired))


class TestVerifiedDirectory(ReleaseTestCase):
    def _directory(self) -> tuple[Path, Path]:
        base = self.scratch / f"directory-{uuid.uuid4().hex}"
        base.mkdir()
        archive = _synthetic_smoke_archive(base)
        directory = base / "extracted"
        directory.mkdir()
        _extract_exact(archive, directory)
        return archive, directory

    def test_exact_directory_has_the_canonical_zip_identity(self) -> None:
        archive, directory = self._directory()

        report, members = release.read_verified_directory(directory)

        self.assertEqual("directory", report["format"])
        self.assertEqual(release.verify_archive(archive)["archive_sha256"],
                         report["archive_sha256"])
        self.assertIn(release.INSTALL_MANIFEST_PATH, members)

    def test_tampered_and_extra_directory_content_is_refused(self) -> None:
        _archive, directory = self._directory()
        (directory / "README.md").write_bytes(b"tampered\n")
        with self.assertRaisesRegex(release.ReleaseError, "size does not match|SHA-256"):
            release.read_verified_directory(directory)

        _archive, directory = self._directory()
        _write(directory, "extra.txt", "extra\n")
        with self.assertRaisesRegex(release.ReleaseError, "member set differs"):
            release.read_verified_directory(directory)

    def test_extra_or_linked_directory_is_refused(self) -> None:
        _archive, directory = self._directory()
        (directory / "extra-empty").mkdir()
        with self.assertRaisesRegex(release.ReleaseError, "directory set is not exact"):
            release.read_verified_directory(directory)

        _archive, directory = self._directory()
        linked = directory / "linked"
        linked.mkdir()
        original = release._is_reparse_point

        def marked(path: Path) -> bool:
            return path == linked or original(path)

        with mock.patch.object(release, "_is_reparse_point", side_effect=marked):
            with self.assertRaisesRegex(release.ReleaseError, "linked directory"):
                release.read_verified_directory(directory)

    def test_directory_materializes_to_the_same_deterministic_zip(self) -> None:
        archive, directory = self._directory()
        output = self.scratch / "materialized.zip"

        report = release.materialize_verified_directory_zip(directory, output)

        self.assertEqual(archive.read_bytes(), output.read_bytes())
        self.assertEqual(report["archive_sha256"],
                         release.verify_archive(archive)["archive_sha256"])

    def test_directory_materialization_refuses_a_dangling_output_redirect(self) -> None:
        _archive, directory = self._directory()
        target = self.scratch / "removed-output-target"
        target.mkdir()
        output = self.scratch / "materialized.zip"
        _make_directory_redirect(output, target)
        target.rmdir()
        try:
            with self.assertRaisesRegex(release.ReleaseError, "not a regular file"):
                release.materialize_verified_directory_zip(directory, output)
        finally:
            _remove_directory_redirect(output)


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
        source_validation_files = (
            release.VALIDATION_FILES | release.SOURCE_ONLY_VALIDATION_FILES
        )
        self.assertTrue(actual_tests.issubset(source_validation_files))
        self.assertEqual(
            actual_tests - set(release.VALIDATION_FILES),
            set(release.SOURCE_ONLY_VALIDATION_FILES),
        )
        self.assertEqual(actual_skills, set(release.REQUIRED_SKILL_FILES))
        self.assertEqual(actual_agents, set(release.REQUIRED_AGENT_FILES))

    def test_source_only_lifecycle_e2e_is_discoverable_but_not_released(self) -> None:
        relative = "tools/tests/test_lifecycle_e2e.py"

        self.assertIn(relative, release.SOURCE_ONLY_VALIDATION_FILES)
        self.assertIn(
            relative,
            {
                path.relative_to(REPOSITORY).as_posix()
                for path in (REPOSITORY / "tools" / "tests").glob("test_*.py")
            },
        )
        self.assertNotIn(relative, release.VALIDATION_FILES)
        self.assertNotIn(relative, release.REQUIRED_KIT_FILES)
        self.assertFalse(release.is_allowlisted(relative))

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

    def test_allowlisted_source_hardlink_is_refused(self) -> None:
        root, _commit = _fixture_repository(self.scratch)
        readme = root / "README.md"
        alias = root / "README-hardlink.md"
        try:
            os.link(readme, alias)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable on this host: {exc}")

        with self.assertRaisesRegex(release.ReleaseError, "hard link"):
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

    def test_hardlinked_archive_is_refused(self) -> None:
        archive = self._zip_with("README.md")
        alias = self.scratch / "archive-hardlink.zip"
        try:
            os.link(archive, alias)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable on this host: {exc}")

        with self.assertRaisesRegex(release.ReleaseError, "hard link"):
            release.inspect_archive(alias)

    def test_archive_growth_during_read_is_refused(self) -> None:
        archive = self._zip_with("README.md")
        original_fstat = release.os.fstat
        calls = 0

        def changed_fstat(descriptor: int):
            nonlocal calls
            calls += 1
            info = original_fstat(descriptor)
            if calls == 2:
                changed = mock.Mock(wraps=info)
                changed.st_size = info.st_size + 1
                return changed
            return info

        with mock.patch.object(release.os, "fstat", side_effect=changed_fstat):
            with self.assertRaisesRegex(release.ReleaseError, "changed while reading"):
                release.inspect_archive(archive)

    def test_archive_replacement_during_read_is_refused(self) -> None:
        archive = self._zip_with("README.md")
        path_type = type(archive)
        original_lstat = path_type.lstat
        calls = 0

        def replaced_lstat(path: Path):
            nonlocal calls
            info = original_lstat(path)
            if path == archive:
                calls += 1
                if calls == 2:
                    replaced = mock.Mock(wraps=info)
                    replaced.st_ino = info.st_ino + 1
                    return replaced
            return info

        with mock.patch.object(release, "_is_reparse_point", return_value=False):
            with mock.patch.object(path_type, "lstat", autospec=True, side_effect=replaced_lstat):
                with self.assertRaisesRegex(release.ReleaseError, "changed while reading"):
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
        sentinel = self.scratch / "hostile-command-processor.ran"
        hostile = self.scratch / "hostile-command-processor.cmd"
        hostile.write_text(
            "@echo off\r\n"
            f">\"{sentinel}\" echo executed\r\n"
            "exit /b 99\r\n",
            encoding="utf-8",
        )

        with mock.patch.dict(
            os.environ,
            {
                "COMSPEC": str(hostile),
                "PATH": f"{self.scratch}{os.pathsep}{os.environ.get('PATH', '')}",
            },
            clear=False,
        ):
            report = release.smoke_archive(archive, workspace)

        self.assertTrue(report["ok"])
        self.assertFalse(sentinel.exists())
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

    def test_smoke_refuses_a_redirected_workspace_parent(self) -> None:
        archive = _synthetic_smoke_archive(self.scratch)
        external = self.scratch / "external-smoke"
        external.mkdir()
        redirected = self.scratch / "redirected-smoke"
        _make_directory_redirect(redirected, external)
        try:
            with self.assertRaisesRegex(
                release.ReleaseError, "unredirected|redirected components"
            ):
                release.smoke_archive(archive, redirected / "workspace")
            self.assertEqual([], list(external.iterdir()))
        finally:
            _remove_directory_redirect(redirected)


class TestVerification(ReleaseTestCase):
    def test_release_manifest_rejects_duplicate_keys(self) -> None:
        member = release.ArchiveMember(
            release.MANIFEST_PATH,
            b'{"schema":3,"schema":3}\n',
            0o644,
        )
        with self.assertRaisesRegex(release.ReleaseError, "duplicate key 'schema'"):
            release._parse_manifest(member)

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
