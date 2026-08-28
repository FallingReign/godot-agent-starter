# Slice notes

One file per slice, written by the game builder at slice end. See
`../README.md` for the shape and `.github/agents/game-builder.agent.md` for the
template.

These are consumed and archived by the retrospective agent. Do not edit a note
after the slice it describes.

## Deterministic warning declarations

Ordinary prose is testimony and is never severity-classified. When one of these
consequences was directly observed, add its exact declaration on its own line:

```text
consequence: native-crash
```

Immediate consequence codes are `native-crash`, `false-green`,
`unauthorized-provider-use`, `irreversible-without-approval`, and
`work-destroyed`. Prompt-level trigger codes are `wrong-built`,
`repeated-correction`, and `repeated-verification-failure`, declared as:

```text
retro_trigger: repeated-verification-failure
```

Omit the declaration when nothing in the closed list happened. Never write
`none`, invent a code, infer a code from the human's mood, or promote ordinary
friction into a consequence. `kit retro status` reports unknown codes visibly.
