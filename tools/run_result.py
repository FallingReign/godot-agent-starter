#!/usr/bin/env python3
"""Validate and integrate dispatched edits against repository evidence.

Process exit is not task completion. Automatic providers are edit-only: the
trusted host validates their working-tree scope, creates the commit and result
artifact, then accepts implementation only when the commit, changed files and a
fresh gate run all agree. Legacy externally produced result artifacts remain
verifiable, but never become trusted merely because the process exited zero.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_context  # noqa: E402
import managed_launcher  # noqa: E402
import runtime_paths  # noqa: E402
import process_supervisor  # noqa: E402

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
_ACTIVE_INSTALLATION = project_context.resolve_active_installation(CORE_ROOT)
PROJECT_ROOT = _ACTIVE_INSTALLATION.project_root
CHILD_ENVIRONMENT = managed_launcher.bound_environment(_ACTIVE_INSTALLATION)

SCHEMA = 1
OUTCOMES = ("implemented", "blocked", "failed")
RUN_ID = re.compile(r"(?:run|retro)-[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _core_for_project(root: Path) -> Path:
    canonical = root.resolve(strict=True)
    return CORE_ROOT if canonical == PROJECT_ROOT else canonical


class DispatchWorkspaceError(RuntimeError):
    """An isolated worker workspace cannot be created or integrated safely."""


def _git_executable(root: Path) -> str:
    try:
        return process_supervisor.resolve_ordinary_executable(
            "git", excluded_roots=(root, CORE_ROOT)
        )
    except (FileNotFoundError, ValueError) as exc:
        raise DispatchWorkspaceError("trusted Git executable is unavailable") from exc


def _git_result(root: Path, *arguments: str, timeout: int = 120,
                environment: dict[str, str] | None = None
                ) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [_git_executable(root), "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DispatchWorkspaceError(f"Git could not run: {exc}") from exc


def _path_present(path: Path) -> bool:
    """Return whether a path entry exists without following a symlink."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DispatchWorkspaceError(f"workspace path could not be inspected: {exc}") from exc
    return True


def _metadata_vault(workspace: Path, run_id: str) -> Path:
    """Return the trusted Git-metadata location outside a provider workspace."""
    if RUN_ID.fullmatch(run_id or "") is None:
        raise DispatchWorkspaceError("run id is not safe for workspace metadata")
    workspace = workspace.resolve(strict=True)
    if workspace.name != run_id:
        raise DispatchWorkspaceError("dispatch workspace does not match its reserved run id")
    parent = workspace.parent.resolve(strict=True)
    vault = (parent / f".{run_id}.git").resolve(strict=False)
    if vault.parent != parent:
        raise DispatchWorkspaceError("workspace metadata escapes its private parent")
    return vault


def _detach_workspace_metadata(workspace: Path, run_id: str) -> None:
    """Move Git control data outside the directory an automatic worker sees."""
    metadata = workspace / ".git"
    vault = _metadata_vault(workspace, run_id)
    if not metadata.is_dir():
        raise DispatchWorkspaceError("isolated clone has no trusted Git metadata")
    if _path_present(vault):
        raise DispatchWorkspaceError("trusted workspace metadata already exists")
    try:
        os.replace(metadata, vault)
    except OSError as exc:
        raise DispatchWorkspaceError(
            f"isolated Git metadata could not be secured: {exc}"
        ) from exc
    if _path_present(metadata) or not vault.is_dir():
        raise DispatchWorkspaceError("isolated Git metadata was not secured")


def _restore_workspace_metadata(workspace: Path, run_id: str) -> None:
    """Restore trusted Git control data after the provider process has exited."""
    metadata = workspace / ".git"
    vault = _metadata_vault(workspace, run_id)
    if _path_present(metadata):
        raise DispatchWorkspaceError(
            "provider created the reserved .git path; workspace is not trusted"
        )
    if not vault.is_dir():
        raise DispatchWorkspaceError("trusted workspace Git metadata is unavailable")
    try:
        os.replace(vault, metadata)
    except OSError as exc:
        raise DispatchWorkspaceError(
            f"trusted workspace Git metadata could not be restored: {exc}"
        ) from exc
    if not metadata.is_dir() or _path_present(vault):
        raise DispatchWorkspaceError("trusted workspace Git metadata was not restored")


def dispatch_blockers(root: Path, expected_head: str = "",
                      allowed_changes: frozenset[str] = frozenset(),
                      requested_files: object = None) -> list[str]:
    """Explain why an automatic worker must not start against ``root``.

    Dispatch is based on one immutable commit and later fast-forwards that
    exact result.  Starting from a dirty tree, nested repository, or moving
    baseline would either hide developer work from the worker or overwrite it
    during integration, so all three conditions fail closed before approval.
    """
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        return [f"repository root is unavailable: {exc}"]
    top = _git_result(root, "rev-parse", "--show-toplevel", timeout=20)
    if top.returncode != 0:
        return ["project is not an accessible Git worktree"]
    try:
        actual_top = Path(top.stdout.strip()).resolve(strict=True)
    except OSError:
        return ["Git returned an unreadable worktree root"]
    if actual_top != root:
        return [f"kit root is nested inside Git worktree {actual_top}"]
    head = git_head(root)
    if not head:
        return ["repository has no readable baseline commit"]
    if expected_head and head != expected_head:
        return [f"repository HEAD moved from {expected_head} to {head}"]
    blockers = (
        dispatch_scope_blockers(root, head, requested_files)
        if requested_files is not None else []
    )
    status = _git_result(
        root, "status", "--porcelain=v1", "--untracked-files=all", timeout=30
    )
    if status.returncode != 0:
        return [*blockers, "repository cleanliness could not be established"]
    changed: list[str] = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        if len(line) < 4 or line[:2].strip() in {"R", "C"} or " -> " in line:
            return [*blockers,
                    "repository has an unsupported rename or copy in progress"]
        changed.append(line[3:].strip().replace("\\", "/"))
    unexpected = sorted(path for path in changed if path not in allowed_changes)
    if unexpected:
        preview = ", ".join(unexpected[:5])
        suffix = "" if len(unexpected) <= 5 else f" (+{len(unexpected) - 5} more)"
        blockers.append(
            "repository has uncommitted or untracked changes: " + preview + suffix
        )
    return blockers


def prepare_workspace(root: Path, run_id: str, baseline_sha: str,
                      allowed_changes: frozenset[str] = frozenset(),
                      requested_files: object = None) -> Path:
    """Clone one committed baseline into the private runtime for a worker.

    A fully copied local clone is used instead of a linked worktree so an
    untrusted provider cannot mutate the source repository's refs, index, or
    object database through shared Git administrative files. The clone's
    origin is removed, then its private ``.git`` directory is moved outside
    the provider-visible workspace. The trusted host restores that metadata
    only after the provider process exits.
    """
    root = root.resolve(strict=True)
    if RUN_ID.fullmatch(run_id or "") is None:
        raise DispatchWorkspaceError("run id is not safe for a workspace path")
    blockers = dispatch_blockers(
        root, baseline_sha, allowed_changes, requested_files=requested_files
    )
    if blockers:
        raise DispatchWorkspaceError("; ".join(blockers))
    paths = runtime_paths.resolve(root)
    parent = paths.dispatch_workspaces.resolve(strict=False)
    parent.mkdir(parents=True, exist_ok=True)
    workspace = (parent / run_id).resolve(strict=False)
    if workspace.parent != parent:
        raise DispatchWorkspaceError("dispatch workspace escapes its private parent")
    if workspace.exists():
        raise DispatchWorkspaceError(f"dispatch workspace already exists: {workspace}")
    # Use an explicit local file URI and a closed protocol allowlist. This
    # avoids Git for Windows interpreting a drive path as an scp-style remote,
    # while --no-local forces a full object copy instead of sharing hardlinks.
    clone_source = root.as_uri()
    clone_environment = dict(os.environ)
    clone_environment.update({
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_ALLOW_PROTOCOLS": "file",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
    try:
        cloned = subprocess.run(
            [
                _git_executable(root),
                "-c", "protocol.file.allow=always", "clone", "--no-local",
                "--no-hardlinks", "--no-checkout", "--quiet",
                clone_source, str(workspace),
            ],
            cwd=str(root),
            env=clone_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DispatchWorkspaceError(f"isolated Git clone could not start: {exc}") from exc
    if cloned.returncode != 0:
        detail = (cloned.stderr or cloned.stdout).strip()
        raise DispatchWorkspaceError(
            "isolated Git clone failed" + (f": {detail}" if detail else "")
        )
    checkout = _git_result(workspace, "checkout", "--detach", baseline_sha, timeout=120)
    if checkout.returncode != 0:
        detail = (checkout.stderr or checkout.stdout).strip()
        raise DispatchWorkspaceError(
            "isolated baseline checkout failed" + (f": {detail}" if detail else "")
        )
    removed = _git_result(workspace, "remote", "remove", "origin", timeout=20)
    if removed.returncode != 0:
        raise DispatchWorkspaceError("isolated clone origin could not be removed")
    if git_head(workspace) != baseline_sha:
        raise DispatchWorkspaceError("isolated clone does not match the reserved baseline")
    _detach_workspace_metadata(workspace, run_id)
    return workspace


def workspace_result_path(workspace: Path, run_id: str) -> Path:
    if RUN_ID.fullmatch(run_id or "") is None:
        raise DispatchWorkspaceError("run id is not safe for a result path")
    runtime = runtime_paths.resolve(workspace).runtime
    path = runtime / "dispatch" / f"{run_id}.result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.resolve(strict=False).is_relative_to(runtime.resolve(strict=False)):
        raise DispatchWorkspaceError("worker result path escapes private runtime")
    return path


def integrate_workspace(root: Path, workspace: Path, baseline_sha: str,
                        after_sha: str,
                        allowed_changes: frozenset[str] = frozenset()) -> dict:
    """Fast-forward one verified isolated result without merging unrelated work."""
    outcome = {"integrated": False, "errors": [], "before_sha": baseline_sha,
               "after_sha": after_sha}
    if not re.fullmatch(r"[0-9a-f]{40,64}", baseline_sha or ""):
        outcome["errors"].append("integration baseline is not a Git object id")
        return outcome
    if not re.fullmatch(r"[0-9a-f]{40,64}", after_sha or ""):
        outcome["errors"].append("integration result is not a Git object id")
        return outcome
    root = root.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    if git_head(workspace) != after_sha:
        outcome["errors"].append("isolated workspace HEAD no longer matches its result")
        return outcome
    ancestor = _git_result(
        workspace, "merge-base", "--is-ancestor", baseline_sha, after_sha, timeout=30
    )
    if ancestor.returncode != 0:
        outcome["errors"].append("worker result is not descended from its baseline")
        return outcome
    blockers = dispatch_blockers(root, baseline_sha, allowed_changes)
    if blockers:
        outcome["errors"].extend(blockers)
        return outcome
    fetched = _git_result(
        root, "fetch", "--no-tags", "--quiet", str(workspace), after_sha, timeout=180
    )
    if fetched.returncode != 0:
        detail = (fetched.stderr or fetched.stdout).strip()
        outcome["errors"].append(
            "verified worker commit could not be copied to the project"
            + (f": {detail}" if detail else "")
        )
        return outcome
    blockers = dispatch_blockers(root, baseline_sha, allowed_changes)
    if blockers:
        outcome["errors"].extend(blockers)
        return outcome
    merged = _git_result(root, "merge", "--ff-only", "--no-edit", after_sha, timeout=180)
    if merged.returncode != 0:
        detail = (merged.stderr or merged.stdout).strip()
        outcome["errors"].append(
            "verified worker commit could not be fast-forwarded"
            + (f": {detail}" if detail else "")
        )
        return outcome
    if git_head(root) != after_sha:
        outcome["errors"].append("project HEAD does not match the integrated worker result")
        return outcome
    outcome["integrated"] = True
    return outcome


def git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            [_git_executable(root), "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True,
            text=True, timeout=20,
        )
    except (OSError, DispatchWorkspaceError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _changed_files(root: Path, before_sha: str, after_sha: str) -> tuple[list[str], str]:
    try:
        result = subprocess.run(
            [
                _git_executable(root),
                "diff", "--name-only", before_sha, after_sha, "--",
            ],
            cwd=str(root), capture_output=True, text=True, timeout=30,
        )
    except (OSError, DispatchWorkspaceError, subprocess.SubprocessError) as exc:
        return [], str(exc)
    if result.returncode != 0:
        return [], (result.stderr or result.stdout).strip()
    return sorted({line.strip().replace("\\", "/")
                   for line in result.stdout.splitlines() if line.strip()}), ""


def _safe_files(value, field: str = "changed_files") -> tuple[list[str], list[str]]:
    errors: list[str] = []
    if not isinstance(value, list) or not value:
        return [], [f"{field} must be a non-empty list"]
    files: list[str] = []
    for raw in value:
        path = str(raw or "").strip().replace("\\", "/")
        parts = Path(path).parts
        if not path or Path(path).is_absolute() or ".." in parts:
            errors.append(f"unsafe {field} path: {raw!r}")
            continue
        files.append(path)
    return sorted(set(files)), errors


def _config_at_commit(root: Path, commit: str) -> dict:
    """Read trusted dispatch configuration from the reserved baseline commit."""
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise runtime_paths.RuntimeConfigError("dispatch baseline is not a Git object id")
    try:
        result = subprocess.run(
            [_git_executable(root), "show", f"{commit}:kit.config.json"],
            cwd=str(root), capture_output=True, text=True, timeout=20,
        )
    except (OSError, DispatchWorkspaceError, subprocess.SubprocessError) as exc:
        raise runtime_paths.RuntimeConfigError(
            f"cannot read baseline kit.config.json: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise runtime_paths.RuntimeConfigError(
            f"cannot read baseline kit.config.json: {detail or 'git show failed'}"
        )
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise runtime_paths.RuntimeConfigError(
            f"baseline kit.config.json is invalid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise runtime_paths.RuntimeConfigError(
            "baseline kit.config.json must be a schema-1 object"
        )
    return value


def _dispatch_policy(root: Path, baseline_sha: str) -> tuple[
        project_context.PathPolicy | None, list[str]]:
    """Load worker ownership from the immutable pre-dispatch configuration."""
    try:
        config = _config_at_commit(root, baseline_sha)
        value = config.get("dispatch_policy")
        if not isinstance(value, dict):
            raise runtime_paths.RuntimeConfigError(
                "kit.config.json dispatch_policy must be an object"
            )
        unknown = set(value) - {"owned", "forbidden"}
        if unknown:
            raise runtime_paths.RuntimeConfigError(
                "kit.config.json dispatch_policy has unsupported key(s): "
                + ", ".join(sorted(unknown))
            )
        owned = value.get("owned")
        forbidden = value.get("forbidden")
        if (not isinstance(owned, list) or not owned
                or not all(isinstance(item, str) and item.strip() for item in owned)):
            raise runtime_paths.RuntimeConfigError(
                "kit.config.json dispatch_policy.owned must be a non-empty string list"
            )
        if (not isinstance(forbidden, list)
                or not all(isinstance(item, str) and item.strip() for item in forbidden)):
            raise runtime_paths.RuntimeConfigError(
                "kit.config.json dispatch_policy.forbidden must be a string list"
            )
        runtime_relative = project_context.runtime_root_relative(
            config.get("runtime_root", "")
        )
        # Private runtime is derived from the same immutable baseline as the
        # ownership policy.  A maintainer cannot accidentally make session
        # evidence or detached Git metadata writable by omitting it from the
        # hand-maintained forbidden list.
        derived_forbidden = [*forbidden, runtime_relative]
        return project_context.PathPolicy.create(
            root, owned, derived_forbidden
        ), []
    except (runtime_paths.RuntimeConfigError,
            project_context.ProjectContextError, OSError) as exc:
        return None, [f"dispatch ownership policy is invalid: {exc}"]


def dispatch_scope_blockers(root: Path, baseline_sha: str,
                            requested_files: object) -> list[str]:
    """Validate a finding's declared scope before an automatic run starts.

    The same policy from the immutable baseline commit validates both the
    declared target and the worker's eventual change set.  This prevents an
    approved finding aimed at the dispatch or verification control plane from
    consuming provider quota only to be rejected after implementation.
    """
    files, errors = _safe_files(requested_files, "requested_files")
    blockers = [f"dispatch requested scope is invalid: {error}" for error in errors]
    policy, policy_errors = _dispatch_policy(root, baseline_sha)
    blockers.extend(policy_errors)
    if policy is not None:
        blockers.extend(
            "automatic dispatch requires an interactive maintainer for " + error
            for error in policy.violations(files)
        )
    return blockers


def _atomic_result(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _working_files(root: Path) -> tuple[list[str], list[str]]:
    result = _git_result(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all", timeout=30
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return [], ["provider edit scope could not be read" + (f": {detail}" if detail else "")]
    files: list[str] = []
    records = result.stdout.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            return [], ["provider edit scope contains malformed Git status data"]
        status = record[:2]
        if "R" in status or "C" in status:
            return [], ["provider edit scope contains an unsupported rename or copy"]
        path = record[3:].replace("\\", "/")
        safe, errors = _safe_files([path])
        if errors:
            return [], errors
        files.extend(safe)
    return sorted(set(files)), []


def finalize_workspace(result_path: Path, run_id: str, baseline_sha: str,
                       workspace: Path, requested_files: object,
                       summary: str) -> dict:
    """Turn edit-only provider output into a host-owned candidate commit."""
    workspace = workspace.resolve(strict=True)
    result_path = result_path.resolve(strict=False)
    try:
        result_path.relative_to(workspace)
    except ValueError as exc:
        raise DispatchWorkspaceError("host result path escapes the workspace") from exc

    def publish(outcome: str, message: str, **extra) -> dict:
        payload = {
            "schema": SCHEMA,
            "run_id": run_id,
            "outcome": outcome,
            "summary": message,
            **extra,
        }
        _atomic_result(result_path, payload)
        return payload

    if RUN_ID.fullmatch(run_id or "") is None:
        return publish("failed", "dispatcher reservation has an invalid run id")
    try:
        _restore_workspace_metadata(workspace, run_id)
    except DispatchWorkspaceError as exc:
        return publish("failed", str(exc))
    if git_head(workspace) != baseline_sha:
        return publish("failed", "isolated workspace moved from its reserved baseline")
    requested, requested_errors = _safe_files(requested_files, "requested_files")
    changed, change_errors = _working_files(workspace)
    errors = [*requested_errors, *change_errors]
    if not changed and not errors:
        return publish("blocked", "provider exited without proposed file changes")
    outside = sorted(set(changed) - set(requested)) if requested else changed
    if outside:
        errors.append(
            "provider changed files outside the reviewed finding scope: "
            + ", ".join(outside)
        )
    policy, policy_errors = _dispatch_policy(workspace, baseline_sha)
    errors.extend(policy_errors)
    if policy is not None:
        errors.extend(
            f"dispatch ownership violation: {error}"
            for error in policy.violations(changed)
        )
    if errors:
        return publish("failed", "; ".join(errors), changed_files=changed)

    hooks = workspace.parent / f".{run_id}-empty-hooks"
    try:
        hooks.mkdir(exist_ok=False)
        staged = _git_result(
            workspace, "add", "--all", "--", *changed, timeout=120
        )
        if staged.returncode != 0:
            detail = (staged.stderr or staged.stdout).strip()
            return publish(
                "failed", "dispatcher could not stage the bounded candidate"
                + (f": {detail}" if detail else ""), changed_files=changed
            )
        environment = dict(os.environ)
        environment.update({
            "GIT_AUTHOR_NAME": "Agent Kit Dispatcher",
            "GIT_AUTHOR_EMAIL": "agent-kit@localhost",
            "GIT_COMMITTER_NAME": "Agent Kit Dispatcher",
            "GIT_COMMITTER_EMAIL": "agent-kit@localhost",
        })
        committed = _git_result(
            workspace,
            "-c", f"core.hooksPath={hooks}",
            "-c", "commit.gpgsign=false",
            "commit", "--no-verify", "-m", f"kit dispatch: {run_id}",
            timeout=120,
            environment=environment,
        )
        if committed.returncode != 0:
            detail = (committed.stderr or committed.stdout).strip()
            return publish(
                "failed", "dispatcher could not commit the bounded candidate"
                + (f": {detail}" if detail else ""), changed_files=changed
            )
    finally:
        try:
            hooks.rmdir()
        except OSError:
            pass

    after_sha = git_head(workspace)
    actual, diff_error = _changed_files(workspace, baseline_sha, after_sha)
    if diff_error or actual != changed:
        detail = diff_error or f"working={changed}, committed={actual}"
        return publish(
            "failed", f"dispatcher commit differs from the bounded candidate: {detail}",
            changed_files=changed
        )
    return publish(
        "implemented",
        summary.strip() or "implemented the approved finding",
        before_sha=baseline_sha,
        after_sha=after_sha,
        changed_files=actual,
        verification={
            "command": "kit verify --static",
            "passed": False,
            "status": "pending",
        },
        producer="dispatcher",
    )


def evaluate(path: Path, expected_run_id: str, expected_before_sha: str,
             root: Path, run_gate: bool = True, gate_timeout: int = 900,
             requested_files: object = None,
             trusted_host_result: bool = False) -> dict:
    """Return one honest terminal status for a worker result artifact."""
    base = {"status": "unverified", "outcome": None, "summary": "",
            "errors": [], "changed_files": [], "gate": None}
    if not path.is_file():
        base["errors"] = ["worker exited without a result artifact"]
        return base
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        base["errors"] = [f"result artifact is not readable JSON: {exc}"]
        return base
    if not isinstance(value, dict):
        base["errors"] = ["result artifact root must be an object"]
        return base
    if value.get("schema") != SCHEMA:
        base["errors"].append(f"result schema must be {SCHEMA}")
    if value.get("run_id") != expected_run_id:
        base["errors"].append("result run_id does not match the reserved run")
    outcome = str(value.get("outcome") or "").strip().lower()
    if outcome not in OUTCOMES:
        base["errors"].append(f"outcome must be one of {', '.join(OUTCOMES)}")
    summary = str(value.get("summary") or "").strip()
    if not summary:
        base["errors"].append("summary is required")
    base.update({"outcome": outcome or None, "summary": summary})
    if base["errors"]:
        return base
    if outcome == "blocked":
        base["status"] = "blocked"
        return base
    if outcome == "failed":
        base["status"] = "failed"
        return base

    before_sha = str(value.get("before_sha") or "").strip()
    after_sha = str(value.get("after_sha") or "").strip()
    claimed, file_errors = _safe_files(value.get("changed_files"))
    base["changed_files"] = claimed
    base["errors"].extend(file_errors)
    policy, policy_errors = _dispatch_policy(root, expected_before_sha)
    base["errors"].extend(policy_errors)
    if policy is not None:
        base["errors"].extend(
            f"dispatch ownership violation: {error}"
            for error in policy.violations(claimed)
        )
    if requested_files is not None:
        requested, requested_errors = _safe_files(requested_files, "requested_files")
        base["errors"].extend(requested_errors)
        outside = sorted(set(claimed) - set(requested)) if requested else claimed
        if outside:
            base["errors"].append(
                "changed_files exceed the reviewed finding scope: " + ", ".join(outside)
            )
    if before_sha != expected_before_sha or not before_sha:
        base["errors"].append("before_sha does not match the dispatch baseline")
    current_sha = git_head(root)
    if not after_sha or after_sha != current_sha:
        base["errors"].append("after_sha is not the repository's current HEAD")
    if before_sha and after_sha and before_sha == after_sha:
        base["errors"].append("implemented outcome created no commit")
    actual, diff_error = _changed_files(root, before_sha, after_sha)
    if diff_error:
        base["errors"].append(f"could not verify changed files: {diff_error}")
    elif claimed != actual:
        base["errors"].append(
            f"claimed changed_files do not match Git: claimed={claimed}, actual={actual}"
        )
    verification = value.get("verification")
    if (not trusted_host_result
            and (not isinstance(verification, dict)
                 or verification.get("passed") is not True)):
        base["errors"].append("worker did not report a passing verification")
    if trusted_host_result and not run_gate:
        base["errors"].append("host-produced result requires fresh dispatcher verification")
    if base["errors"]:
        return base

    if run_gate:
        static_only = trusted_host_result
        canonical_root = root.resolve(strict=True)
        core = _core_for_project(root)
        public_command = "kit verify"
        try:
            gate_command = process_supervisor.isolated_python_script_command(
                sys.executable, core / "check.py", core
            )
            if static_only:
                gate_command.append("--static")
                public_command += " --static"
            gate = subprocess.run(
                gate_command, cwd=str(root),
                capture_output=True, text=True, timeout=gate_timeout,
                env=process_supervisor.isolated_python_environment(
                    CHILD_ENVIRONMENT if canonical_root == PROJECT_ROOT else {}
                ),
            )
            output = (gate.stdout or "") + (gate.stderr or "")
            passed = gate.returncode == 0 and "GATE PASSED" in output
            base["gate"] = {"passed": passed, "exit_code": gate.returncode,
                            "command": public_command,
                            "tail": "\n".join(output.splitlines()[-30:])}
            if not passed:
                base["errors"].append("fresh dispatcher gate verification failed")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            base["errors"].append(f"fresh dispatcher gate verification failed: {exc}")
    if not base["errors"]:
        base["status"] = "completed"
    return base
