"""Hidden checks for w2-16: IPv6 multicast is exempt only unanswered; answered IGMP still fails."""
import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wf-conntrack-check.py")
ALLOWED = ["192.168.6.153:8000"]

MDNS6 = ("udp      17 0 src=fd00::10 dst=ff02::fb sport=5353 dport=5353 [UNREPLIED] "
         "src=ff02::fb dst=fd00::10 sport=5353 dport=5353 mark=176 zone=1 use=1")
IGMP_ANSWERED = ("unknown  2 569 src=10.79.0.10 dst=224.0.0.22 src=224.0.0.22 dst=10.79.0.10 "
                 "mark=176 zone=1 use=1")


def check(text):
    return subprocess.run([sys.executable, SCRIPT, *ALLOWED], input=text, capture_output=True, text=True)


def test_unanswered_ipv6_multicast_is_not_a_flow():
    r = check(MDNS6 + "\n")
    assert r.returncode == 0, r.stdout


def test_an_answered_multicast_entry_of_any_protocol_still_fails():
    r = check(IGMP_ANSWERED + "\n")
    assert r.returncode == 1 and "224.0.0.22" in r.stdout
