"""live_dashboard.parsers -- recorded fixtures from the live cluster (2026-10-16, llama.cpp b11578)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
FX = os.path.join(HERE, "fixtures", "live_dashboard")

from live_dashboard import parsers as P  # noqa: E402


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return f.read()


def journal_events():
    return [e for e in (P.parse_journal_line(l) for l in fx("journal_chat_last10min_raw.txt").splitlines()) if e]


def test_journal_request_block_parses_in_order():
    kinds = [e["kind"] for e in journal_events()]
    assert kinds == ["start", "prefill", "prefill", "prefill", "prompt_eval", "eval", "total", "draft", "release"]


def test_journal_values():
    ev = {e["kind"]: e for e in journal_events()}
    assert ev["start"]["slot"] == 1 and ev["start"]["task"] == 4772
    assert ev["prompt_eval"]["n"] == 3727 and abs(ev["prompt_eval"]["ms"] - 6154.43) < 1e-6
    assert ev["eval"]["n"] == 23 and abs(ev["eval"]["ms"] - 459.73) < 1e-6
    assert ev["total"]["n"] == 3750
    assert ev["draft"]["accepted"] == 16 and ev["draft"]["generated"] == 21
    assert ev["release"]["n"] == 3750
    p = [e for e in journal_events() if e["kind"] == "prefill"]
    assert [x["n"] for x in p] == [2048, 3211, 3723] and p[-1]["progress"] == 1.0
    assert abs(ev["start"]["since_start_s"] - (552 * 60 + 57.641854)) < 1e-6


def test_journal_noise_and_unauthorized_are_ignored():
    assert P.parse_journal_line("552.57.632.231 W srv    operator(): unauthorized: Invalid API Key") is None
    assert P.parse_journal_line("553.06.878.025 I slot print_timing: id  1 | task 4772 |    graphs reused =       4570") is None
    assert P.parse_journal_line("") is None


def test_journal_cancel_and_server_lifecycle():
    assert P.parse_journal_line("12.00.000.001 W srv          stop: cancel task, id_task = 77") == {
        "kind": "cancel", "since_start_s": 720.000001, "task": 77}
    # This fixture was recorded with `-o short-iso` (ISO stamp + host + `llama-server[pid]: ` before the llama
    # prefix); the dashboard follows `-o cat`, so strip that wrapper to get the production line shape.
    raw = fx("journal_chat_server_start.txt").splitlines()
    cat = [l.split("]: ", 1)[1] if "llama-server[" in l and "]: " in l else l for l in raw]
    kinds = [e["kind"] for e in (P.parse_journal_line(l) for l in cat) if e]
    assert kinds.count("server_start") == 1 and kinds.count("server_ready") == 1
    assert kinds.index("server_start") < kinds.index("server_ready")
    # unwrapped short-iso lines are not `-o cat` lines and must be ignored, not misparsed
    assert all(P.parse_journal_line(l) is None for l in raw)


def test_prometheus_counters_and_labels():
    m = P.parse_prometheus(
        "# HELP x\nllamacpp:prompt_tokens_total 72775\nllamacpp:requests_deferred 0\n"
        'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"} 3761\n')
    assert m["llamacpp:prompt_tokens_total"] == 72775.0 and m["llamacpp:requests_deferred"] == 0.0
    assert m['llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"}'] == 3761.0


def test_slots_normalised():
    d = json.loads(fx("llama_slots_metrics_session.json"))
    s = P.parse_slots(d["samples"][5]["slots"])
    one = [x for x in s if x["id"] == 1][0]
    assert set(one) == {"id", "n_ctx", "is_processing", "id_task", "n_prompt_tokens", "processed", "cache", "n_decoded"}
    assert one["is_processing"] is True and one["id_task"] == 4772 and one["n_ctx"] == 131072


def _materialise(tmp_path, mapping):
    for path, content in mapping.items():
        p = tmp_path / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


def test_gpu_dirs_found_by_bus_and_read(tmp_path):
    root = _materialise(tmp_path, json.loads(fx("gpu_sysfs.json")))
    dirs = P.find_gpu_dirs(os.path.join(root, "sys", "class", "drm"), ["0000:03:00.0", "0000:07:00.0"])
    assert set(dirs) == {"0000:03:00.0", "0000:07:00.0"}
    g = P.read_gpu(dirs["0000:03:00.0"])
    assert g["vram_total"] == 32195477504 and g["vram_used"] == 28283002880
    assert g["junction_c"] == 33.0 and g["power_w"] == 12.0 and g["power_cap_w"] == 180.0
    assert g["load_pct"] == 0.0 and g["link"] == "16.0 GT/s x16" and g["error"] is None


def test_gpu_missing_file_gives_null_field_and_error(tmp_path):
    root = _materialise(tmp_path, json.loads(fx("gpu_sysfs.json")))
    dirs = P.find_gpu_dirs(os.path.join(root, "sys", "class", "drm"), ["0000:03:00.0"])
    os.remove(os.path.join(dirs["0000:03:00.0"], "gpu_busy_percent"))
    g = P.read_gpu(dirs["0000:03:00.0"])
    assert g["load_pct"] is None and g["vram_total"] == 32195477504 and "gpu_busy_percent" in g["error"]


def test_fans(tmp_path):
    hw = tmp_path / "hwmon"
    for name, chip in (("hwmon3", "k10temp"), ("hwmon14", "nct6798")):
        (hw / name).mkdir(parents=True)
        (hw / name / "name").write_text(chip + "\n")
    (hw / "hwmon14" / "pwm5").write_text("64\n")
    (hw / "hwmon14" / "pwm6").write_text("255\n")
    f = P.read_fans(str(hw))
    assert abs(f["pwm5_pct"] - 25.098) < 0.01 and f["pwm6_pct"] == 100.0


def test_proc_parsers():
    lines = fx("proc_stat_2samples.txt").splitlines()
    cpus = [l for l in lines if l.startswith("cpu ")]
    b0, t0 = P.cpu_times(cpus[0] + "\n")
    b1, t1 = P.cpu_times(cpus[1] + "\n")
    assert t1 > t0 and b1 >= b0
    used, total = P.mem_from_meminfo(fx("proc_meminfo.txt"))
    assert 0 < used < total
    r, w = P.disk_sectors(fx("proc_diskstats_and_zfs.txt"), ["nvme1n1", "nvme2n1"])
    assert r == 156792512 + 155774472 and w == 63890216 + 63890216
    size, cmax = P.arc_from_arcstats(fx("proc_diskstats_and_zfs.txt"))
    assert size == 17184893480 and cmax == 17179869184


def test_next_elapse():
    assert P.parse_next_elapse("NextElapseUSecRealtime=Fri 2026-10-16 03:02:27 UTC\n") == 1792119747.0
    assert P.parse_next_elapse("NextElapseUSecRealtime=\n") is None
