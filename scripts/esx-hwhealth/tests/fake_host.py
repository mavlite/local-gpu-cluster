"""A scripted ESX host for collector tests: answers every argv the readers issue."""
from fakes import fixture
from hwhealth.smn import CCD, ROOT_CFG, TCTL

UNIT_RAW = 0xA1000  # energy unit 2**-16 J
COUNTS_PER_J = 65536
TCTL_RAW = (1001 << 21) | (1 << 19)  # 76.125 C
CCD_RAW = (1 << 11) | 1000  # 76.0 C
PERF_TEMPLATE = "   APERF reading:{a}\n   MPERF reading:{m}\n"


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Host:
    """Callable Runner. Override attributes to script faults."""

    def __init__(self, clock=None, family=25, model=97):
        self.clock = clock or Clock()
        self.family, self.model = family, model
        self.index = 0
        self.index_reg = 0  # what a GET of the index register returns while non-zero
        self.energy_calls = 0
        self.perf_calls = {}
        self.joules = 150.47
        self.smn = {TCTL: TCTL_RAW, CCD[0]: CCD_RAW, CCD[1]: CCD_RAW}
        self.aperf2 = lambda p, a, m: (a + 1000, m + 2000)
        self.on_call = None  # hook(argv) for scripting clock jumps
        self.calls = []

    def __call__(self, argv, timeout):
        argv = tuple(argv)
        self.calls.append(argv)
        if self.on_call:
            self.on_call(argv)
        if argv[:3] == ("esxcli", "hardware", "cpu"):
            return (fixture("cpu_list.txt").replace("Family: 25", f"Family: {self.family}")
                    .replace("Model: 97", f"Model: {self.model}"))
        if argv[:2] == ("esxcli", "nvme"):
            return self._nvme(argv)
        if argv[:3] == ("vsish", "-e", "get"):
            return self._get(argv[3])
        if argv[:3] == ("vsish", "-e", "set"):
            assert argv[3] == f"{ROOT_CFG}/0x60"
            self.index = int(argv[4], 16)
            return ""
        raise AssertionError(f"unexpected call: {argv}")

    def _get(self, path):
        if path.endswith("0xC0010299"):
            return hex(UNIT_RAW)
        if path.endswith("0xC001029B"):
            self.energy_calls += 1
            extra = round(self.joules * COUNTS_PER_J) if self.energy_calls > 1 else 0
            return hex(0xA1000 + extra)
        if path.startswith("/power/pcpu/"):
            p = int(path.split("/")[3])
            n = self.perf_calls[p] = self.perf_calls.get(p, 0) + 1
            a, m = 565115497405, 270057413775
            if n > 1:
                a, m = self.aperf2(p, a, m)
            return PERF_TEMPLATE.format(a=a, m=m)
        if path == f"{ROOT_CFG}/0x60":
            return hex(self.index_reg or self.index)
        if path == f"{ROOT_CFG}/0x64":
            return hex(self.smn[self.index])
        raise AssertionError(f"unexpected get: {path}")

    def _nvme(self, argv):
        if argv[2:4] == ("device", "list"):
            return fixture("nvme_device_list.txt")
        adapter = argv[-1]
        if "smart" in argv:
            return fixture(f"nvme_smart_{adapter}.txt")
        return fixture(f"nvme_device_get_{adapter}.txt")

    @property
    def msr_or_smn_calls(self):
        return [c for c in self.calls if c[0] == "vsish"]
