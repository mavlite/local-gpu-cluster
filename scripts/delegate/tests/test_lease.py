import subprocess, sys, textwrap, time
from scripts.delegate.lease import GpuLease

def test_same_process_second_acquire_fails_fast(tmp_path):
    p = str(tmp_path / "gpu.lock")
    a = GpuLease(p)
    assert a.acquire(timeout_s=0) is True
    b = GpuLease(p)
    assert b.acquire(timeout_s=0) is False
    a.release()
    assert b.acquire(timeout_s=0) is True
    b.release()

def test_cross_process_exclusion(tmp_path):
    p = str(tmp_path / "gpu.lock")
    holder = textwrap.dedent(f"""
        import time, sys
        sys.path.insert(0, r"{tmp_path.parents[100] if False else ''}")
        from scripts.delegate.lease import GpuLease
        g = GpuLease(r"{p}")
        assert g.acquire(timeout_s=0) is True
        print("HELD", flush=True)
        time.sleep(3)
        g.release()
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdout=subprocess.PIPE, cwd=".")
    assert proc.stdout.readline().strip() == b"HELD"   # holder has the lock
    local = GpuLease(p)
    assert local.acquire(timeout_s=0) is False          # we cannot take it
    proc.wait(timeout=10)
    assert local.acquire(timeout_s=1) is True            # free after holder exits
    local.release()

def test_sequential_acquire_release_two_instances(tmp_path):
    p = str(tmp_path / "gpu.lock")
    a = GpuLease(p); assert a.acquire(timeout_s=0) is True
    a.release()
    b = GpuLease(p); assert b.acquire(timeout_s=0) is True
    b.release()   # must not raise
