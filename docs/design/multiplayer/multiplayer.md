# Playing together

_Resolution: direction_

You play with a few friends. One of you starts a session and the others join.
There is nothing between you: no server, no lobby service, no account.

## Session shape

Small groups, peer to peer. A session is started by one player and joined
directly by the others, which means a session exists only while someone is
hosting it.

No central server is a constraint rather than a cost saving. It shapes
[the content format](../ugc/format.md), because a dungeon travels between
players directly and has to be small enough to send and safe enough to open.

Who is allowed to decide what belongs to [authority](authority.md).

## What players share

Position and action, the state of the place they are in, and any
[player-made content](../ugc/ugc.md) the session is using.

Latency is hidden by design rather than eliminated. Interactions are chosen so
that a short delay does not read as unresponsiveness, which constrains
[combat](../combat/combat.md) more than movement.

## Open

How many players a session supports.

What happens to a session when the host leaves.
