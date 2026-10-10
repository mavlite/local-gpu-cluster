## Router: a scoped request can take the owner's last chat slot after the layout shrinks

Scoped (workforce) keys may hold at most `chat slots - RESERVED_SLOTS` slots, so the owner always has
one. The lane is enforced when a request enters, but a scoped request can pass the lane and then wait
for the chat gate.

If the chat layout shrinks while it waits (for example 3 slots down to 1 when the redteam layout
exits), it is then admitted into a slot the reserve should have kept for the owner.

**Wanted:** a scoped request whose chat slot arrives after the layout shrank below its lane share is
refused, exactly as a new scoped request would be. It gives back both its lane slot and its chat
slot. `scripts/files/tests/test_workforce_lane.py` covers the lane.
