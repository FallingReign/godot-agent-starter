# Authority

_Resolution: direction_

Two players see the same thing happen. Neither of them can make the other's
game lie.

## Who decides what

State has exactly one owner at any moment, and only the owner may change it.
Everyone else is told. That is the property everything else depends on, so it is
stated here and referenced rather than restated.

A player owns their own position and inputs. The host owns the world: enemies,
loot placement, whether a door opened. Ownership can transfer, but never
ambiguously and never simultaneously.

Session shape belongs to [playing together](multiplayer.md).

## Trust

Content arrives from other players, so nothing received may be executed. A
received place is data that is validated before it is opened, which is why
[the content format](../ugc/format.md) forbids anything executable.

## Open

Whether the host is authoritative over combat resolution or whether an attacking
player resolves their own hits.

What reconciliation looks like when an owner and an observer disagree.

## What would settle it

Two clients, one moving and one watching, with deliberate latency introduced.
Whatever disagrees under that is what needs an owner.
