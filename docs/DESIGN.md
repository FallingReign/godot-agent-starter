# How project design works

`docs/design/` documents the game being built. The rest of `docs/` documents
the kit. A distributable contains this guide but no project's design body.

A design section is written before the code it governs. It records intended
player experience, player-visible outcomes and gameplay rules, not a
reconstruction of whatever the code already does. Architecture, file layout,
frameworks and implementation mechanics belong in the proposal unless a
technical property is itself part of gameplay.

## Where a document goes

Ask one question:

> Which domain would be incoherent without this?

That domain owns the rule. Other domains link to it and state only how they
differ. A domain starts as one file and becomes a folder only when a second
document belongs to it. For example, `audio.md` can become
`audio/audio.md` and `audio/dynamic-mix.md`.

Do not create `foundations/`, `systems/`, `shared/`, or `common/` as a second
home for cross-domain rules. Cross-domain reach is expressed through links.

## Goals and rules

A goal admits several implementations. Accessibility may affect input, audio,
dialogue, and interface design differently. Put project-wide goals in
`pillars.md`; each affected domain records its own response.

A rule admits one implementation. It has one owning domain and every consumer
links to it. Repeating a rule in each domain creates several sources of truth.

## Resolution, authority and authorship

Every new or changed section declares four independent facts below the title:

```markdown
_Resolution: settled_
_Authority: human-confirmed_
_Authored by: human_
_Confidence: very-high_
```

- `question` means the decision is open; record the question without inventing
  an answer.
- `direction` means there is a leaning and a stated way to settle it.
- `settled` means the content is complete enough to act on if its authority also
  permits that action.

Resolution is not approval. Authority is either `human-confirmed` or
`agent-provisional`; authorship is either `human` or `agent`; confidence is one
of `very-high`, `high`, `medium`, or `low`. An agent may have authored design
that a human later confirms: in that case `Authored by` remains `agent` and only
`Authority` changes.

The same document can mature through all three resolutions. Legacy sections
without authority metadata remain readable but cannot silently authorize new
implementation. Invalid or partly written metadata is rejected rather than
interpreted.

## When an agent writes missing design

The default is to ask. An agent may write an inferred design only when it can
state why that inference is correct with exactly `very-high` confidence. It
must mark the design `agent-provisional` and `Authored by: agent`; `high` is not
close enough. The document then carries four exact headings:

```markdown
## Quick read

- **Player does:** the moment-to-moment action.
- **Player experiences:** the intended feel and feedback.
- **Successful outcome:** what the player can understand or achieve.

## Why this inference

The evidence in existing design and the human's request that makes this the
single high-confidence reading.

## Assumptions

- Every fact supplied by the agent rather than the human or existing design.

## Veto and go/no-go

- **Veto scope:** what experience can still change without locking in the
  inferred intent.
- **Next go/no-go:** the next player-facing commitment requiring explicit human
  confirmation.
```

These sections remain when the human later confirms agent-authored design. They
make provenance auditable; confirmation does not rewrite history.

An agent-provisional, settled section is eligible to guide implementation only
at exact `very-high` confidence and only while the proposal is `recorded` and
`reversibility.state` is `reversible`. This is permission to gather reversible
evidence, not approval. Silence, elapsed time, code already written and a
legacy no-design acknowledgement never change authority.

## Shape of a section

Open with what the player experiences, without an extra heading. Follow with
the rules the system must preserve. Use headings that fit the concern.

These markers are load-bearing when present:

- `## Tunables` lists adjustable values and ranges.
- `## Open` holds unresolved questions.
- `## What would settle it` states the evidence needed for a question or
  direction.

Design declares a tunable and its acceptable range; code owns the current value:

```markdown
| Tunable | What it controls | Range that still feels right |
|---|---|---|
| `audio.music_level_db` | Music level relative to effects | -18 dB to -6 dB |
```

```gdscript
## @tune audio.music_level_db
## Music level relative to effects.
@export var music_level_db: float = -12.0
```

Design never stores a code path or duplicates the current value. `kit plan`
regenerates `docs/design/INDEX.md` and binding tables from the design body and
code claims. The index is generated and must not be hand-edited.

Each proposal reference binds the canonical SHA-256 reported for the section.
The digest normalizes line endings and ignores generated binding tables, but
otherwise covers the authored design. A changed digest means the proposal was
based on different design and must be re-recorded or approved again. When a
human confirms a section, its decision entry records both `docs_at` and
`design_sha256`. It also records an authority-normalized
`design_intent_sha256`: confirmation may change the Authority header without
changing the intended player experience, so a later design veto survives that
bookkeeping transition and unrelated plan, baseline or envelope edits.

`Veto design and plan` rejects each referenced design intent independently.
For an agent-authored section the active Authority header returns to
`agent-provisional`; for human-authored design the document is left intact and
the append-only decision ledger carries the veto. In both cases implementation
authority remains withdrawn until the design intent itself changes or a later
explicit cockpit confirmation supersedes the veto. `Request changes` is
different: it withdraws the exact plan only and does not veto design.

## Questions and decisions

An open question lives in the domain it concerns under `## Open`. When it is
settled, append the decision to `project.shape.json` and point `docs_at` to the
section that explains the current intent. An explicit local operator confirmation
also records the section's canonical `design_sha256`; its portable receipt is
tamper-evident policy evidence, not person authentication. An agent must never
manufacture that confirmation on the operator's behalf.
