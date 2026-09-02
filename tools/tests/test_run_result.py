#!/usr/bin/env python3
"""Tests for truthful worker completion semantics."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
REPOSITORY = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import run_result  # noqa: E402
import process_supervisor  # noqa: E402


def _git(root: Path, *args: str) -> str:
    executable = process_supervisor.resolve_ordinary_executable(
        "git", excluded_roots=(root, REPOSITORY)
    )
    result = subprocess.run([executable, "-C", str(root), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _scratch() -> Path:
    parent = REPOSITORY / ".checklogs"
    parent.mkdir(exist_ok=True)
    path = parent / f"run-result-test-{uuid.uuid4().hex}"
    path.mkdir()
    return path


def _remove_scratch(path: Path) -> None:
    resolved = path.resolve()
    parent = (REPOSITORY / ".checklogs").resolve()
    if resolved.parent != parent or not resolved.name.startswith("run-result-test-"):
        raise AssertionError(f"refusing to remove unexpected scratch path: {resolved}")

    def remove_readonly(function, target, _error) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(resolved, onerror=remove_readonly)


class CurrentDispatchPolicyTest(unittest.TestCase):
    """Exercise the shipped policy without creating a nested Git repository."""

    def setUp(self) -> None:
        config = json.loads(
            (REPOSITORY / "kit.config.json").read_text(encoding="utf-8")
        )
        dispatch = config["dispatch_policy"]
        self.policy = run_result.project_context.PathPolicy.create(
            REPOSITORY,
            dispatch["owned"],
            dispatch["forbidden"],
        )
        self.policy_patch = mock.patch.object(
            run_result,
            "_dispatch_policy",
            return_value=(self.policy, []),
        )
        self.policy_patch.start()

    def tearDown(self) -> None:
        self.policy_patch.stop()

    def test_ordinary_kit_documentation_is_automatically_dispatchable(self) -> None:
        self.assertEqual(
            [],
            run_result.dispatch_scope_blockers(
                REPOSITORY, "immutable-baseline", ["docs/GATE.md"]
            ),
        )

    def test_dispatch_control_plane_requires_an_interactive_maintainer(self) -> None:
        for path in ("tools/board.py", "tools/md.py"):
            with self.subTest(path=path):
                blockers = run_result.dispatch_scope_blockers(
                    REPOSITORY, "immutable-baseline", [path]
                )
                self.assertTrue(any(
                    "interactive maintainer" in blocker
                    and f"{path}: forbidden" in blocker
                    for blocker in blockers
                ))

    def test_traversal_in_declared_scope_fails_closed(self) -> None:
        blockers = run_result.dispatch_scope_blockers(
            REPOSITORY, "immutable-baseline", ["../outside.txt"]
        )

        self.assertTrue(any("requested scope is invalid" in blocker
                            for blocker in blockers))


class RunResultTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            process_supervisor.resolve_ordinary_executable(
                "git", excluded_roots=(REPOSITORY,)
            )
        except (FileNotFoundError, ValueError):
            self.skipTest("git is not installed")
        self.root = _scratch()
        _git(self.root, "init")
        _git(self.root, "config", "user.email", "test@example.invalid")
        _git(self.root, "config", "user.name", "Result Test")
        (self.root / "kit.config.json").write_text(json.dumps({
            "schema": 1,
            "game_root": "src",
            "runtime_root": ".kit/runtime",
            "dispatch_policy": {
                "owned": ["check.py", "kit.txt", "tools"],
                "forbidden": [
                    "src", "docs/retro", ".gate.sha256", "tools/control.py"
                ],
            },
        }), encoding="utf-8")
        (self.root / "check.py").write_text("print('GATE PASSED')\n", encoding="utf-8")
        (self.root / "tools").mkdir()
        (self.root / "tools" / ".keep").write_text("fixture\n", encoding="utf-8")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".kit/\n", encoding="utf-8")
        _git(
            self.root,
            "add",
            ".gitignore",
            "check.py",
            "base.txt",
            "kit.config.json",
            "tools/.keep",
        )
        _git(self.root, "commit", "-m", "baseline")
        self.before = _git(self.root, "rev-parse", "HEAD")
        self.path = self.root / "result.json"

    def tearDown(self) -> None:
        _remove_scratch(self.root)

    def write(self, **overrides) -> None:
        value = {"schema": 1, "run_id": "run-1", "outcome": "implemented",
                 "summary": "implemented the repair", "before_sha": self.before,
                 "after_sha": _git(self.root, "rev-parse", "HEAD"),
                 "changed_files": ["kit.txt"],
                 "verification": {"command": "python check.py", "passed": True}}
        value.update(overrides)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_exit_without_artifact_is_unverified(self) -> None:
        result = run_result.evaluate(self.path, "run-1", self.before, self.root)
        self.assertEqual("unverified", result["status"])
        self.assertIn("without a result", result["errors"][0])

    def test_blocked_is_distinct_from_failure_or_completion(self) -> None:
        self.write(outcome="blocked", summary="write permission denied")
        result = run_result.evaluate(self.path, "run-1", self.before, self.root)
        self.assertEqual("blocked", result["status"])

    def test_implemented_requires_matching_commit_files_and_fresh_gate(self) -> None:
        (self.root / "kit.txt").write_text("fixed\n", encoding="utf-8")
        _git(self.root, "add", "kit.txt")
        _git(self.root, "commit", "-m", "kit fix")
        after = _git(self.root, "rev-parse", "HEAD")
        self.write(after_sha=after)
        result = run_result.evaluate(self.path, "run-1", self.before, self.root)
        self.assertEqual("completed", result["status"], result["errors"])
        self.assertTrue(result["gate"]["passed"])

    def test_host_finalizer_commits_only_the_reviewed_edit_then_runs_fresh_gate(self) -> None:
        workspace = run_result.prepare_workspace(
            self.root, "run-host", self.before
        )
        (workspace / "kit.txt").write_text("host finalized\n", encoding="utf-8")
        result_path = run_result.workspace_result_path(workspace, "run-host")

        payload = run_result.finalize_workspace(
            result_path,
            "run-host",
            self.before,
            workspace,
            ["kit.txt"],
            "implemented the reviewed documentation repair",
        )
        evaluated = run_result.evaluate(
            result_path,
            "run-host",
            self.before,
            workspace,
            requested_files=["kit.txt"],
            trusted_host_result=True,
        )

        self.assertEqual("implemented", payload["outcome"])
        self.assertEqual("completed", evaluated["status"], evaluated["errors"])
        self.assertTrue(evaluated["gate"]["passed"])
        self.assertEqual("kit verify --static", evaluated["gate"]["command"])
        self.assertEqual("kit verify --static", payload["verification"]["command"])

    def test_host_finalizer_rejects_edits_outside_reviewed_scope_without_commit(self) -> None:
        workspace = run_result.prepare_workspace(
            self.root, "run-scope", self.before
        )
        (workspace / "base.txt").write_text("outside scope\n", encoding="utf-8")
        result_path = run_result.workspace_result_path(workspace, "run-scope")

        payload = run_result.finalize_workspace(
            result_path,
            "run-scope",
            self.before,
            workspace,
            ["kit.txt"],
            "should not commit",
        )

        self.assertEqual("failed", payload["outcome"])
        self.assertIn("outside the reviewed finding scope", payload["summary"])
        self.assertEqual(self.before, run_result.git_head(workspace))

    def test_host_finalizer_rejects_provider_created_git_metadata(self) -> None:
        workspace = run_result.prepare_workspace(
            self.root, "run-metadata", self.before
        )
        (workspace / ".git").mkdir()
        (workspace / ".git" / "config").write_text(
            "[core]\n\tfsmonitor = malicious-command\n", encoding="utf-8"
        )
        (workspace / "kit.txt").write_text("host finalized\n", encoding="utf-8")
        result_path = run_result.workspace_result_path(workspace, "run-metadata")

        payload = run_result.finalize_workspace(
            result_path,
            "run-metadata",
            self.before,
            workspace,
            ["kit.txt"],
            "must not run Git",
        )

        self.assertEqual("failed", payload["outcome"])
        self.assertIn("reserved .git path", payload["summary"])
        self.assertEqual(self.before, run_result.git_head(self.root))
        self.assertTrue((workspace.parent / ".run-metadata.git").is_dir())

    def test_implemented_cannot_claim_uncommitted_or_unrelated_changes(self) -> None:
        self.write()
        result = run_result.evaluate(self.path, "run-1", self.before, self.root,
                                     run_gate=False)
        self.assertEqual("unverified", result["status"])
        self.assertTrue(any("no commit" in error or "changed_files" in error
                            for error in result["errors"]))

    def test_implemented_rejects_game_source_paths(self) -> None:
        self.write(changed_files=["src/game.gd"])
        result = run_result.evaluate(self.path, "run-1", self.before, self.root,
                                     run_gate=False)
        self.assertEqual("unverified", result["status"])
        self.assertTrue(any("ownership violation" in error and "forbidden" in error
                            for error in result["errors"]))

    def test_implemented_rejects_unowned_kit_paths(self) -> None:
        self.write(changed_files=["base.txt"])
        result = run_result.evaluate(self.path, "run-1", self.before, self.root,
                                     run_gate=False)
        self.assertEqual("unverified", result["status"])
        self.assertTrue(any("ownership violation" in error and "unowned" in error
                            for error in result["errors"]))

    def test_declared_scope_is_checked_before_automatic_dispatch(self) -> None:
        allowed = run_result.dispatch_scope_blockers(
            self.root, self.before, ["tools/feature.py"]
        )
        protected = run_result.dispatch_scope_blockers(
            self.root, self.before, ["tools/control.py"]
        )

        self.assertEqual([], allowed)
        self.assertTrue(any(
            "interactive maintainer" in error and "tools/control.py: forbidden" in error
            for error in protected
        ))

    def test_runtime_root_is_derived_as_forbidden_from_immutable_config(self) -> None:
        config = json.loads((self.root / "kit.config.json").read_text(encoding="utf-8"))
        config["dispatch_policy"]["owned"].append(".kit/private-runtime")
        config["dispatch_policy"]["forbidden"] = []
        config["runtime_root"] = ".kit/private-runtime"
        (self.root / "kit.config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        _git(self.root, "add", "kit.config.json")
        _git(self.root, "commit", "-m", "runtime policy fixture")
        baseline = _git(self.root, "rev-parse", "HEAD")

        blockers = run_result.dispatch_scope_blockers(
            self.root, baseline, [".kit/private-runtime/secret.json"]
        )

        self.assertTrue(any(
            ".kit/private-runtime/secret.json: forbidden" in error
            for error in blockers
        ))

    def test_protected_declared_scope_blocks_a_clean_dispatch(self) -> None:
        blockers = run_result.dispatch_blockers(
            self.root, requested_files=["tools/control.py"]
        )

        self.assertTrue(any("interactive maintainer" in error for error in blockers))

    def test_workspace_creation_rechecks_declared_scope_before_clone(self) -> None:
        with self.assertRaisesRegex(
            run_result.DispatchWorkspaceError, "interactive maintainer"
        ):
            run_result.prepare_workspace(
                self.root,
                "run-protected",
                self.before,
                requested_files=["tools/control.py"],
            )
        self.assertFalse(
            (self.root / ".kit" / "runtime" / "dispatch" / "workspaces"
             / "run-protected").exists()
        )

    def test_missing_dispatch_policy_fails_closed(self) -> None:
        (self.root / "kit.config.json").write_text(
            json.dumps({"schema": 1, "runtime_root": ".kit/runtime"}),
            encoding="utf-8",
        )
        _git(self.root, "add", "kit.config.json")
        _git(self.root, "commit", "-m", "invalid policy baseline")
        invalid_before = _git(self.root, "rev-parse", "HEAD")
        (self.root / "kit.txt").write_text("fixed\n", encoding="utf-8")
        _git(self.root, "add", "kit.txt")
        _git(self.root, "commit", "-m", "kit fix")
        self.write(before_sha=invalid_before)
        result = run_result.evaluate(self.path, "run-1", invalid_before, self.root,
                                     run_gate=False)
        self.assertEqual("unverified", result["status"])
        self.assertTrue(any("policy is invalid" in error for error in result["errors"]))

    def test_worker_cannot_widen_the_policy_used_to_verify_its_own_run(self) -> None:
        widened = {
            "schema": 1,
            "runtime_root": ".kit/runtime",
            "dispatch_policy": {"owned": ["src", "kit.config.json"], "forbidden": []},
        }
        (self.root / "kit.config.json").write_text(json.dumps(widened), encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "game.gd").write_text("extends Node\n", encoding="utf-8")
        _git(self.root, "add", "kit.config.json", "src/game.gd")
        _git(self.root, "commit", "-m", "attempt policy widening")
        after = _git(self.root, "rev-parse", "HEAD")
        self.write(
            after_sha=after,
            changed_files=["kit.config.json", "src/game.gd"],
        )
        result = run_result.evaluate(self.path, "run-1", self.before, self.root,
                                     run_gate=False)
        self.assertEqual("unverified", result["status"])
        self.assertTrue(any("src/game.gd: forbidden" in error
                            for error in result["errors"]))

    def test_dispatch_workspace_is_an_independent_originless_clone(self) -> None:
        workspace = run_result.prepare_workspace(self.root, "run-isolated", self.before)

        self.assertTrue(workspace.is_relative_to(self.root / ".kit" / "runtime"))
        self.assertFalse((workspace / ".git").exists())
        self.assertTrue((workspace.parent / ".run-isolated.git").is_dir())
        run_result._restore_workspace_metadata(workspace, "run-isolated")
        self.assertEqual(self.before, run_result.git_head(workspace))
        self.assertEqual("", _git(workspace, "remote"))
        self.assertTrue((workspace / "base.txt").is_file())

    def test_verified_workspace_fast_forwards_without_sharing_git_state(self) -> None:
        workspace = run_result.prepare_workspace(self.root, "run-integrate", self.before)
        (workspace / "kit.txt").write_text("isolated fix\n", encoding="utf-8")
        result_path = run_result.workspace_result_path(workspace, "run-integrate")
        payload = run_result.finalize_workspace(
            result_path, "run-integrate", self.before, workspace,
            ["kit.txt"], "isolated kit fix",
        )
        after = run_result.git_head(workspace)

        integrated = run_result.integrate_workspace(
            self.root, workspace, self.before, after
        )

        self.assertEqual("implemented", payload["outcome"])
        self.assertTrue(integrated["integrated"], integrated["errors"])
        self.assertEqual(after, run_result.git_head(self.root))
        self.assertEqual("isolated fix\n", (self.root / "kit.txt").read_text())

    def test_integration_refuses_unrelated_local_work(self) -> None:
        workspace = run_result.prepare_workspace(self.root, "run-refuse", self.before)
        (workspace / "kit.txt").write_text("isolated fix\n", encoding="utf-8")
        result_path = run_result.workspace_result_path(workspace, "run-refuse")
        payload = run_result.finalize_workspace(
            result_path, "run-refuse", self.before, workspace,
            ["kit.txt"], "isolated kit fix",
        )
        after = run_result.git_head(workspace)
        (self.root / "base.txt").write_text("developer work\n", encoding="utf-8")

        integrated = run_result.integrate_workspace(
            self.root, workspace, self.before, after
        )

        self.assertEqual("implemented", payload["outcome"])
        self.assertFalse(integrated["integrated"])
        self.assertTrue(any("base.txt" in item for item in integrated["errors"]))
        self.assertEqual(self.before, run_result.git_head(self.root))


if __name__ == "__main__":
    unittest.main()
