---
name: godot-performance-evidence
description: Use when Godot performance is in question — something feels slow, frame rate drops, many objects are being created, or someone proposes optimising, rewriting in C++, using GDExtension, or adopting an ECS or data-oriented design. Also use before optimising anything, when choosing between per-object nodes and bulk server calls, and when deciding what evidence justifies a rewrite.
---

# Performance and what justifies a rewrite

## The rule

**No optimisation without a measurement, and no rewrite without a comparison.**

Scripting language overhead is rarely the bottleneck. Script code calls into the
same native engine underneath, so once execution reaches an engine call the
language difference largely disappears. The real costs are elsewhere: draw calls,
object count, per-call overhead crossing a language boundary, and work repeated
every frame that could be cached or made event-driven.

Guessing which of those applies is how optimisation effort gets spent in the
wrong place.

## Where cost actually accumulates

**Object count.** Thousands of individually managed objects each carrying tree
membership, transforms and callbacks is expensive regardless of language. The fix
is not a faster language; it is fewer objects. Drive many things through a bulk
server API with handles instead.

**Per-call boundary crossings.** This is the trap that makes naive native ports
*slower* than script. Each call across the boundary has fixed overhead. A loop
that crosses per item can cost more than the equivalent script loop. Native code
wins when it does bulk work behind a single call and returns bulk data.

**Repeated per-frame work.** Recomputing something unchanged, or rebuilding a
buffer that could be updated only when dirty. Separate static from dynamic and
rebuild only what changed.

**Allocation churn.** Creating and discarding objects every frame. Reuse.

## The escalation ladder

Take these in order. Do not skip.

1. **Measure.** Identify the specific hot path with real numbers, on a real build
   rather than an editor session.
2. **Reduce work.** Fewer objects, less per-frame recomputation, caching,
   event-driven instead of polling. Most problems end here.
3. **Restructure data.** Flat contiguous arrays, bulk updates, separate static
   from dynamic. This is where data-oriented thinking pays off, and it pays off
   *in script* — you do not need a different language to get it.
4. **Port the specific hot path to native code**, only with a before-and-after
   measurement of that path, and only if it can be structured as bulk work.

A proposal to adopt an ECS, or to move to a native extension, that arrives before
step 1 is a proposal without evidence. Say so.

## What a native port actually costs

A build toolchain, a version-pinning obligation between the extension and the
engine, a platform matrix, and iteration that requires recompilation. Some of
those are invisible until they break, and one of the failure modes is the
extension silently not loading at all — which an automated loop will misread as
success.

Keeping the boundary bulk, and keeping the surface small, is what makes it worth
paying.

## Designing so a port stays cheap

Write logic as plain classes over flat, typed data now. That shape ports to a
native structure mechanically. Logic entangled with nodes and globals does not,
because every global read inside a hot loop becomes a boundary crossing.

This is the same architectural rule that makes code testable, which is why it is
worth following before any performance question arises.

## Reporting

If you cannot measure — because measurement needs a real device, a real build, or
human observation — say that explicitly and hand it back. Do not report a
performance improvement you have not measured.

## If the fork is the human's to make

The trade-offs above are yours to weigh, but some forks are not technical
choices at all - they turn on scope, audience, art direction or how much the
project intends to carry. If the right answer here depends on something like
that and nothing in `project.shape.json` decides it, raise it rather than
picking, and record the answer per
`.agents/skills/godot-project-decisions/SKILL.md`.
