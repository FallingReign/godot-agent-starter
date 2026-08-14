---
name: godot-content-pipeline
description: Use BEFORE inventing any format that persists content - save files, level or map documents, item definitions. Also use when adding art, audio or model files to a Godot project, when import settings or .import sidecar files are involved, when building user-generated content or an in-game editor, or when deciding whether game content should be a Godot resource or a custom text format. Also use when assets look wrong after import or when the import cache misbehaves.
---

# Assets and content data

## Two different things

**Assets** are source files the engine imports: images, audio, models. The engine
converts them into an internal form and writes a sidecar file recording how.

**Content** is data your game defines: levels, maps, item definitions, save
files. You own that format entirely.

The distinction matters because the rules are opposite. Asset metadata is
machine-generated and must never be hand-edited. Content format is yours to
design and should be plain text you fully control.

## Assets

Drop the source file in and run the project's import step. The engine generates
the sidecar. Commit both the source and the sidecar; never hand-edit the sidecar.

**Import defaults are effectively a one-time decision.** The engine writes every
parameter into each sidecar individually rather than recording "uses the default",
so changing a project-wide default does not propagate to anything already
imported. Adopting new defaults later means deleting every sidecar and
reimporting, which is an enormous diff. Set them before the asset library exists.

If the import cache becomes inconsistent, deleting the engine's cache directory
and reimporting is the normal repair, not a symptom of something broken.

## Content format: the choice depends on who authors it

Check `project.shape.json` for a decision on who authors content. If there is
none, ask before choosing a format - the format is hard to change later, and
the UGC answer is materially different from the others.

"Will players author and share this content, or is it authored only by you?" is
answerable at the point you are designing the format. Record it per
`.agents/skills/godot-project-decisions/SKILL.md`.

**Players author and share it (`ugc`).** Plain text you define. Non-negotiable
here, for three reasons: the format is a permanent public contract, so it needs
an explicit version field from the first commit; engine resource files can embed
script paths, which is a code-execution vector the day a player downloads
someone else's content; and an in-game editor must read and write it without the
Godot editor present.

**Procedurally generated (`procedural`).** Plain text or no file at all - if it
is derived from a seed, store the seed. Text still wins for anything a human
inspects while debugging generation.

**Hand-authored by your team only (`hand-authored`).** Either is defensible.
Engine resources give you the inspector, typed fields and reference tracking for
free, which is real authoring value. Plain text gives you reviewable diffs and
lets an agent generate content directly. Prefer text if an agent will be
producing content; prefer resources if a human authors everything in the editor
and values the inspector.

**Unknown.** Ask. Do not pick for them - this decision is expensive to reverse
once content exists.

## Read direction before choosing the document shape

Check `direction` in `project.shape.json` first. It records what is known about
where the project is heading, and for a persisted format that changes the
document **shape** even though it changes nothing you build today.

A format designed for exactly today's fields is locally correct and globally
expensive. Adding a field later is cheap. Restructuring a document is not: it
needs a version bump and a migration of every file already authored, including
anything a player has made.

So if layers are coming, cell data sits inside a named container rather than at
the document root. If reusable tiles are coming, the document has room for a
tile table. You implement neither. You leave the room.

If `direction` is empty and you are about to invent a persisted format, ask:
what will this eventually need to hold? That is the only moment where the
answer is cheap to act on. Record the reply as direction, then design the
shape, then implement only today's fields.

## Three properties to decide before the first commit

**Versioned.** Every file carries a format version; every load checks it. If
players will ever author content, old content must keep loading after the format
changes. Retrofitting a version field onto files already in the wild is not
possible.

**Strict.** Reject unknown fields rather than ignoring them, so a typo fails
loudly instead of silently doing nothing.

**Single validator.** One validation function, called by the loader, by any
in-game or development editor, and by the project's checks. Two validators drift,
and the one that drifts is the one CI does not run.

## If development and players share an editor

Building content with the same editor players will use is a strong choice: it
guarantees the format round-trips, and it means the tool is exercised constantly.

It also means the format is a public contract from the first release. Version it,
validate it, and treat every field as something you will support indefinitely.

## Checklist

0. For content: read `direction` - what must the document shape leave room for?
1. Is this an asset or content? Do not mix the rules.
2. For assets: sidecar committed, never edited, defaults already decided.
3. For content: plain text, versioned, strict, data-only.
4. Is there exactly one validator, and does the gate run it?
