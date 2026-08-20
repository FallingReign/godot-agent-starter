---
name: shell-compat
description: Use before running any shell command in this repo, and immediately when a command fails with an error like "A parameter cannot be found that matches parameter name" or "is not recognized as the name of a cmdlet". Covers translating POSIX shell idioms - ls -la, mkdir -p, heredocs, && chaining - to the PowerShell this environment actually runs. Load this instead of guessing a second time at the same class of failure.
---

# Shell compatibility

This repo is worked on from PowerShell, not bash, even though most agent
training data and most muscle memory is POSIX. The failure is not a one-off:
`ls -la` cost a turn misreading "-la" as a parameter, and `mkdir a/b` cost six
turns across two earlier sessions before someone tried `-Force` and forward
slashes. None of that is a Godot problem or a project-logic problem, so it
does not belong in a `godot-*` skill; it belongs here, loaded whenever a shell
command is about to run or has just failed.

## The four idioms that have actually bitten this repo

| POSIX | Fails as | PowerShell form |
|---|---|---|
| `ls -la` | `-la` parsed as a named parameter, not flags | `Get-ChildItem -Force` (add `\| Format-Table -AutoSize` for wide output) |
| `mkdir -p a/b` | `-p` unrecognised; PowerShell does not auto-create parents by default in older builds | `New-Item -ItemType Directory -Force -Path a\b` (`-Force` also makes it idempotent, no error if it exists) |
| `cmd1 && cmd2` | Works for external commands, but silently does **not** short-circuit before a PowerShell keyword (`if`, `foreach`, assignment) | Use `;` to sequence unconditionally, or check `$LASTEXITCODE`/`$?` explicitly when the second command must not run after a failure |
| `cat <<'EOF' ... EOF` (heredoc) | No heredoc syntax in PowerShell; a literal `<<` is a parse error | A single-quoted here-string: `@'` on its own line, the body, then `'@` at column 0, piped to the target, e.g. `@'\nbody\n'@ \| python -` |

## Why this recurs

An agent reaches for the POSIX form first because it is the more common shape
in training data, notices the specific error text, and re-derives the fix from
scratch each time — burning a full turn per occurrence instead of a lookup.
This skill exists so that recovery is "load the table above" instead of
"reason about PowerShell parameter parsing again."

## What this skill does not cover

Anything about *what* command to run — that is the task at hand. This is only
about translating a known-good POSIX-shaped command into the PowerShell form
that this environment's tool actually executes. If a command fails for a
reason not shaped like the table above (a missing tool, a permissions error,
a wrong path), that is a different problem; do not force it into this skill.
