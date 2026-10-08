"""V620 blower fan control (BFB1012SHA01 blowers, 2026-10-08).

v620-temp-publish.sh runs in LXC 151 and publishes the hotter card's JUNCTION temperature (the
sensor that trips at 100 C) for the host bridge, plus the legacy max edge temperature. A card
it cannot read is published as 105 C, so a broken reading means full airflow, never a guess.

v620-fan-bridge.sh runs on the host and maps that junction temperature to a blower duty. The
curve comes from a loaded sweep at the 180 W cap: 90 % holds GPU0 at 81-84 C, 85 % reaches
88 C in 3 min. A missing, stale or unparsable reading drives the fail-safe duty.
"""
import os
import shutil
import subprocess
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(HERE, "..")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

PUBLISHER = os.path.join(FILES, "v620-temp-publish.sh").replace("\\", "/")
BRIDGE = os.path.join(FILES, "v620-fan-bridge.sh").replace("\\", "/")

ROCM_BOTH = """
============================ ROCm System Management Interface ============================
=================================== Temperature ====================================
GPU[0]		: Temperature (Sensor edge) (C): 70.0
GPU[0]		: Temperature (Sensor junction) (C): 83.0
GPU[0]		: Temperature (Sensor memory) (C): 74.0
GPU[1]		: Temperature (Sensor edge) (C): 72.0
GPU[1]		: Temperature (Sensor junction) (C): 80.0
GPU[1]		: Temperature (Sensor memory) (C): 72.0
==========================================================================================
"""


def run(prog, env):
    return subprocess.run([BASH, "-c", prog], capture_output=True, text=True, env=env)


@pytest.fixture
def publisher(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    out = tmp_path / "temps"
    out.mkdir()
    fake = tmp_path / "rocm.txt"

    def publish(rocm_text, rc=0, delay=0, **extra):
        fake.write_text(rocm_text, newline="\n")
        (bindir / "rocm-smi").write_text(f'#!/bin/bash\nsleep {delay}\ncat "$FAKE_ROCM"\nexit {rc}\n',
                                         newline="\n")
        (bindir / "rocm-smi").chmod(0o755)
        env = dict(os.environ, FAKEBIN=bindir.as_posix(), FAKE_ROCM=fake.as_posix(),
                   V620_TEMPS_DIR=out.as_posix(), **extra)
        prog = ('fb="$FAKEBIN"; command -v cygpath >/dev/null && fb="$(cygpath -u "$FAKEBIN")"; '
                f'export PATH="$fb:$PATH"; source "{PUBLISHER}"; publish_once')
        r = run(prog, env)
        assert r.returncode == 0, r.stderr
        return ((out / "current-junction").read_text().strip(), (out / "current-temp").read_text().strip())

    return publish


def test_publisher_writes_hotter_cards_junction_and_edge(publisher):
    assert publisher(ROCM_BOTH) == ("83", "72")


def test_publisher_treats_a_missing_card_as_hot(publisher):
    only_gpu0 = "\n".join(line for line in ROCM_BOTH.splitlines() if "GPU[1]" not in line)
    assert publisher(only_gpu0) == ("105", "105")


def test_publisher_treats_a_failed_rocm_smi_as_hot(publisher):
    assert publisher("", rc=1) == ("105", "105")


def test_publisher_treats_an_unparsable_reading_as_hot(publisher):
    assert publisher(ROCM_BOTH.replace("(C): 80.0", "(C): N/A")) == ("105", "72")


def test_publisher_treats_a_hung_rocm_smi_as_hot(publisher):
    assert publisher(ROCM_BOTH, delay=3, V620_SMI_TIMEOUT="1") == ("105", "105")


def test_publisher_reads_whatever_indices_the_cards_have(publisher):
    shifted = ROCM_BOTH.replace("GPU[1]", "GPU[2]").replace("GPU[0]", "GPU[1]")
    assert publisher(shifted) == ("83", "72")


def test_publisher_output_name_is_what_the_installer_points_the_bridge_at():
    installer = open(os.path.join(FILES, "..", "56-fan-control.sh"), encoding="utf-8").read()
    publisher_src = open(PUBLISHER, encoding="utf-8").read()
    assert "FAN_TEMP_FILE=/var/lib/v620-temps/current-junction" in installer
    assert "publish current-junction " in publisher_src


def test_installer_unit_writes_failsafe_when_the_bridge_stops():
    installer = open(os.path.join(FILES, "..", "56-fan-control.sh"), encoding="utf-8").read()
    assert "ExecStopPost=/usr/local/bin/v620-fan-bridge.sh --failsafe" in installer


def bridge(expr, **env):
    r = run(f'source "{BRIDGE}"; {expr}', dict(os.environ, **env))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("junction,pwm", [
    (30, 64), (59, 64),          # idle: 25 %, inaudible
    (60, 128), (69, 128),        # 50 %
    (70, 191), (77, 191),        # 75 %
    (78, 230), (83, 230),        # 90 %: the lowest duty that holds full load
    (84, 255), (105, 255),       # 100 %
    ("077", 191), ("0105", 255), # never octal
])
def test_curve_maps_junction_to_duty(junction, pwm):
    assert bridge(f"target_pwm {junction}") == str(pwm)


@pytest.fixture
def temp_file(tmp_path):
    path = tmp_path / "current-junction"

    def write(text, age_s):
        path.write_text(text, newline="\n")
        mtime = time.time() - age_s
        os.utime(path, (mtime, mtime))
        return path.as_posix()

    return write


def test_read_temp_returns_a_fresh_reading(temp_file):
    assert bridge(f'read_temp "{temp_file("82", 3)}" 10') == "82"


def test_read_temp_rejects_a_stale_reading(temp_file):
    assert bridge(f'read_temp "{temp_file("40", 30)}" 10') == ""


def test_read_temp_rejects_a_future_reading(temp_file):
    assert bridge(f'read_temp "{temp_file("40", -3600)}" 10') == ""


def test_read_temp_rejects_a_missing_file(tmp_path):
    assert bridge(f'read_temp "{(tmp_path / "absent").as_posix()}" 10') == ""


@pytest.mark.parametrize("text", ["", "abc", "8 2", "-5", "077"])
def test_read_temp_rejects_an_unparsable_reading(temp_file, text):
    assert bridge(f'read_temp "{temp_file(text, 1)}" 10') == ""


@pytest.mark.parametrize("temp,ssc,expected", [
    ("", "", 255),               # no usable reading: fail-safe
    ("", "3", 255),              # boost never lowers the fail-safe
    ("30", "", 64),              # idle, no chat
    ("30", "5", 204),            # chat just arrived: early ramp-up
    ("30", "25", 64),            # chat outside the 20 s window
    ("84", "5", 255),            # curve above the boost wins
])
def test_decide_target_combines_curve_failsafe_and_boost(temp, ssc, expected):
    assert bridge(f'decide_target "{temp}" "{ssc}"') == str(expected)


@pytest.mark.parametrize("cur,target,expected", [
    (64, 230, 230),              # ramp up instantly
    (255, 64, 251),              # ramp down by DECAY_STEP
    (66, 64, 64),                # never undershoot the target
    (64, 0, 64),                 # never below MIN_PWM
])
def test_shape_ramps_up_fast_and_down_slowly(cur, target, expected):
    assert bridge(f"shape {cur} {target}") == str(expected)


def test_tunables_come_from_the_environment():
    assert bridge("decide_target '' ''", FAN_FAILSAFE_PWM="230") == "230"
    assert bridge("shape 255 64", FAN_DECAY_STEP="10") == "245"



@pytest.fixture
def sysfs(tmp_path):
    """A fake /sys/class/hwmon: an nct6798 with pwm5/pwm6 left by the BIOS in auto mode at 25 %."""
    root = tmp_path / "hwmon"
    chip = root / "hwmon14"
    chip.mkdir(parents=True)
    (chip / "name").write_text("nct6798\n", newline="\n")
    for pwm in ("pwm5", "pwm6"):
        (chip / pwm).write_text("64\n", newline="\n")
        (chip / f"{pwm}_enable").write_text("5\n", newline="\n")
    (root / "hwmon3").mkdir()
    (root / "hwmon3" / "name").write_text("k10temp\n", newline="\n")

    def read():
        return {p: ((chip / p).read_text().strip(), (chip / f"{p}_enable").read_text().strip())
                for p in ("pwm5", "pwm6")}

    return root.as_posix(), chip, read


def test_failsafe_mode_drives_every_header_to_full_in_manual(sysfs):
    root, _, read = sysfs
    r = run(f'bash "{BRIDGE}" --failsafe', dict(os.environ, FAN_HWMON_ROOT=root))
    assert r.returncode == 0, r.stderr
    assert read() == {"pwm5": ("255", "1"), "pwm6": ("255", "1")}


def test_write_pwms_reports_a_failed_write(sysfs, tmp_path):
    _, chip, _ = sysfs
    gone = (tmp_path / "gone" / "pwm5").as_posix()
    r = run(f'source "{BRIDGE}"; write_pwms 128 "{(chip / "pwm5").as_posix()}" "{gone}"', dict(os.environ))
    assert r.returncode != 0
    assert (chip / "pwm5").read_text().strip() == "128"
    assert (chip / "pwm5_enable").read_text().strip() == "1"   # manual mode re-asserted every write


def test_main_runs_the_loop_and_a_stop_leaves_the_blowers_at_full(sysfs, temp_file):
    root, _, read = sysfs
    env = dict(os.environ, FAN_HWMON_ROOT=root, FAN_TEMP_FILE=temp_file("30", 0), FAN_TEMP_MAX_AGE="60",
               FAN_BOOST_URL="http://127.0.0.1:9/healthz", FAN_DECAY_STEP="100")
    prog = (f'bash "{BRIDGE}" & pid=$!; sleep 4; '
            # mid-run: the idle reading has pulled the duty down to the floor, in manual mode
            f'cat "{root}/hwmon14/pwm5" "{root}/hwmon14/pwm5_enable"; kill -TERM $pid; wait $pid; echo "rc=$?"')
    r = run(prog, env)
    assert r.stdout.split()[:2] == ["64", "1"], r.stdout + r.stderr
    assert read() == {"pwm5": ("255", "1"), "pwm6": ("255", "1")}   # the exit trap failed safe


def test_installer_unit_counts_a_stop_as_success():
    # The bridge exits 143 on TERM (after writing fail-safe); a deliberate stop must not read "failed".
    installer = open(os.path.join(FILES, "..", "56-fan-control.sh"), encoding="utf-8").read()
    assert "SuccessExitStatus=143" in installer
