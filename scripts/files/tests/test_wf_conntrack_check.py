"""Host-side conntrack check (security review HIGH: build-mode connections survive `75 locked`)."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "wf-conntrack-check.py")
ALLOWED = ["192.168.6.153:8000", "172.16.10.205:8090", "172.16.10.206:8090", "172.16.10.207:8090"]

TCP_OK = ("tcp      6 431999 ESTABLISHED src=10.79.0.10 dst=192.168.6.153 sport=40404 dport=8000 "
          "src=192.168.6.153 dst=192.168.6.79 sport=8000 dport=40404 [ASSURED] mark=0 use=1")
UDP_C2 = ("udp      17 29 src=10.79.0.10 dst=203.0.113.9 sport=5353 dport=443 "
          "src=203.0.113.9 dst=192.168.6.79 sport=443 dport=5353 mark=0 use=1")
TCP_OTHER_PORT = ("tcp      6 299 ESTABLISHED src=10.79.0.10 dst=192.168.6.153 sport=4 dport=22 "
                  "src=192.168.6.153 dst=192.168.6.79 sport=22 dport=4 [ASSURED] mark=0 use=1")
ICMP = ("icmp     1 29 src=10.79.0.10 dst=1.1.1.1 type=8 code=0 id=7 src=1.1.1.1 dst=192.168.6.79 type=0 "
        "code=0 id=7 mark=0 use=1")


def check(stdin):
    return subprocess.run([sys.executable, SCRIPT, *ALLOWED], input=stdin, capture_output=True, text=True)


def test_only_allowed_flows_pass():
    r = check(TCP_OK + "\n")
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_surviving_build_mode_flow_fails_and_is_named():
    r = check("\n".join([TCP_OK, UDP_C2]) + "\n")
    assert r.returncode == 1 and "203.0.113.9:443" in r.stdout


def test_the_right_host_on_the_wrong_port_fails():
    assert check(TCP_OTHER_PORT + "\n").returncode == 1


def test_portless_protocols_fail():
    assert check(ICMP + "\n").returncode == 1


def test_an_empty_table_passes_and_garbage_is_an_error():
    assert check("").returncode == 0
    assert check("conntrack v1.4.8 (conntrack-tools): 0 flow entries have been shown.\n").returncode == 0
    assert check("tcp 6 1 ESTABLISHED weird\n").returncode == 2
