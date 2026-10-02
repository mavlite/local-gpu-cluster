import msvcrt, os, time

class GpuLease:
    def __init__(self, path: str):
        self.path = path
        self._fh = None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def acquire(self, timeout_s: float = 0) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            fh = open(self.path, "a+")
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
                self._fh = fh
                return True
            except OSError:
                try:
                    fh.close()
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)

    def held_by(self):
        try:
            with open(self.path) as f:
                v = f.read().strip()
                return int(v) if v else None
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._fh.close()
                self._fh = None
