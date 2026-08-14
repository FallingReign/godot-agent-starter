---
name: godot-api-lookup
description: Use when you are about to write a Godot API call and are not certain the class, method, property or signal exists, what arguments it takes, or what it returns. Also use when a parse error says a method or property is not present on a type, when you are tempted to guess at an API from memory, or when you want to confirm behaviour changed between Godot versions. Covers looking up the engine class reference for the exact engine version installed here.
---

# Looking up Godot APIs

## The rule

**Do not write an engine API call from memory.** Look it up first.

Model training data for Godot is unreliable: the 3.x to 4.x transition renamed
or removed a large fraction of the API, and 4.x minor releases keep changing it.
A remembered API that no longer exists produces a parse error at best and silent
wrong behaviour at worst.

Web documentation is also not a reliable answer, because the docs site serves
whichever version the URL names. Landing on a 4.3 page while building against a
different version is a subtler error than admitting you do not know.

## The authoritative source is local

The engine binary can dump its own complete class reference. That reference
describes exactly the engine that will run this project, so it cannot be out of
date relative to reality.

```
python tools/gddoc.py --build             # once, generates the reference
python tools/gddoc.py Color               # class summary
python tools/gddoc.py Color.from_string   # exact signature and description
python tools/gddoc.py --search from_str   # find a member across all classes
```

Never read the generated XML directly, and never dump it into context. It is
roughly a thousand files. Query it.

## When to look something up

Always, before writing the call, if any of these is true:

- You have not used this class in this project yet.
- You are unsure whether a method is static, and on which type it lives.
- You are unsure of argument order, argument types, or the return type.
- You are about to use something you associate with Godot 3.
- A parse error says a method or property is not present on an inferred type.
- You are choosing between two similar-sounding APIs.

The cost of a lookup is one command. The cost of a guess is a failed gate run,
a re-read, and a repair — and sometimes code that parses and silently does
nothing.

## What the local reference does not cover

The class reference is API truth: what exists, its signature, its brief
description. It does not explain architecture, idiom, or intent.

For conceptual material — how to structure something, why an approach is
preferred, tutorials, migration guides — the official online documentation is
the right source. Fetch it if you have web access, and check that the page is
for this project's engine version before trusting it.

If you have no web access and the concept is unclear, say so and ask, rather
than inventing an approach and presenting it as established practice.

## Reporting

When you looked something up and it contradicted what you expected, say so in
your summary. That is useful signal, not an admission of weakness.
