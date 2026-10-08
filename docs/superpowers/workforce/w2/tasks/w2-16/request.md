## Sandbox: the boundary proof refuses on the guest's own boot-time multicast

Before every measured window, `76-vm-wf-sandbox.sh proof` first checks the host's conntrack table: any
tracked sandbox flow that isn't TCP/UDP to an allowed `ip:port` fails it
(`scripts/files/wf-conntrack-check.py`).

On the first locked-mode proof it refused on two flows from the freshly booted guest:
- an IGMP report to `224.0.0.22`;
- an LLMNR query to `224.0.0.252:5355`.

Both were marked `[UNREPLIED]`. tcpdump showed neither got past the PVE filter: 3 packets on
`tap176i0`, 0 on `fwln176i0`, `fwpr176p0` and `vmbrwf`. The kernel confirms bridged multicast in
conntrack before the per-port filter drops the copies.

**Wanted:**
- Unanswered entries to a multicast address are not flows.
- An **answered** multicast entry, or any unanswered unicast entry, still fails.

`scripts/files/tests/test_wf_conntrack_check.py` holds verbatim entries from the host.
