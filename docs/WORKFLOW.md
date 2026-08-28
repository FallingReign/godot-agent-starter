# Workflow

The public entry point is `kit` (`.\kit.cmd` on Windows, `./kit` on
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
   switch to design discovery. Ask the human unless one inferred design is
   defensible at exactly `very-high` confidence; if so, disclose it as
   agent-authored and agent-provisional before delivery continues.
3. Check `project.shape.json` for settled decisions, direction, open questions and
   the configured human involvement level.
4. Write the proposal with design digests, design authority and the reversible
   envelope. At `hands-off`, record coarse file/directory `scope[]` boundaries;
   `recorded` permits work only inside them while it is reversible.
   At `module`, `file` or `function`, keep it `draft`, run `kit serve start`,
   give the human the printed loopback `plan.html` URL, and wait for a cockpit
   decision on that exact fingerprint. Module scope includes the complete
   allowed outgoing dependency set and boundary data; file scope includes new,
   modified and deleted authored inputs; function scope additionally binds each
   changed GDScript function's typed canonical signature and purpose.
5. Build the thinnest increment that runs and can be judged. Do not deliver an
   invisible infrastructure layer as though it answered an experience question.
6. Run `kit verify --static` while engine evidence is unnecessary or unsafe. Run
   full `kit verify` only when the native-engine boundary is intended and safe.
7. Regenerate `kit plan`. If the result needs visual judgement, launch it through
   the approved project workflow only when no unresolved native failure or
   concurrent engine process exists, and report what automation cannot prove.
8. At the slice boundary, run `kit friction` and record the factual slice note.
   If retrospective status is due, follow `kit retro status` and the public retro
   workflow.

Nothing is complete merely because a narrow test passed. Report the exact evidence
for the outcome and name every visual, engine, provider, device or platform property
that remains unverified.

## Approval and change control

`proposal.json` is the durable proposal; chat is not. Write experience first,
then an inline SVG mockup when the outcome is vector-approximable, then
`design_authority`, `reversibility`, and structure.

- `draft` authorizes no implementation.
- `recorded` is a non-approval audit state for hands-off work inside a reversible
  envelope and its explicit coarse `scope[]`. Agent-provisional design additionally
  requires exact `very-high` confidence.
- `approved` records explicit human approval and requires human-confirmed design.

Only an explicit operator action in the local loopback cockpit may supply
`approved_by`, `approved_on`, `approval_sha256`, the transition to `approved`,
and append-only `authority_action: confirm` entries bound to matching
`docs_at`, `design_sha256`, `design_intent_sha256` and `proposal_sha256`.
Each event also carries a portable `cockpit_receipt_id`, the exact reviewed
fingerprint and a tamper-evident receipt digest. The gate rejects missing or
inconsistent receipt evidence, so hand-filling only the approval fields cannot
silently grant authority. This is not cryptographic person authentication: a
writer with repository access can deliberately forge both an event and its
public hash. Local operator presence and repository write access are explicit
policy trust boundaries. When the originating private runtime receipt is
present, a mismatch fails closed; when it is absent after clone or CI, the
cockpit labels the event `portable-policy`, never verified or authenticated.
Stronger human identity would require an explicitly provisioned signing
mechanism and key policy. It is an optional future integration, not a dependency
the kit installs or infers. Never hand-fill authority events.
At every involvement level, `reversibility.state: go-no-go` stops work before
the hard-to-undo commitment. **Request changes** withdraws only the exact
reviewed plan and leaves the referenced design-authority state unchanged,
including confirmed authority when present. This wording applies equally to a
recorded agent-provisional plan: `recorded` was never approval. **Veto design and
plan** binds every referenced section's authority-normalized design intent; a
baseline, envelope or other plan-only edit cannot erase it. It remains active
until that exact intent changes or a later explicit cockpit confirmation
supersedes it. Agent-authored confirmed sections return to provisional metadata;
human-authored section metadata is not rewritten, but its veto still blocks
implementation.

An open question is promoted into the active cockpit only when its optional
`related_slices[]` or `related_design_refs[]` relation matches the current
proposal. Older unscoped questions remain visible in the full project record
with a legacy label; they are not silently discarded or presented as decisions
for unrelated work.

If implementation needs a materially different structure:

1. stop before silently deviating;
2. append the reason to `revisions`;
3. return the proposal to draft;
4. run `kit serve start` and give the human the printed review URL;
5. either re-record a still-reversible hands-off plan or ask for the changed
   decision.

An absent design reference cannot be acknowledged into authority. Write the
missing design first. If the agent authors it, the document must contain the
Quick read, inference rationale, assumptions, veto scope and next go/no-go; it
remains provisional until the human confirms the exact digest.

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
The cockpit and `kit retro status` keep the unresolved native consequence visible;
a later static, fast or targeted check does not clear it. Resume a full
verification only after the human explicitly approves one bounded retry. The gate
performs one health check and suppresses all later native stages after the first
failure. Only a successful complete native verification resolves the warning.

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

Conformance uses one inverse authored-input policy: every regular file under the
configured game root is in scope unless it is a known generated sidecar, engine
file, private runtime surface or third-party dependency. Custom content
extensions, extensionless content, project tests and deletions therefore cannot
disappear from review. Function-level review compares canonical typed signatures
and normalized bodies at the exact Git baseline; a source or baseline it cannot
parse or retrieve is a blocking unknown, not an omitted function.

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

Legal metadata, a version and a clean source repository are mandatory. A dirty
manifest may be inspected diagnostically but cannot pass production release
verification. Build and verification also require the manifest's explicit
`receipt_trust: portable-policy` and `portable-policy-audit` identity model.
This is reviewable policy evidence, not person authentication. Any project
proposal/shape receipt state is evaluated before those files are excluded; a
present mismatch blocks the build, while a clean kit with no project state is
explicitly not applicable. Full `kit verify --strict` and the
three-platform CI matrix remain required release evidence and may run only after
the engine boundary is safe. Building an archive is local; publishing is a
separate human-controlled action.
