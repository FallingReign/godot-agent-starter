# How project design works

`docs/design/` documents the game being built. The rest of `docs/` documents
the kit. A distributable contains this guide but no project's design body.

A design section is written before the code it governs. It records intended
experience and constraints, not a reconstruction of whatever the code already
does.

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

## Resolution

Every section declares its current maturity below the title:

```markdown
_Resolution: settled_
```

- `question` means the decision is open; record the question without inventing
  an answer.
- `direction` means there is a leaning and a stated way to settle it.
- `settled` means code may depend on it.

The same document can mature through all three states. Never write beyond the
resolution the human has reached.

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

## Questions and decisions

An open question lives in the domain it concerns under `## Open`. When it is
settled, append the decision to `project.shape.json` and point `docs_at` to the
section that explains the current intent.
