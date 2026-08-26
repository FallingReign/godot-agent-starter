---
name: godot-typed-data-boundary
description: Use when loading, parsing or saving external data in Godot — JSON, ConfigFile, FileAccess, save files, user-generated content, level or map data — or when hitting parse errors about Variant. Covers errors like "requires the subtype X but the supertype Variant was provided", "Cannot infer the type of variable", unsafe_cast, unsafe_method_access, unsafe_property_access, and any situation where casting a Variant appears necessary. Read before writing a parser or serialiser.
---

# The typed data boundary

## The problem this prevents

Under a strict warnings configuration, every value coming out of `JSON`,
`ConfigFile`, `FileAccess` or `Dictionary` lookup is a `Variant`, and assigning
a `Variant` to a typed variable is an error.

**GDScript has no safe cast that unboxes a Variant.** `value as Type` is flagged
as an unsafe cast. Direct assignment is flagged. A conversion constructor taking
a `Variant` argument is flagged as an unsafe call argument. There is no syntax
that makes this go away at the point of use.

This is the single most expensive mistake to get wrong, because the failure mode
is a loop: you add a cast at one use site, the error moves downstream, you add
another, and the error count never reaches zero. The number of use sites is
unbounded. The number of boundaries is one.

## The rule

**Convert external data to typed objects once, at the edge. Pass only typed
values inward.**

Concretely, in this project:

- Parsing lives in the data layer. That layer is allowed to hold loose
  `Dictionary`, `Array` and `Variant`, because that is what it exists for.
- The logic layer receives typed objects. It never parses external data, and it
  never has a bare `Dictionary`, `Array` or `Variant` in a function signature.

If a gate stage reports a loose type in the logic layer, the fix is never to add
a cast there. The fix is to move the conversion back to the boundary and pass a
typed object.

## How to narrow a Variant safely

Two mechanisms exist. Both are narrowing, not casting.

**Type-check narrowing.** The analyser tracks a local's type inside a branch
guarded by a type check, so returning it from inside that branch is safe. Use
this for container and object types, and always provide a fallback for the
failure path.

**Stringify-then-convert.** Conversions that accept a string are safe from a
`Variant`, because the stringify step itself accepts any type. This is the
standard route to numeric and enumerated values.

Confirm the exact signature of any conversion function you use with
`kit godot-docs show <Type>.<member>` rather than assuming it exists.

## Designing the format so this is cheap

- Prefer a small number of scalar field types. Every extra representation is
  another conversion to write and test.
- Store colours and similar composite values in a form that has a single-call
  string constructor, rather than as nested arrays of numbers.
- Version the format from the first commit, and check the version on load.
- Reject unknown keys rather than ignoring them, so a typo fails loudly.
- Never store a script path, expression or callable. Content authored elsewhere
  is untrusted input, and an executable field is a security hole you cannot
  retrofit a fix for.

## Validation belongs with the conversion

The same function that converts should validate, and it should be the only
validator — called by the loader, by any editor, and by the gate. Two validators
drift, and the one that drifts is always the one CI does not run.

## Checklist before writing a parser

1. Where is the boundary? Name the one file that touches the raw data.
2. What typed objects does it produce?
3. Does any signature outside that file take a loose container? If so, redesign
   before writing more code.
4. Is the version field written and checked?
5. Does validation reject unknown keys?
