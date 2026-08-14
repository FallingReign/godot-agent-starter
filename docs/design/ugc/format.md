# Content format

_Resolution: direction_

You can open a map file and read it. So can the game, five years from now, after
the format has changed twice.

## Three properties that cannot be retrofitted

Every file carries an explicit version, and old versions keep loading. Content
made by a player outlives the version it was made in, so a migration path is
part of the format rather than something added when it breaks.

The format is data only. Nothing in a file is executed, evaluated or resolved to
code, because a file arrives from a stranger over a
[direct connection](../multiplayer/multiplayer.md) and is opened by a client
that cannot inspect the sender's intent. This is the same requirement
[authority](../multiplayer/authority.md) states from the trust side.

Unknown keys are refused rather than ignored. A file that half loads is worse
than one that does not, because the failure surfaces later and somewhere else.

## Shape

Plain text, hand-editable, diffable. Not an engine resource format: those carry
identifiers that shift, go through an import step, and can reference code.

A file names the [stamps and layers](../world/cells.md) it uses and where they
are placed. It does not contain geometry.

Small enough to send between players without ceremony, which is a real
constraint rather than an aspiration.

## Open

Whether stamps travel with a place or are referenced by identity.

Whether placement is absolute or relative to a region, which depends on
[world structure](../world/structure.md).
