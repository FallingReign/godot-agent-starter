#!/usr/bin/env python3
"""Offline post-apply proof for managed install and upgrade."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import brownfield  # noqa: E402
import kit_change_check as check  # noqa: E402
import release  # noqa: E402


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class OfflineKitChangeCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kit-change-check-")
        self.target = Path(self.temporary.name).resolve() / "project"
        self.target.mkdir()
        self.core = self.target / ".agent-kit" / "releases" / ("a" * 64)
        self.core.mkdir(parents=True)
        (self.core / "kit.py").write_text("# kit\n", encoding="utf-8")
        (self.core / "check.py").write_text("# check\n", encoding="utf-8")
        (self.core / "tools").mkdir()
        (self.core / "tools" / "managed_launcher.py").write_text(
            "# managed launcher\n", encoding="utf-8"
        )
        launcher_contents = {
            ".agent-kit/launcher.py": b"# launcher\n",
            "kit": b"#!/bin/sh\n",
            "kit.cmd": b"@echo off\r\n",
        }
        surfaces = []
        for index, (relative, content) in enumerate(launcher_contents.items()):
            path = self.target.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            surfaces.append({
                "id": f"launcher-{index}",
                "path": relative,
                "strategy": "replace",
                "base_sha256": "0" * 64,
                "applied_sha256": _sha256(content),
            })
        self.current = {
            "installation_id": "1" * 32,
            "active_release": {"archive_sha256": "a" * 64},
            "managed_surfaces": surfaces,
        }
        self.current_path = self.target / ".agent-kit" / "current.json"
        self.current_path.write_bytes(_canonical(self.current))
        self.runtime = self.target / ".kit" / "runtime"
        self.game = self.target / "game" / "player.gd"
        self.game.parent.mkdir(parents=True)
        self.game.write_text("extends Node\n", encoding="utf-8")
        self.issues: list[dict[str, object]] = []
        self.returncodes = {"self-test": 0, "scan": 0, "verify": 0}
        self.malformed_verify = False
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.installation = SimpleNamespace(
            mode="managed",
            project_root=self.target,
            core_root=self.core,
            release_sha256="a" * 64,
            current_path=self.current_path,
        )
        self.context = SimpleNamespace(
            install_mode="managed",
            project_root=self.target,
            core_root=self.core,
            runtime_root=self.runtime,
        )
        self.session = {
            "session_id": "b" * 64,
            "release": {"archive_sha256": "a" * 64},
            "request": {"mode": "install"},
            "baseline": {
                "prior": {"exists": False, "sha256": "", "content_base64": ""},
                "generated": None,
            },
        }
        self.patches = [
            mock.patch.object(
                check.managed_launcher,
                "resolve_installation",
                return_value=self.installation,
            ),
            mock.patch.object(
                check.project_context,
                "load_configured_context",
                return_value=self.context,
            ),
            mock.patch.object(
                check.managed_launcher,
                "bound_environment",
                side_effect=lambda _installation, base: dict(base),
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    @staticmethod
    def _process_outcome(returncode: int, stdout: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr="",
            termination_verified=True,
            timed_out=False,
            cancelled=False,
            launch_error=None,
        )

    def _runner(self, command: list[str], **kwargs: object) -> SimpleNamespace:
        self.commands.append(list(command))
        environment = kwargs.get("environment")
        if isinstance(environment, dict):
            self.environments.append(dict(environment))
        if "self-test" in command:
            code = self.returncodes["self-test"]
            return self._process_outcome(code, json.dumps({
                "command": "self-test", "ok": code == 0, "exit_code": code
            }))
        if "__brownfield-scan" in command:
            code = self.returncodes["scan"]
            if code == 0:
                output = Path(command[command.index("__brownfield-scan") + 1])
                output.write_bytes(brownfield.canonical_json(
                    brownfield.build_scan_document(self.issues)
                ))
            return self._process_outcome(code, "brownfield scan\n")
        if "verify" in command:
            code = self.returncodes["verify"]
            if self.malformed_verify:
                return self._process_outcome(code, "{}")
            return self._process_outcome(code, json.dumps({
                "command": "verify", "ok": code == 0, "exit_code": code
            }))
        raise AssertionError(f"unexpected command: {command}")

    def _set_upgrade_prior(self, issues: list[dict[str, object]]) -> None:
        previous_release = "d" * 64
        prior = brownfield.canonical_json(brownfield.build_baseline(
            self.target,
            issues,
            installation_id=self.current["installation_id"],
            release_sha256=previous_release,
        ))
        baseline_path = self.target / brownfield.BASELINE_RELATIVE
        baseline_path.write_bytes(prior)
        self.session["request"] = {"mode": "upgrade"}
        self.session["preview"] = {
            "raw": {
                "material": {
                    "current": {"active_archive_sha256": previous_release},
                },
            },
        }
        self.session["baseline"] = {
            "prior": {
                "exists": True,
                "sha256": _sha256(prior),
                "content_base64": base64.b64encode(prior).decode("ascii"),
            },
            "generated": None,
        }

    def _stored_baseline(self) -> dict[str, object]:
        content = brownfield.read_baseline_file(self.target)
        self.assertIsNotNone(content)
        return brownfield.validate_baseline(
            content or b"",
            installation_id=self.current["installation_id"],
            release_sha256=self.current["active_release"]["archive_sha256"],
        )

    def test_success_validates_and_writes_empty_baseline_without_touching_game(self) -> None:
        before = self.game.read_bytes()

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertTrue(result["project_ok"])
        self.assertEqual(before, self.game.read_bytes())
        baseline = brownfield.read_baseline_file(self.target)
        self.assertIsNotNone(baseline)
        self.assertEqual(_sha256(baseline or b""), result["baseline_sha256"])
        joined = " ".join(part for command in self.commands for part in command).lower()
        self.assertNotIn("godot", joined)
        self.assertNotIn("git ", joined)
        self.assertNotIn("http", joined)
        selected_launcher = "managed_launcher.py"
        self.assertTrue(
            all(selected_launcher in " ".join(command) for command in self.commands)
        )
        for environment in self.environments:
            self.assertEqual(str(Path(sys.executable).resolve()), environment["KIT_PYTHON"])
            for unsafe in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"):
                self.assertNotIn(unsafe, environment)

    def test_existing_issues_make_adoption_required_not_kit_failure(self) -> None:
        self.issues = [{
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }]

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertEqual(self.issues, result["existing_issues"])

    def test_upgrade_carries_only_an_unchanged_existing_gap(self) -> None:
        issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }
        self.issues = [issue]
        self._set_upgrade_prior([issue])

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        stored = self._stored_baseline()
        self.assertEqual([issue], [
            {key: item[key] for key in check._PUBLIC_ISSUE_FIELDS}
            for item in stored["issues"]
        ])
        self.assertIn("unchanged existing", str(result["detail"]))

    def test_upgrade_drops_a_resolved_gap(self) -> None:
        old_issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }
        self._set_upgrade_prior([old_issue])

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertTrue(result["project_ok"])
        self.assertEqual([], self._stored_baseline()["issues"])
        self.assertIn("1 old gap(s) resolved", str(result["detail"]))

    def test_upgrade_does_not_baseline_a_new_gap(self) -> None:
        new_issue = {
            "stage": "lint",
            "code": "new-warning",
            "path": "game/player.gd",
            "line": 2,
            "message_sha256": "e" * 64,
        }
        self.issues = [new_issue]
        self._set_upgrade_prior([])
        self.returncodes["verify"] = 1

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertEqual([], self._stored_baseline()["issues"])
        self.assertEqual([new_issue], result["existing_issues"])
        self.assertIn("static verification failed", str(result["detail"]))

    def test_upgrade_does_not_baseline_an_issue_after_its_file_changed(self) -> None:
        issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }
        self.issues = [issue]
        self._set_upgrade_prior([issue])
        self.game.write_text("extends Node\n# changed\n", encoding="utf-8")
        self.returncodes["verify"] = 1

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertEqual([], self._stored_baseline()["issues"])
        self.assertIn("static verification failed", str(result["detail"]))

    def test_upgrade_rechecks_a_file_changed_after_its_first_evaluation(self) -> None:
        issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }
        self.issues = [issue]
        self._set_upgrade_prior([issue])
        self.returncodes["verify"] = 1

        def mutate(name: str) -> None:
            if name == "after-upgrade-evaluation":
                self.game.write_text("extends Node\n# changed in race\n", encoding="utf-8")

        with mock.patch.object(check, "_failpoint", side_effect=mutate):
            result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertEqual([], self._stored_baseline()["issues"])
        self.assertIn("static verification failed", str(result["detail"]))

    def test_self_test_failure_stops_before_scan_and_baseline(self) -> None:
        self.returncodes["self-test"] = 1

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertEqual(1, len(self.commands))
        self.assertIsNone(brownfield.read_baseline_file(self.target))

    def test_scan_failure_stops_without_baseline(self) -> None:
        self.returncodes["scan"] = 2

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertEqual(2, len(self.commands))
        self.assertIsNone(brownfield.read_baseline_file(self.target))

    def test_clean_scan_plus_static_failure_is_a_kit_failure(self) -> None:
        self.returncodes["verify"] = 1

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertTrue(brownfield.read_baseline_file(self.target))
        self.assertIn("static verification failed", str(result["detail"]))

    def test_malformed_static_proof_is_kit_failure_with_generated_identity(self) -> None:
        self.malformed_verify = True

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        baseline = brownfield.read_baseline_file(self.target)
        self.assertIsNotNone(baseline)
        self.assertEqual(_sha256(baseline or b""), result["baseline_sha256"])

    def test_third_baseline_version_is_never_overwritten(self) -> None:
        baseline_path = self.target / brownfield.BASELINE_RELATIVE
        baseline_path.write_bytes(b"third version\n")
        before = baseline_path.read_bytes()

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertEqual(before, baseline_path.read_bytes())

    def test_recovery_accepts_the_exact_generated_baseline(self) -> None:
        first = check.run(self.target, self.session, runner=self._runner)
        self.commands.clear()

        second = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(first["kit_ok"])
        self.assertTrue(second["kit_ok"])
        self.assertEqual(first["baseline_sha256"], second["baseline_sha256"])

    def test_real_children_ignore_target_and_pythonpath_startup_files(self) -> None:
        launcher = self.target / ".agent-kit" / "launcher.py"
        launcher_bytes = b"""#!/usr/bin/env python3
import base64
import json
import sys
from pathlib import Path

base64.b64encode(b\"proof\")
command = sys.argv[1]
if command == \"__brownfield-scan\":
    Path(sys.argv[2]).write_bytes(
        b'{\"complete\":true,\"errors\":[],\"issues\":[],\"kind\":'
        b'\"agent-kit-brownfield-scan\",\"schema\":1}\\n'
    )
    raise SystemExit(0)
if command in {\"self-test\", \"verify\"}:
    print(json.dumps({\"command\": command, \"ok\": True, \"exit_code\": 0}))
    raise SystemExit(0)
raise SystemExit(4)
"""
        launcher.write_bytes(launcher_bytes)
        (self.core / "tools" / "managed_launcher.py").write_bytes(launcher_bytes)
        launchers = {
            ".agent-kit/launcher.py": launcher_bytes,
            "kit": release._managed_unix_launcher(launcher_bytes),
            "kit.cmd": release._managed_windows_launcher(launcher_bytes),
        }
        for relative, content in launchers.items():
            path = self.target.joinpath(*relative.split("/"))
            path.write_bytes(content)
            if relative == "kit":
                path.chmod(0o755)
        for surface in self.current["managed_surfaces"]:
            surface["applied_sha256"] = _sha256(launchers[surface["path"]])
        self.current_path.write_bytes(_canonical(self.current))

        poison = self.target.parent / "python-poison"
        poison.mkdir()
        sentinels: list[Path] = []
        for root, label in ((self.target, "target"), (poison, "pythonpath")):
            for module in ("sitecustomize.py", "base64.py"):
                sentinel = self.target.parent / f"{label}-{module}.ran"
                sentinels.append(sentinel)
                (root / module).write_text(
                    "from pathlib import Path\n"
                    f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
                    "raise RuntimeError('unsafe Python startup input executed')\n",
                    encoding="utf-8",
                )

        with mock.patch.dict(
            os.environ,
            {
                "COMSPEC": str(poison / "hostile-comspec.exe"),
                "PATH": f"{self.target}{os.pathsep}{poison}",
                "PYTHONPATH": f"{self.target}{os.pathsep}{poison}",
                "PYTHONSTARTUP": str(poison / "sitecustomize.py"),
                "PYTHONUSERBASE": str(poison),
            },
            clear=False,
        ):
            result = check.run(self.target, self.session)

        self.assertTrue(result["kit_ok"])
        self.assertTrue(result["project_ok"])
        self.assertTrue(all(not sentinel.exists() for sentinel in sentinels))

    def test_tampered_managed_launcher_is_not_executed(self) -> None:
        sentinel = self.target.parent / "tampered-launcher.ran"
        tampered = (
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        ).encode("utf-8")
        (self.target / ".agent-kit" / "launcher.py").write_bytes(tampered)

        result = check.run(self.target, self.session)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(sentinel.exists())
        self.assertIsNone(brownfield.read_baseline_file(self.target))

    def test_tampered_root_command_launcher_is_refused_before_core_runs(self) -> None:
        sentinel = self.target.parent / "core-launcher.ran"
        (self.core / "tools" / "managed_launcher.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )
        (self.target / "kit.cmd").write_bytes(b"@echo off\r\necho tampered\r\n")

        result = check.run(self.target, self.session)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(sentinel.exists())
        self.assertEqual([], self.commands)
        self.assertIsNone(brownfield.read_baseline_file(self.target))


if __name__ == "__main__":
    unittest.main()
