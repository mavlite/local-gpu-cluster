"""SQLite request history (spec §2, §5). One writer thread owns all writes; reads use their own short-lived
connections. If the database can't be opened or written, `ok` turns False and the live view carries on."""
import logging
import queue
import sqlite3
import statistics
import threading
import time

log = logging.getLogger("live_dashboard.history")
COLUMNS = ("key", "server_run", "slot", "task", "started", "finished", "status", "prompt_tokens",
           "new_prompt_tokens", "cached_tokens", "output_tokens", "prefill_tok_s", "gen_tok_s", "duration_s",
           "draft_accept", "slow")
QUEUE_MAX = 10000
# Per-row problems (bad values) must not disable the store; only real database failures do.
DATA_ERRORS = (TypeError, ValueError, KeyError, OverflowError, sqlite3.InterfaceError,
               sqlite3.ProgrammingError, sqlite3.IntegrityError, sqlite3.DataError)
BANDS = ((8192, "0-8K"), (32768, "8-32K"), (65536, "32-64K"), (98304, "64-96K"))


def band_of(prompt_tokens):
    if prompt_tokens is None:
        return None
    for limit, name in BANDS:
        if prompt_tokens < limit:
            return name
    return "96-128K"


class History:
    def __init__(self, path: str, retention_days: int = 30):
        self.path = path
        self.retention_s = retention_days * 86400.0
        self.ok = True
        self._q = queue.Queue(maxsize=QUEUE_MAX)
        self._closed = False
        con = None
        try:
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE IF NOT EXISTS requests (" + ", ".join(
                f"{c} {'TEXT PRIMARY KEY' if c == 'key' else 'TEXT' if c == 'status' else 'REAL'}" for c in COLUMNS) + ")")
            con.execute("CREATE INDEX IF NOT EXISTS requests_finished ON requests(finished)")
            con.commit()
        except sqlite3.Error as e:
            log.error("history unavailable: %s", e)
            self.ok = False
        finally:
            if con is not None:
                con.close()
        self._t = threading.Thread(target=self._writer, name="history-writer", daemon=True)
        self._t.start()

    def _writer(self):
        con = None
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                kind, payload, done = item
                try:
                    if self.ok and kind != "noop":
                        if con is None:
                            con = sqlite3.connect(self.path)
                        self._apply(con, kind, payload, done)
                except DATA_ERRORS as e:
                    log.warning("history dropped one %s: %s", kind, e)
                    self._rollback(con)
                except Exception as e:  # the writer must never die; a real failure disables the store
                    log.error("history write failed: %s", e)
                    self.ok = False
                    self._rollback(con)
                finally:
                    if done is not None:
                        done["event"].set()
        finally:
            if con is not None:
                con.close()

    @staticmethod
    def _rollback(con):
        try:
            if con is not None:
                con.rollback()
        except sqlite3.Error:
            pass

    @staticmethod
    def _apply(con, kind, payload, done):
        if kind == "add":
            con.execute(f"INSERT OR REPLACE INTO requests ({', '.join(COLUMNS)}) VALUES "
                        f"({', '.join('?' * len(COLUMNS))})",
                        [int(payload["slow"]) if c == "slow" else payload.get(c) for c in COLUMNS])
        elif kind == "prune":
            cur = con.execute("DELETE FROM requests WHERE finished < ?", (payload,))
            done["deleted"] = cur.rowcount
        con.commit()

    def _alive(self) -> bool:
        return self.ok and not self._closed and self._t.is_alive()

    def add(self, row: dict) -> None:
        if not self._alive():
            return
        try:
            self._q.put_nowait(("add", dict(row), None))
        except (queue.Full, TypeError, ValueError):
            log.warning("history queue full or bad row; dropping a write")

    def _roundtrip(self, kind, payload, timeout, done):
        if not self._alive():
            return False
        try:
            self._q.put((kind, payload, done), timeout=min(timeout, 1.0))
        except queue.Full:
            return False
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if done["event"].wait(0.05):
                return True
            if not self._t.is_alive():
                return False
        return False

    def flush(self, timeout: float = 5.0) -> bool:
        return self._roundtrip("noop", None, timeout, {"event": threading.Event()})

    def prune(self, now: float) -> int:
        done = {"event": threading.Event(), "deleted": 0}
        self._roundtrip("prune", now - self.retention_s, 10.0, done)
        return done["deleted"]

    def _read(self, sql, args):
        if not self.ok:
            return []
        try:
            con = sqlite3.connect(self.path)
            try:
                return con.execute(sql, args).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []

    def recent(self, limit: int) -> list:
        limit = max(1, min(int(limit), 500))
        rows = self._read(f"SELECT {', '.join(COLUMNS)} FROM requests ORDER BY finished DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(zip(COLUMNS, r))
            d["slow"] = bool(d["slow"])
            for c in ("slot", "task", "prompt_tokens", "new_prompt_tokens", "cached_tokens", "output_tokens"):
                d[c] = int(d[c]) if d[c] is not None else None
            out.append(d)
        return out

    def bands(self, days: int, now: float) -> list:
        days = max(1, min(int(days), 30))
        rows = self._read("SELECT prompt_tokens, gen_tok_s, prefill_tok_s FROM requests "
                          "WHERE status = 'done' AND finished >= ?", (now - days * 86400.0,))
        groups = {}
        for prompt, gen, pre in rows:
            b = band_of(int(prompt) if prompt is not None else None)
            if b:
                groups.setdefault(b, []).append((gen, pre))
        order = [name for _, name in BANDS] + ["96-128K"]
        out = []
        for name in order:
            if name in groups:
                g = [x for x, _ in groups[name] if x is not None]
                p = [y for _, y in groups[name] if y is not None]
                out.append({"band": name, "n": len(groups[name]),
                            "gen_tok_s": statistics.median(g) if g else None,
                            "prefill_tok_s": statistics.median(p) if p else None})
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._q.put(None, timeout=2.0)
        except queue.Full:
            log.error("history queue full at close; writer left to daemon exit")
            return
        self._t.join(5.0)
