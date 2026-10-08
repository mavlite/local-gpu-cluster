"""Hidden checks for w2-11: the configured container id is used for both GPU checks."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_monitor as cm  # noqa: E402

CFG = {"gpu_vmid": 160, "gpu_vram_warn_pct": 90, "gpu_vram_fail_pct": 98,
       "gpu_temp_warn_c": 95, "gpu_temp_fail_c": 105}


class Probes:
    def __init__(self, cmd):
        self._cmd = cmd

    def cmd(self, args, timeout=10.0):
        return self._cmd.get(" ".join(args), cm.CmdResult(127, "", "not found"))

    def http(self, method, url, **kw):
        return cm.HttpResult(0, "", "no route")


def test_vram_runs_in_the_configured_container():
    vram = {"card0": {"VRAM Total Memory (B)": "34359738368", "VRAM Total Used Memory (B)": "1073741824"}}
    p = Probes({"pct exec 160 -- rocm-smi --showmeminfo vram --json": cm.CmdResult(0, json.dumps(vram), "")})
    out = cm.check_gpu_vram(p, CFG)
    assert [r.status for r in out] == [cm.STATUS_OK]


def test_temperature_runs_in_the_configured_container():
    temp = {"card0": {"Temperature (Sensor junction) (C)": "45.0"}}
    p = Probes({"pct exec 160 -- rocm-smi --showtemp --json": cm.CmdResult(0, json.dumps(temp), "")})
    out = cm.check_gpu_temp(p, CFG)
    assert [r.status for r in out] == [cm.STATUS_OK]
