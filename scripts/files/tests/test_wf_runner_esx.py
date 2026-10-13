"""wf-runner-esx.sh: register a runner VM on a Minisforum ESXi host and power it on safely (coding engine phase 2).

Host-direct govc only, so it works in Minimal mode with vCenter down. Two facts learned on hyp01 (2026-10-13) shape
it: a host that vCenter manages refuses *resource* settings (the memory reservation) host-direct while vCenter is up,
but accepts extraConfig (guestinfo, sched.mem.enableTiering). So deploy must not fail on the reservation, and
power-on -- which runs in Minimal mode -- sets it and refuses to start a runner without it, without
enableTiering=FALSE, or on a host whose DRAM would pass 80% consumed (crash-hardening rule 6).

Tests drive the script against a stateful fake govc that logs every call."""
import base64
import hashlib
import os
import shutil
import subprocess

import pytest

FILES = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(FILES, "wf-runner-esx.sh")

FAKE_GOVC = r'''#!/usr/bin/env bash
st="$FAKE_STATE"
printf '%s\n' "$*" >> "$st/calls"
get() { if [[ -f "$st/$1" ]]; then cat "$st/$1"; else echo "$2"; fi; }
case "$1" in
  import.vmdk) [[ -f "$st/fail_import" ]] && exit 1; exit 0 ;;
  vm.create) echo yes > "$st/exists"; exit 0 ;;
  vm.change)
    if [[ " $* " == *" -mem.reservation "* ]]; then
      if [[ -f "$st/vcenter_up" ]]; then
        echo "govc: Access to resource settings on the host is restricted to the server that is managing it" >&2; exit 1
      fi
      prev=""; for a in "$@"; do [[ "$prev" == -mem.reservation ]] && echo "$a" > "$st/reservation"; prev="$a"; done
      exit 0
    fi
    [[ -f "$st/fail_extra" ]] && exit 1
    [[ "$*" == *"sched.mem.enableTiering=FALSE"* ]] && echo FALSE > "$st/tiering"
    exit 0 ;;
  vm.info)
    if [[ " $* " == *" -json "* ]]; then
      printf '{"virtualMachines":['; sep=""
      for m in $(get running_mb ''); do
        printf '%s{"name":"vm%s","config":{"hardware":{"memoryMB":%s}},"runtime":{"powerState":"poweredOn"}}' "$sep" "$m" "$m"; sep=","
      done
      printf '%s{"name":"off","config":{"hardware":{"memoryMB":65536}},"runtime":{"powerState":"poweredOff"}}]}\n' "$sep"
      exit 0
    fi
    [[ "$(get exists no)" == yes ]] || { echo "govc: vm '${!#}' not found" >&2; exit 1; }
    echo "Name:           ${!#}"
    if [[ " $* " == *" -e "* ]]; then
      echo "  ExtraConfig:"
      echo "    nvram:                         x.nvram"
      t="$(get tiering '')"; [[ -n "$t" ]] && echo "    sched.mem.enableTiering:       $t"
    fi
    exit 0 ;;
  object.collect)
    case "$*" in
      *memoryAllocation.reservation*) get reservation 0 ;;
      *config.hardware.memoryMB*) get mem 16384 ;;
      *config.version*) echo vmx-22 ;;
      *runtime.powerState*) get power poweredOff ;;
    esac
    exit 0 ;;
  host.info)
    [[ -f "$st/fail_host" ]] && { echo "govc: connection refused" >&2; exit 1; }
    printf '{"hostSystems":[{"hardware":{"memorySize":%d},"summary":{"quickStats":{"overallMemoryUsage":%d}}}]}\n' \
      $(( 191924 * 1048576 )) "$(get host_used 30000)"
    exit 0 ;;
  vm.power) echo poweredOn > "$st/power"; exit 0 ;;
esac
echo "fake govc: unexpected $*" >&2; exit 99
'''


def find_bash():
    sh = shutil.which("sh")
    if sh is None:
        return None
    cand = os.path.join(os.path.dirname(sh), "bash" + (".exe" if os.name == "nt" else ""))
    return cand if os.path.exists(cand) else shutil.which("bash")


class Esx:
    def __init__(self, tmp_path):
        self.bash = find_bash()
        if self.bash is None:
            pytest.skip("bash not available")
        self.state = tmp_path / "state"
        self.state.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "govc").write_text(FAKE_GOVC, newline="\n")
        (bin_dir / "govc").chmod(0o755)
        self.vmdk = tmp_path / "wf-runner.vmdk"
        self.vmdk.write_bytes(b"disk")
        (tmp_path / "wf-runner.vmdk.sha256").write_text(hashlib.sha256(b"disk").hexdigest() + "\n", newline="\n")
        py = shutil.which("python3") or shutil.which("python") or "python3"
        self.env = {"PATH": f"{bin_dir.as_posix()}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "FAKE_STATE": self.state.as_posix(), "GOVC_USERNAME": "root", "GOVC_PASSWORD": "pw",
                    "PYTHON": py}
        for k in ("SYSTEMROOT", "TEMP", "TMP"):
            if k in os.environ:
                self.env[k] = os.environ[k]

    def set(self, key, value="1"):
        (self.state / key).write_text(value + "\n", newline="\n")

    def run(self, *args, drop=()):
        env = {k: v for k, v in self.env.items() if k not in drop}
        return subprocess.run([self.bash, SCRIPT, *args], capture_output=True, text=True, env=env)

    def deploy(self, name="wf-runner-01", ip="172.16.10.208", **kw):
        return self.run("deploy", "172.16.10.145", "hyp01-local", name, ip, self.vmdk.as_posix(), **kw)

    def power_on(self, **kw):
        return self.run("power-on", "172.16.10.145", "wf-runner-01", **kw)

    def calls(self):
        p = self.state / "calls"
        return p.read_text().splitlines() if p.exists() else []


@pytest.fixture
def esx(tmp_path):
    return Esx(tmp_path)


def test_syntax(esx):
    assert subprocess.run([esx.bash, "-n", SCRIPT], capture_output=True).returncode == 0


def test_deploy_imports_registers_and_configures_powered_off(esx):
    r = esx.deploy()
    assert r.returncode == 0, r.stderr
    calls = esx.calls()
    assert any(c.startswith("import.vmdk -ds hyp01-local ") and c.endswith(" wf-runner-01/wf-runner.vmdk")
               for c in calls), calls
    create = next(c for c in calls if c.startswith("vm.create"))
    for frag in ("-m 16384", "-c 8", "-disk wf-runner-01/wf-runner.vmdk", "-disk.controller pvscsi",
                 "-net SDDC-DPortGroup-VM-Mgmt", "-net.adapter vmxnet3", "-on=false"):
        assert frag in create, (frag, create)
    extra = next(c for c in calls if c.startswith("vm.change") and "guestinfo.metadata=" in c)
    assert "sched.mem.enableTiering=FALSE" in extra and "guestinfo.metadata.encoding=base64" in extra
    assert not any(c.startswith("vm.power") for c in calls)
    assert "NOT powered on" in r.stdout


def test_deploy_metadata_carries_the_static_ip(esx):
    r = esx.deploy(ip="172.16.10.209", name="wf-runner-02")
    assert r.returncode == 0, r.stderr
    extra = next(c for c in esx.calls() if "guestinfo.metadata=" in c)
    meta = base64.b64decode(extra.split("guestinfo.metadata=", 1)[1].split()[0]).decode()
    assert "addresses: [172.16.10.209/24]" in meta and "local-hostname: wf-runner-02" in meta
    assert "via: 172.16.10.1" in meta


def test_reservation_refused_while_vcenter_manages_the_host_is_a_warning_not_a_failure(esx):
    esx.set("vcenter_up")
    r = esx.deploy()
    assert r.returncode == 0, r.stderr
    assert any("sched.mem.enableTiering=FALSE" in c for c in esx.calls())
    assert "power-on sets it" in r.stderr


def test_deploy_fails_when_extra_config_cannot_be_applied(esx):
    esx.set("fail_extra")
    r = esx.deploy()
    assert r.returncode != 0
    assert "enableTiering" in r.stderr


def test_deploy_rerun_on_a_registered_powered_off_vm_reconfigures_without_reimport(esx):
    esx.set("exists", "yes")
    r = esx.deploy()
    assert r.returncode == 0, r.stderr
    calls = esx.calls()
    assert not any(c.startswith(("import.vmdk", "vm.create")) for c in calls)
    assert any("sched.mem.enableTiering=FALSE" in c for c in calls)


def test_deploy_refuses_a_running_vm(esx):
    esx.set("exists", "yes")
    esx.set("power", "poweredOn")
    r = esx.deploy()
    assert r.returncode != 0
    assert not any(c.startswith("vm.change") for c in esx.calls())


def test_deploy_refuses_a_checksum_mismatch(esx):
    esx.vmdk.write_bytes(b"tampered")
    r = esx.deploy()
    assert r.returncode != 0 and "checksum" in r.stderr
    assert not any(c.startswith("import.vmdk") for c in esx.calls())


@pytest.mark.parametrize("name,ip", [("runner-01", "172.16.10.208"), ("wf-runner-01;x", "172.16.10.208"),
                                     ("wf-runner-01", "192.168.6.10"), ("wf-runner-01", "172.16.10.999")])
def test_deploy_rejects_bad_names_and_ips(esx, name, ip):
    r = esx.deploy(name=name, ip=ip)
    assert r.returncode != 0 and esx.calls() == []


def test_credentials_must_come_from_the_environment(esx):
    r = esx.deploy(drop=("GOVC_PASSWORD",))
    assert r.returncode != 0 and "environment" in r.stderr and esx.calls() == []


def test_power_on_sets_a_missing_reservation_then_starts(esx):
    esx.set("exists", "yes")
    esx.set("tiering", "FALSE")
    r = esx.power_on()
    assert r.returncode == 0, r.stderr
    calls = esx.calls()
    res = [i for i, c in enumerate(calls) if "-mem.reservation 16384" in c]
    power = [i for i, c in enumerate(calls) if c.startswith("vm.power -on")]
    assert res and power and res[0] < power[0]


def test_power_on_refuses_without_the_reservation(esx):
    esx.set("exists", "yes")
    esx.set("tiering", "FALSE")
    esx.set("vcenter_up")
    r = esx.power_on()
    assert r.returncode != 0 and "Minimal mode" in r.stderr
    assert not any(c.startswith("vm.power") for c in esx.calls())


def test_power_on_refuses_without_tiering_off(esx):
    esx.set("exists", "yes")
    esx.set("reservation", "16384")
    r = esx.power_on()
    assert r.returncode != 0 and "enableTiering" in r.stderr
    assert not any(c.startswith("vm.power") for c in esx.calls())


@pytest.mark.parametrize("used,starts", [(60000, True), (60620, True), (60621, False), (95124, False)])
def test_power_on_keeps_the_host_at_or_below_80_percent_dram(esx, used, starts):
    # 96256 MB DRAM * 80% = 77004.8 MB; the runner adds 16384 MB, so 60620 fits and 60621 does not
    esx.set("exists", "yes")
    esx.set("tiering", "FALSE")
    esx.set("reservation", "16384")
    esx.set("host_used", str(used))
    r = esx.power_on()
    assert (r.returncode == 0) == starts, r.stderr
    assert any(c.startswith("vm.power") for c in esx.calls()) == starts
    if not starts:
        assert "rule 6" in r.stderr


def ready(esx):
    esx.set("exists", "yes")
    esx.set("tiering", "FALSE")
    esx.set("reservation", "16384")


def test_power_on_counts_memory_committed_to_running_vms_not_only_consumed(esx):
    # a VM started moments ago has its reservation but has not touched its pages yet: overallMemoryUsage lags
    ready(esx)
    esx.set("host_used", "20000")
    esx.set("running_mb", "24576 24576 16384")
    r = esx.power_on()
    assert r.returncode != 0 and "rule 6" in r.stderr
    assert not any(c.startswith("vm.power") for c in esx.calls())


@pytest.mark.parametrize("mem", ["", "unset", "0"])
def test_power_on_fails_closed_when_the_vm_memory_cannot_be_read(esx, mem):
    ready(esx)
    esx.set("mem", mem)
    r = esx.power_on()
    assert r.returncode != 0
    assert not any(c.startswith("vm.power") for c in esx.calls())


def test_power_on_fails_closed_when_host_memory_cannot_be_read(esx):
    ready(esx)
    esx.set("fail_host")
    r = esx.power_on()
    assert r.returncode != 0 and "memory" in r.stderr
    assert not any(c.startswith("vm.power") for c in esx.calls())


def test_power_on_refuses_a_running_vm(esx):
    ready(esx)
    esx.set("power", "poweredOn")
    r = esx.power_on()
    assert r.returncode != 0
    assert not any(c.startswith(("vm.power", "vm.change")) for c in esx.calls())


def test_the_password_never_reaches_argv_or_output(esx):
    esx.env["GOVC_PASSWORD"] = "s3cret-Pa55"
    esx.set("vcenter_up")
    r1 = esx.deploy()
    esx.state.joinpath("vcenter_up").unlink()
    r2 = esx.power_on()
    blob = "\n".join([r1.stdout, r1.stderr, r2.stdout, r2.stderr, *esx.calls()])
    assert r1.returncode == 0 and r2.returncode == 0, (r1.stderr, r2.stderr)
    assert "s3cret-Pa55" not in blob


@pytest.mark.parametrize("ip", ["172.16.10.010", "172.16.10.0", "172.16.10.255"])
def test_deploy_rejects_ambiguous_or_network_addresses(esx, ip):
    r = esx.deploy(ip=ip)
    assert r.returncode != 0 and esx.calls() == []
