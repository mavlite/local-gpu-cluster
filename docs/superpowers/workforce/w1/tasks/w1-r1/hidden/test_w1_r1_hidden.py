"""Hidden checks for w1-r1: failures fan out to the success IDs on every failure path."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_monitor as cm  # noqa: E402


class Probes:
    def __init__(self, cmd=None, http=None):
        self._cmd, self._http = cmd or {}, http or {}

    def cmd(self, args, timeout=10.0):
        return self._cmd.get(" ".join(args), cm.CmdResult(127, "", "not found"))

    def http(self, method, url, *, headers=None, json_body=None, timeout=5.0):
        return self._http.get((method, url), cm.HttpResult(0, "", "no route"))


UPSTREAMS = {"router_chat_upstream", "router_embed_upstream", "router_rerank_upstream"}


def test_a_non_json_healthz_fails_under_the_upstream_ids():
    p = Probes(http={("GET", "http://r:8000/healthz"): cm.HttpResult(200, "<html>bad gateway</html>", "")})
    out = cm.check_router_healthz(p, {"router_url": "http://r:8000"})
    assert {r.id for r in out} == UPSTREAMS
    assert all(r.status == cm.STATUS_FAIL for r in out)


def test_the_gpu_fan_out_follows_the_configured_card_count():
    cfg = {"gpu_vmid": 151, "gpu_card_count": 3, "gpu_vram_warn_pct": 90, "gpu_vram_fail_pct": 98,
           "gpu_temp_warn_c": 95, "gpu_temp_fail_c": 105}
    vram = cm.check_gpu_vram(Probes(), cfg)
    temp = cm.check_gpu_temp(Probes(), cfg)
    assert {r.id for r in vram} == {"gpu_vram_0", "gpu_vram_1", "gpu_vram_2"}
    assert {r.id for r in temp} == {"gpu_temp_0", "gpu_temp_1", "gpu_temp_2"}
    assert all(r.status == cm.STATUS_FAIL for r in vram + temp)


def test_rocm_smi_that_returns_no_cards_also_fans_out():
    cfg = {"gpu_vmid": 151, "gpu_vram_warn_pct": 90, "gpu_vram_fail_pct": 98}
    p = Probes(cmd={"pct exec 151 -- rocm-smi --showmeminfo vram --json": cm.CmdResult(0, "{}", "")})
    out = cm.check_gpu_vram(p, cfg)
    assert {r.id for r in out} == {"gpu_vram_0", "gpu_vram_1"}
