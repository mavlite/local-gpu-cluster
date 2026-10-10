## Fan bridge: a deliberate stop leaves the unit marked "failed"

`v620-fan-bridge.service` (installed by `scripts/56-fan-control.sh`) traps TERM. On stop it writes the
fail-safe 100% duty to every blower header, then exits with status 143.

systemd treats 143 as a failure. Every `systemctl stop v620-fan-bridge` therefore leaves the unit in
the `failed` state, with `Result: exit-code`. That reads as a broken fan bridge in monitoring and in
`systemctl --failed`, although the stop did exactly what it should.

**Wanted:** a deliberate stop of the bridge is reported as a success. Real failures must still show
as failures, and the fail-safe on stop must stay. `scripts/files/tests/test_v620_fan.py` covers the
bridge and the installer.
