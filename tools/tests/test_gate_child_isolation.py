#!/usr/bin/env python3
"""Prove gate-owned children cannot resolve or import project tools."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import check as gate  # noqa: E402
from tools import process_supervisor  # noqa: E402


class GateChildIsolationTests(unittest.TestCase):
    def test_lifecycle_conformance_does_not_refresh_the_project_plan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-plan-lifecycle-") as raw:
            project = Path(raw).resolve()
            plan = project / "plan.html"
            plan.write_bytes(b"existing reviewed plan\n")

            def refresh_plan(
                _script: Path,
                _arguments: tuple[str, ...],
                _timeout: int,
                _log: Path,
            ) -> tuple[int, str]:
                plan.write_bytes(b"new projection\n")
                return 0, ""

            with mock.patch.object(
                gate, "PROJECT_ROOT", project
            ), mock.patch.object(
                gate, "_run_internal_python", side_effect=refresh_plan
            ) as run, mock.patch.dict(
                os.environ, {"KIT_LIFECYCLE_CHECK": "1"}
            ):
                gate.write_plan()

            run.assert_not_called()
            self.assertEqual(b"existing reviewed plan\n", plan.read_bytes())

            with mock.patch.object(
                gate, "PROJECT_ROOT", project
            ), mock.patch.object(
                gate, "_run_internal_python", side_effect=refresh_plan
            ) as run, mock.patch.dict(
                os.environ, {"KIT_LIFECYCLE_CHECK": ""}
            ):
                gate.write_plan()

            run.assert_called_once()
            self.assertEqual(b"new projection\n", plan.read_bytes())

    def test_lifecycle_design_check_does_not_write_project_projections(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-design-lifecycle-") as raw:
            project = Path(raw).resolve()
            design = project / "docs" / "design"
            design.mkdir(parents=True)
            section = design / "experience.md"
            section.write_bytes(b"# Existing player design\n")
            results = gate.Results()
            analysis = json.dumps(
                {
                    "sections": [{"declared_tunables": []}],
                    "bound": {},
                    "unbound": [],
                    "undeclared": [],
                    "const_bound": [],
                    "unstated_resolution": [],
                    "metadata_warnings": [],
                    "ready": [],
                }
            )

            def run_design(
                _tool: Path,
                arguments: tuple[str, ...],
                _timeout: int,
                _log: Path,
            ) -> tuple[int, str]:
                if "--check" in arguments:
                    return 0, analysis
                (design / "INDEX.md").write_bytes(b"generated\n")
                section.write_bytes(b"changed\n")
                return 0, ""

            with mock.patch.object(
                gate, "PROJECT_ROOT", project
            ), mock.patch.object(
                gate, "RESULTS", results
            ), mock.patch.object(
                gate, "_run_internal_python", side_effect=run_design
            ) as run, mock.patch.dict(
                os.environ, {"KIT_LIFECYCLE_CHECK": "1"}
            ):
                gate.stage_design()

            self.assertEqual(1, run.call_count)
            self.assertEqual(("--json", "--check"), run.call_args.args[1])
            self.assertEqual(b"# Existing player design\n", section.read_bytes())
            self.assertFalse((design / "INDEX.md").exists())
            self.assertIn("PASS  design", results.lines)

    def test_external_tool_environment_drops_project_python_controls(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PYTHONHOME": "project-home",
                "PYTHONPATH": "project-path",
                "PYTHONSTARTUP": "project-startup",
            },
        ):
            environment = gate._external_tool_environment()

        self.assertNotIn("PYTHONHOME", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PYTHONSTARTUP", environment)
        self.assertEqual("1", environment["PYTHONDONTWRITEBYTECODE"])
        if os.name == "nt":
            self.assertEqual("1", environment["NoDefaultCurrentDirectoryInExePath"])
            self.assertTrue(Path(environment["COMSPEC"]).is_absolute())

    def test_gate_git_child_drops_inherited_git_controls(self) -> None:
        runner = mock.Mock(return_value=(0, ""))
        with tempfile.TemporaryDirectory(prefix="gate-git-environment-") as raw, mock.patch.object(
            gate, "run", runner
        ), mock.patch.dict(
            os.environ,
            {
                "GIT_EXTERNAL_DIFF": "project-tool",
                "GIT_CONFIG_GLOBAL": "project-config",
                "GIT_PAGER": "project-pager",
            },
        ):
            gate._run_git(["trusted-git", "status"], 30, Path(raw) / "git.log")

        command = runner.call_args.args[0]
        self.assertEqual("trusted-git", command[0])
        self.assertIn("--no-pager", command)
        self.assertIn("core.fsmonitor=false", command)
        self.assertIn("diff.external=", command)
        self.assertIn("diff.trustExitCode=false", command)
        self.assertIn(f"core.attributesFile={os.devnull}", command)
        environment = runner.call_args.kwargs["env"]
        self.assertNotIn("GIT_EXTERNAL_DIFF", environment)
        self.assertNotIn("GIT_PAGER", environment)
        self.assertEqual(os.devnull, environment["GIT_CONFIG_GLOBAL"])
        self.assertEqual("1", environment["GIT_CONFIG_NOSYSTEM"])
        self.assertEqual("0", environment["GIT_OPTIONAL_LOCKS"])
        self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])

    def test_gate_git_child_disables_repository_fsmonitor_process(self) -> None:
        git = shutil.which("git")
        if not git:
            runner = mock.Mock(return_value=(0, ""))
            with tempfile.TemporaryDirectory(prefix="gate-git-fallback-") as raw, \
                    mock.patch.object(gate, "run", runner):
                gate._run_git(
                    ["trusted-git", "status"],
                    30,
                    Path(raw) / "git.log",
                )
            self.assertIn("core.fsmonitor=false", runner.call_args.args[0])
            return
        with tempfile.TemporaryDirectory(prefix="gate-git-fsmonitor-") as raw:
            repository = Path(raw).resolve()
            sentinel = repository / "fsmonitor-ran.txt"
            monitor = f'echo fsmonitor-ran > "{sentinel.as_posix()}"'

            def git_run(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [git, "-C", str(repository), *arguments],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )

            self.assertEqual(0, git_run("init").returncode)
            tracked = repository / "tracked.txt"
            tracked.write_text("tracked\n", encoding="utf-8")
            self.assertEqual(0, git_run("add", "tracked.txt").returncode)
            self.assertEqual(
                0,
                git_run(
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-m",
                    "fixture",
                ).returncode,
            )
            self.assertEqual(
                0,
                git_run("config", "core.fsmonitor", monitor).returncode,
            )

            unsafe = git_run("diff", "--no-ext-diff", "--name-only", "HEAD", "--", ".")
            self.assertEqual(0, unsafe.returncode, unsafe.stderr)
            self.assertTrue(sentinel.is_file(), "fixture did not exercise fsmonitor")
            sentinel.unlink()

            code, output = gate._run_git(
                [
                    git,
                    "-C",
                    str(repository),
                    "diff",
                    "--no-ext-diff",
                    "--name-only",
                    "HEAD",
                    "--",
                    ".",
                ],
                30,
                repository / "safe-git.log",
            )
            self.assertEqual(0, code, output)
            self.assertFalse(sentinel.exists())

            code, output = gate._run_git(
                [
                    git,
                    "-C",
                    str(repository),
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "--",
                    ".",
                ],
                30,
                repository / "safe-ls-files.log",
            )
            self.assertEqual(0, code, output)
            self.assertFalse(sentinel.exists())

    def test_gate_git_child_uses_bounded_process_supervision(self) -> None:
        supervised = process_supervisor.SupervisedResult(
            0,
            "bounded git output\n",
            "",
            0.01,
        )
        with tempfile.TemporaryDirectory(prefix="gate-git-bounded-") as raw, mock.patch.object(
            process_supervisor,
            "run_supervised",
            return_value=supervised,
        ) as runner:
            log = Path(raw) / "git.log"
            code, output = gate._run_git(["trusted-git", "status"], 30, log)

        self.assertEqual(0, code)
        self.assertEqual("bounded git output\n", output)
        self.assertEqual(gate.LOG_CAP_BYTES, runner.call_args.kwargs["output_cap_bytes"])
        self.assertEqual(gate.PROJECT_DIR, runner.call_args.kwargs["cwd"])
        self.assertIn("--no-pager", runner.call_args.args[0])

    def test_gate_tool_resolution_never_selects_the_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-tool-boundary-") as raw:
            scratch = Path(raw).resolve()
            project = scratch / "project"
            game = project / "src"
            core = scratch / "core"
            tools = project / "bin"
            for directory in (game, core, tools):
                directory.mkdir(parents=True, exist_ok=True)
            suffix = ".exe" if os.name == "nt" else ""
            hostile = tools / f"git{suffix}"
            hostile.write_bytes(b"project-local executable must not run")
            hostile.chmod(0o755)

            with mock.patch.object(gate, "PROJECT_ROOT", project), mock.patch.object(
                gate, "PROJECT_DIR", game
            ), mock.patch.object(gate, "CORE_ROOT", core), mock.patch.dict(
                os.environ,
                {"PATH": str(tools)},
            ), self.assertRaises(FileNotFoundError):
                gate._ordinary_executable("git")

    def test_lifecycle_check_skips_project_private_before_external_lookup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-private-tool-") as raw:
            project = Path(raw).resolve()
            config = project / "kit.config.json"
            config.write_text(
                '{"schema":1,"runtime_root":".kit/runtime"}\n',
                encoding="utf-8",
            )
            suffix = ".exe" if os.name == "nt" else ""
            private = (
                project
                / ".kit"
                / "runtime"
                / "tooling"
                / "gdtoolkit-4.5.0"
                / ("Scripts" if os.name == "nt" else "bin")
                / f"gdlint{suffix}"
            )
            private.parent.mkdir(parents=True)
            private.write_bytes(b"must not execute")
            private.chmod(0o755)

            unexpected = AssertionError("lifecycle check probed an executable")
            with mock.patch.object(gate, "ROOT", project), mock.patch.object(
                gate, "PROJECT_ROOT", project
            ), mock.patch.object(gate, "PROJECT_DIR", project), mock.patch.object(
                gate, "KIT_CONFIG_FILE", config
            ), mock.patch.object(gate, "GDTOOLKIT_VERSION", "4.5.0"), mock.patch.object(
                gate,
                "_ordinary_executable",
                side_effect=FileNotFoundError("no external tool"),
            ) as ordinary, mock.patch.object(
                gate.importlib.metadata,
                "version",
                side_effect=gate.importlib.metadata.PackageNotFoundError,
            ), mock.patch.object(
                gate.subprocess, "run", side_effect=unexpected
            ), mock.patch.dict(
                os.environ,
                {"KIT_LIFECYCLE_CHECK": "1"},
            ):
                self.assertIsNone(gate.gdtool("gdlint"))
            self.assertEqual(
                [mock.call("gdlint"), mock.call("uvx"), mock.call("uv")],
                ordinary.call_args_list,
            )

    def test_lifecycle_check_can_use_a_trusted_external_gdtool(self) -> None:
        probe = subprocess.CompletedProcess(
            ["trusted-gdlint", "--version"],
            0,
            stdout="gdlint 4.5.0\n",
            stderr="",
        )
        with mock.patch.object(
            gate, "_ordinary_executable", return_value="trusted-gdlint"
        ), mock.patch.object(gate.subprocess, "run", return_value=probe) as run, mock.patch.dict(
            os.environ,
            {"KIT_LIFECYCLE_CHECK": "1"},
        ):
            self.assertEqual(["trusted-gdlint"], gate.gdtool("gdlint"))

        environment = run.call_args.kwargs["env"]
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PYTHONHOME", environment)

    def test_lifecycle_scan_reports_unavailable_style_tools_without_a_baseline_issue(
        self,
    ) -> None:
        results = gate.Results()
        gate.BROWNFIELD_CAPTURE_ERRORS.clear()
        with mock.patch.object(gate, "RESULTS", results), mock.patch.object(
            gate, "BROWNFIELD_CAPTURE", True
        ), mock.patch.object(gate, "gdtool", return_value=None), mock.patch.dict(
            os.environ,
            {"KIT_LIFECYCLE_CHECK": "1"},
        ):
            gate.stage_format()
            gate.stage_lint()

        self.assertEqual([], gate.BROWNFIELD_CAPTURE_ERRORS)
        self.assertEqual([], gate.BROWNFIELD_CAPTURE_ISSUES)
        self.assertIn("SKIP  format", results.lines)
        self.assertIn("SKIP  lint", results.lines)

    def test_hostile_cwd_and_pythonpath_cannot_replace_python_or_kit_modules(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-child-isolation-") as raw:
            scratch = Path(raw).resolve()
            core = scratch / "core"
            tools = core / "tools"
            hostile = scratch / "hostile-project"
            tools.mkdir(parents=True)
            hostile.mkdir()

            result_path = scratch / "result.json"
            sentinel_path = scratch / "hostile-imported.txt"
            log_path = scratch / "child.log"

            (tools / "trusted_sibling.py").write_text(
                "VALUE = 'trusted-sibling'\n",
                encoding="utf-8",
            )
            child = tools / "child.py"
            child.write_text(
                """import base64
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
import trusted_sibling

result = {
    "base64": str(Path(base64.__file__).resolve()),
    "hashlib": str(Path(hashlib.__file__).resolve()),
    "unittest": str(Path(unittest.__file__).resolve()),
    "sibling": trusted_sibling.VALUE,
    "sibling_file": str(Path(trusted_sibling.__file__).resolve()),
    "sys_path": list(sys.path),
    "isolated": sys.flags.isolated,
    "no_site": sys.flags.no_site,
    "dont_write_bytecode": sys.flags.dont_write_bytecode,
    "pythonpath": os.environ.get("PYTHONPATH"),
}
Path(os.environ["GATE_CHILD_RESULT"]).write_text(
    json.dumps(result), encoding="utf-8"
)
""",
                encoding="utf-8",
            )
            hostile_module = """import os
with open(os.environ["GATE_CHILD_SENTINEL"], "a", encoding="utf-8") as stream:
    stream.write(__name__ + "\\n")
raise RuntimeError("hostile project module loaded")
"""
            for name in (
                "base64.py",
                "hashlib.py",
                "sitecustomize.py",
                "unittest.py",
            ):
                (hostile / name).write_text(hostile_module, encoding="utf-8")

            with mock.patch.object(gate, "CORE_ROOT", core), mock.patch.object(
                gate,
                "PROJECT_DIR",
                hostile,
            ), mock.patch.dict(
                os.environ,
                {
                    "PYTHONPATH": str(hostile),
                    "GATE_CHILD_RESULT": str(result_path),
                    "GATE_CHILD_SENTINEL": str(sentinel_path),
                },
            ):
                code, output = gate._run_internal_python(
                    child,
                    (),
                    30,
                    log_path,
                )

            self.assertEqual(0, code, output)
            self.assertFalse(sentinel_path.exists())
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual("trusted-sibling", result["sibling"])
            self.assertEqual(
                (tools / "trusted_sibling.py").resolve(),
                Path(result["sibling_file"]),
            )
            self.assertEqual(str(tools.resolve()), result["sys_path"][0])
            self.assertNotIn(str(hostile), result["sys_path"])
            self.assertEqual(1, result["isolated"])
            self.assertEqual(1, result["no_site"])
            self.assertEqual(1, result["dont_write_bytecode"])
            self.assertIsNone(result["pythonpath"])
            for module in ("base64", "hashlib", "unittest"):
                self.assertFalse(
                    Path(result[module]).is_relative_to(hostile),
                    f"{module} loaded from the project",
                )
            self.assertFalse(any(scratch.rglob("__pycache__")))

    def test_script_outside_the_selected_core_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-child-boundary-") as raw:
            scratch = Path(raw).resolve()
            core = scratch / "core"
            (core / "tools").mkdir(parents=True)
            outside = scratch / "outside.py"
            outside.write_text("raise SystemExit(0)\n", encoding="utf-8")
            log = scratch / "refused.log"

            with mock.patch.object(gate, "CORE_ROOT", core):
                code, output = gate._run_internal_python(outside, (), 30, log)

            self.assertEqual(127, code)
            self.assertIn("internal kit script is unsafe", output)
            self.assertEqual(output, log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
