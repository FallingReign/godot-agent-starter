# Workflow

The public entry point is `kit` (`\.\kit.cmd` on Windows, `./kit` on
macOS/Linux). Internal implementation scripts are not part of the developer
workflow.

## Start every session safely

```text
kit doctor --json
```

Doctor is offline and read-only. If readiness is incomplete, handle only the
named prerequisite or decision. Do not turn a diagnosis into permission for a
download, editor edit, Git mutation, import, format, model call, or Godot launch.

Before changing files, inspect `git status --short` and preserve unrelated work.
Use a worktree for long-running or multi-file work when practical.

## One delivery slice

1. State one observable outcome and the question this increment answers.
2. Retrieve only the relevant design sections. If no end state grounds the work,
   switch to design discovery instead of inventing intent.
3. Check `project.shape.json` for settled decisions, direction, open questions and
   the configured human involvement level.
4. At `module`, `file` or `function` involvement, write the required proposal to
   `proposal.json`, regenerate with `kit plan`, and wait for human approval.
5. Build the thinnest increment that runs and can be judged. Do not deliver an
   invisible infrastructure layer as though it answered an experience question.
6. Run `kit verify --static` while engine evidence is unnecessary or unsafe. Run
   full `kit verify` only when the native-engine boundary is intended and safe.
7. Regenerate `kit plan`. If the result needs visual judgement, launch it through
   the approved project workflow and report what automation cannot prove.
8. At the slice boundary, run `kit friction` and record the factual slice note.
   If retrospective status is due, follow `kit retro status` and the public retro
   workflow.

Nothing is complete merely because a narrow test passed. Report the exact evidence
for the outcome and name every visual, engine, provider, device or platform property
that remains unverified.

## Approval and change control

`proposal.json` is the durable proposal; chat is not. Write experience first,
then an inline SVG mockup when the outcome is vector-approximable, then structure.
Draft status is non-binding. Only the human changes it to approved.

If implementation needs a materially different structure:

1. stop before silently deviating;
2. append the reason to `revisions`;
3. return the proposal to draft;
4. run `kit plan`;
5. ask for the changed decision.

An absent design reference is not something an agent acknowledges for itself.
Offer the human the choice between discovery and an explicitly recorded inference.

## Worktrees and the editor

Godot processes must not share one project directory. `.godot/` contains import,
UID and script-class caches with no cross-process arbitration.

Create and enter a worktree using normal Git commands for the developer's shell:

```text
git worktree add ../wt-feature -b codex/feature
git -C ../wt-feature status --short
```

Each worktree pays for its own initial import. If the editor is open in the main
checkout, keep agent engine work in the worktree. Do not switch branches, pull,
rebase, or remove a worktree while Godot has that directory open.

When integrating, use the project's reviewed Git workflow. Agents never use hard
reset, destructive checkout, clean, force push, or any recovery operation that can
discard unrelated work.

## After a native engine failure

Stop launching the engine. Use only:

```text
kit doctor --json
kit verify --static
kit self-test
```

Doctor reports the executable path without running it. Static verification cannot
discover or launch it. Self-test additionally sets an enforced no-engine boundary.
Resume a full verification only after the human explicitly approves one bounded
retry. The gate performs one health check and suppresses all later native stages
after the first failure.

## Architecture changes

The module graph is derived from code; allowed dependencies live in
`arch.rules.json`.

After an approved module or dependency change:

```text
kit architecture update
kit verify --static
```

Review both the generated graph and the difference from the approved proposal.
An undeclared module and a forbidden dependency are different facts: declare a
clear module, but redesign a boundary violation unless widening the boundary was
the actual decision.

## Scenes, assets and persisted content

Before editing `.tscn` or `.tres`, use the scene-file guidance. Never invent a
`uid://` value or hand-edit `.uid`/`.import` sidecars. A resource-load pass proves
that a scene parses and instantiates; it does not prove appearance or feel.

Before importing the first asset, select an import profile explicitly. Changing
defaults after import does not rewrite existing sidecars.

Before inventing a save, map, level, item or user-authored format, read current
direction and the content-pipeline guidance. Leave room for known future content
without implementing fields that today's slice does not need.

## Retrospectives

At a warranted boundary:

```text
kit retro status
kit retro run
kit retro publish
kit plan
```

The default analyzer is manual and costs nothing. An automatic analyzer requires
the explicit `--confirm-spend` action. Findings cite immutable repository-scoped
snapshots and propose a change plus success measure; they do not directly rewrite
the gate or skills.

Approval and worker dispatch are separate decisions. Dispatch is sequential and
stops at the first failure. Automatic workers are limited to the approved
non-executable documentation scope and finish with static verification on the
trusted host. See `docs/retro/README.md` for the complete lifecycle.

## Release handoff

Before claiming a distributable:

```text
kit self-test
kit verify --static
kit release build ../godot-agent-kit.zip
kit release verify ../godot-agent-kit.zip
```

Legal metadata and a version are mandatory. Full `kit verify --strict` and the
three-platform CI matrix remain required release evidence and may run only after
the engine boundary is safe. Building an archive is local; publishing is a
separate human-controlled action.
