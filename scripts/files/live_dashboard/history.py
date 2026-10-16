"""SQLite request history (spec §2, §5). One writer thread owns all writes; reads use their own short-lived
connections. `ok` is current WRITE health: transient lock/busy errors are retried and never flip it; a persistent
failure flips it False, the writer retries every `recover_s`, and flips it back on success. While not ok, up to
BACKLOG_MAX recent rows are kept in memory and written on recovery (older ones are dropped). Reads never depend
on `ok`."""
import logging
import os
import queue
import sqlite3
import statistics
import threading
import time
from collections import deque

log = logging.getLogger("live_dashboard.history")
COLUMNS = ("key", "server_run", "slot", "task", "started", "finished", "status", "prompt_tokens",
           "new_prompt_tokens", "cached_tokens", "output_tokens", "prefill_tok_s", "gen_tok_s", "duration_s",
           "draft_accept", "slow")
QUEUE_MAX = 10000
BACKLOG_MAX = 1000
RETRY_BACKOFF = (0.2, 0.5, 1.0)
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
    def __init__(self, path: str, retention_days: int = 30, busy_timeout_s: float = 10.0, recover_s: float = 30.0):
        self.path = path
        self.retention_s = retention_days * 86400.0
        self.busy_timeout_s = busy_timeout_s
        self.recover_s = recover_s
        self.ok = True
        self._q = queue.Queue(maxsize=QUEUE_MAX)
        self._backlog = deque(maxlen=BACKLOG_MAX)
        self._closed = False
        self._lost = False  # a write was dropped/failed since the last flush
        try:
            self._setup()
        except sqlite3.Error as e:
            log.error("history unavailable: %s", e)
            self.ok = False
        self._t = threading.Thread(target=self._writer, name="history-writer", daemon=True)
        self._t.start()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=self.busy_timeout_s)

    def _setup(self):
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE IF NOT EXISTS requests (" + ", ".join(
                f"{c} {'TEXT PRIMARY KEY' if c == 'key' else 'TEXT' if c == 'status' else 'REAL'}" for c in COLUMNS) + ")")
            con.execute("CREATE INDEX IF NOT EXISTS requests_finished ON requests(finished)")
            con.execute("CREATE INDEX IF NOT EXISTS requests_status_finished ON requests(status, finished)")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _transient(e) -> bool:
        m = str(e).lower()
        return isinstance(e, sqlite3.OperationalError) and ("locked" in m or "busy" in m)

    def _run(self, con, kind, payload, done):
        """Apply one item, retrying transient lock errors. Returns the connection; raises on persistent failure."""
        for attempt in range(len(RETRY_BACKOFF) + 1):
            try:
                if con is None:
                    con = self._connect()
                self._apply(con, kind, payload, done)
                return con
            except DATA_ERRORS as e:
                log.warning("history dropped one %s: %s", kind, e)
                self._rollback(con)
                self._lost = True
                return con
            except sqlite3.OperationalError as e:
                self._rollback(con)
                if not self._transient(e):
                    raise
                if attempt < len(RETRY_BACKOFF):
                    time.sleep(RETRY_BACKOFF[attempt])
                else:
                    log.error("history dropped one %s after retries: %s", kind, e)
                    self._lost = True
                    return con
        return con

    def _writer(self):
        con = None
        next_recover = time.monotonic() + self.recover_s
        try:
            while True:
                try:
                    item = self._q.get(timeout=None if self.ok else 0.1)
                except queue.Empty:
                    item = "tick"
                if item is None:
                    break
                if not self.ok and time.monotonic() >= next_recover:
                    con = self._try_recover(con)
                    next_recover = time.monotonic() + self.recover_s
                if item == "tick":
                    continue
                kind, payload, done = item
                try:
                    con = self._handle(con, kind, payload, done)
                finally:
                    if done is not None:
                        done["event"].set()
        finally:
            if con is not None:
                con.close()

    def _handle(self, con, kind, payload, done):
        if kind == "noop":
            done["ok"] = self.ok and not self._lost
            self._lost = False
            return con
        if not self.ok:
            if kind == "add":
                self._keep(payload)
            return con
        try:
            return self._run(con, kind, payload, done)
        except Exception as e:  # the writer must never die; a persistent failure disables writes until recovery
            log.error("history write failed: %s", e)
            self.ok = False
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            if kind == "add":
                self._keep(payload)
            return None

    def _keep(self, row):
        if len(self._backlog) == self._backlog.maxlen:
            self._lost = True
        self._backlog.append(row)

    def _try_recover(self, con):
        try:
            if con is not None:
                con.close()
                con = None
            self._setup()
        except Exception as e:
            log.warning("history still unavailable: %s", e)
            return None
        self.ok = True
        log.warning("history recovered")
        while self._backlog and self.ok:
            con = self._handle(con, "add", self._backlog.popleft(), None)
        return con

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

    def add(self, row: dict) -> None:
        if self._closed or not self._t.is_alive():
            return
        if not isinstance(row, dict) or row.get("key") is None or row.get("finished") is None:
            log.warning("history rejected a row without key/finished")
            self._lost = True
            return
        try:
            self._q.put_nowait(("add", dict(row), None))
        except queue.Full:
            log.warning("history queue full; dropping a write")
            self._lost = True

    def _roundtrip(self, kind, payload, timeout, done):
        if self._closed or not self._t.is_alive():
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
        """True only if the writer drained everything queued and no write since the last flush was dropped/failed."""
        done = {"event": threading.Event(), "ok": False}
        return self._roundtrip("noop", None, timeout, done) and done["ok"]

    def prune(self, now: float) -> int:
        done = {"event": threading.Event(), "deleted": 0}
        self._roundtrip("prune", now - self.retention_s, 10.0, done)
        return done["deleted"]

    def _read(self, sql, args):
        if not os.path.isfile(self.path):
            return []
        try:
            con = self._connect()
            try:
                return con.execute(sql, args).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []

    @staticmethod
    def _num(v, kind):
        try:
            return kind(v) if v is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    def recent(self, limit: int) -> list:
        limit = max(1, min(int(limit), 500))
        rows = self._read(f"SELECT {', '.join(COLUMNS)} FROM requests ORDER BY finished DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(zip(COLUMNS, r))
            d["slow"] = bool(d["slow"])
            for c in ("slot", "task", "prompt_tokens", "new_prompt_tokens", "cached_tokens", "output_tokens"):
                d[c] = self._num(d[c], int)
            out.append(d)
        return out

    def bands(self, days: int, now: float) -> list:
        days = max(1, min(int(days), 30))
        rows = self._read("SELECT prompt_tokens, gen_tok_s, prefill_tok_s FROM requests "
                          "WHERE status = 'done' AND finished >= ? AND prompt_tokens IS NOT NULL "
                          "ORDER BY prompt_tokens", (now - days * 86400.0,))
        groups = {}
        for prompt, gen, pre in rows:
            b = band_of(self._num(prompt, int))
            if b:
                groups.setdefault(b, []).append((self._num(gen, float), self._num(pre, float)))
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
