"""Request state machine (spec §4). Pure: callers pass `now`. The journal drives the lifecycle (start, prefill
progress, exact timings, release, cancel, server restarts); /slots adds the generating detail and is the only source
while the journal stream is down (rows then end as `approx`)."""
SLOW_NEW_PROMPT_TOKENS = 5000


class Tracker:
    def __init__(self, slot_count: int = 3, approx_grace_s: float = 10.0, unreachable_s: float = 10.0):
        self.slot_count = slot_count
        self.approx_grace_s = approx_grace_s
        self.unreachable_s = unreachable_s
        self.server_run = None          # wall time this server run was first seen
        self.last_since = None          # journal prefix of the previous event (detects restarts)
        self.restarting = False
        self.journal_up = True
        self.unreachable_since = None
        self.open = {}                  # slot -> row dict (+ private live fields)
        self.cancelled = set()          # task ids cancelled in this run
        self.last_done = {}             # slot -> wall time
        self.idle_since = {}            # slot -> wall time the slot was seen idle with a row still open
        self.finished = []

    # ---- helpers
    def _key(self, slot, task):
        return f"{self.server_run:.3f}:{slot}:{task}"

    def _new_row(self, slot, task, now):
        if self.server_run is None:
            self.server_run = now
        self.idle_since.pop(slot, None)
        row = {"key": self._key(slot, task), "server_run": self.server_run, "slot": slot, "task": task,
               "started": now, "finished": None, "status": "reading", "prompt_tokens": None,
               "new_prompt_tokens": None, "cached_tokens": None, "output_tokens": None, "prefill_tok_s": None,
               "gen_tok_s": None, "duration_s": None, "draft_accept": None, "slow": False,
               "_progress": None, "_processed": None, "_live_tok_s": None, "_ctx": None,
               "_dec": None, "_dec_t": None, "_eval": None, "_pe": None, "_total": None,
               "_prompt_est": None, "_rel_n": None}
        self.open[slot] = row
        return row

    def _finish(self, slot, status, now):
        row = self.open.pop(slot, None)
        self.idle_since.pop(slot, None)
        if row is None:
            return
        row["status"], row["finished"] = status, now
        pe, ev, tot = row["_pe"], row["_eval"], row["_total"]
        if pe:
            row["new_prompt_tokens"] = pe["n"]
            row["prefill_tok_s"] = pe["n"] / (pe["ms"] / 1e3) if pe["ms"] > 0 else None
        if ev:
            row["output_tokens"] = ev["n"]
            row["gen_tok_s"] = ev["n"] / (ev["ms"] / 1e3) if ev["ms"] > 0 else None
        if tot:
            row["duration_s"] = tot["ms"] / 1e3
            if ev:
                row["prompt_tokens"] = tot["n"] - ev["n"]
        else:
            row["duration_s"] = now - row["started"]
        if row["output_tokens"] is None and status in ("approx", "cancelled") and row["_dec"] is not None:
            row["output_tokens"] = row["_dec"]
        if status == "approx" and not tot and row["prompt_tokens"] is None and row["_rel_n"] is not None:
            row["prompt_tokens"] = max(0, row["_rel_n"] - (row["_dec"] or 0))
        if row["prompt_tokens"] is not None and row["new_prompt_tokens"] is not None:
            row["cached_tokens"] = max(0, row["prompt_tokens"] - row["new_prompt_tokens"])
        row["slow"] = bool(row["new_prompt_tokens"] and row["new_prompt_tokens"] > SLOW_NEW_PROMPT_TOKENS)
        self.last_done[slot] = now
        self.finished.append({k: v for k, v in row.items() if not k.startswith("_")})

    def _restart(self, now):
        for slot in list(self.open):
            self._finish(slot, "interrupted", now)
        self.idle_since.clear()
        self.server_run = now
        self.cancelled.clear()

    # ---- inputs
    def on_journal(self, ev: dict, now: float) -> None:
        kind, since = ev["kind"], ev.get("since_start_s")
        backwards = since is not None and self.last_since is not None and since < self.last_since - 1.0
        if kind == "server_start" or backwards:
            self._restart(now)
        if kind == "server_start":
            self.restarting = True
        self.last_since = since
        if kind == "server_ready":
            self.restarting = False
            return
        if kind == "server_start":
            return
        if kind in ("start", "prefill", "gen", "prompt_eval", "release"):
            self.restarting = False     # serving again, even if server_ready was missed
        if self.server_run is None:
            self.server_run = now
        if kind == "cancel":
            self.cancelled.add(ev["task"])
            return
        slot, task = ev.get("slot"), ev.get("task")
        if kind == "start":
            cur = self.open.get(slot)
            if cur is not None and cur["task"] == task:
                return                  # adopt the row already opened from /slots
            if cur is not None:
                self._finish(slot, "approx", now)
            self._new_row(slot, task, now)
            return
        row = self.open.get(slot)
        if row is None or row["task"] != task:
            return
        if kind == "prefill":
            if row["status"] == "generating":
                return
            row.update(_progress=ev["progress"], _processed=ev["n"], _live_tok_s=ev["tok_s"], _ctx=ev["n"])
            if ev["progress"] and ev["progress"] > 0:
                row["_prompt_est"] = round(ev["n"] / ev["progress"])
        elif kind == "gen":
            if row["status"] != "generating":
                row["_live_tok_s"] = None
            row.update(status="generating", _live_tok_s=ev["tok_s"])   # /slots owns _dec/_dec_t
        elif kind == "prompt_eval":
            row["_pe"] = {"ms": ev["ms"], "n": ev["n"]}
        elif kind == "eval":
            row["_eval"] = {"ms": ev["ms"], "n": ev["n"]}
        elif kind == "total":
            row["_total"] = {"ms": ev["ms"], "n": ev["n"]}
        elif kind == "draft":
            row["draft_accept"] = ev["accepted"] / ev["generated"] if ev["generated"] else None
        elif kind == "release":
            row["_rel_n"] = ev.get("n")
            status = "cancelled" if task in self.cancelled else "done" if row["_eval"] else "approx"
            self.cancelled.discard(task)
            self._finish(slot, status, now)

    def on_slots(self, slots, now: float) -> None:
        if slots is None:
            if self.unreachable_since is None:
                self.unreachable_since = now
            if not self.journal_up and now - self.unreachable_since >= self.unreachable_s and self.open:
                for slot in list(self.open):
                    self._finish(slot, "interrupted", now)
                self.server_run = now       # the server may have restarted unseen
                self.cancelled.clear()
            return
        self.unreachable_since = None
        for s in slots:
            slot = s["id"]
            row = self.open.get(slot)
            if s["is_processing"]:
                self.restarting = False
                if row is not None and row["task"] != s["id_task"]:
                    if self.journal_up:
                        continue        # journal start/release will sort it out
                    self._finish(slot, "approx", now)
                    row = None
                if row is None and not self.journal_up:
                    row = self._new_row(slot, s["id_task"], now)
                if row is None:
                    continue
                self.idle_since.pop(slot, None)
                if s["n_decoded"] > 0 or not self.journal_up:
                    row["_ctx"] = s["n_prompt_tokens"]
                if s["n_decoded"] > 0:
                    if row["status"] != "generating":
                        row["_live_tok_s"] = None   # never show the prefill rate as gen speed
                    if row["_dec"] is not None and row["_dec_t"] is not None and now > row["_dec_t"]:
                        row["_live_tok_s"] = max(0.0, (s["n_decoded"] - row["_dec"]) / (now - row["_dec_t"]))
                    row.update(status="generating", _dec=s["n_decoded"], _dec_t=now)
            elif row is not None:
                first = self.idle_since.setdefault(slot, now)
                if not self.journal_up or now - first >= self.approx_grace_s:
                    self._finish(slot, "approx", now)

    def set_journal_up(self, up: bool, now: float) -> None:
        self.journal_up = up

    # ---- outputs
    def slot_views(self, now: float) -> list:
        views = []
        for i in range(self.slot_count):
            row = self.open.get(i)
            state = "restarting" if self.restarting else (row["status"] if row else "idle")
            views.append({"id": i, "state": state, "task": row["task"] if row else None,
                          "progress": row["_progress"] if row else None,
                          "prompt_tokens": (row["prompt_tokens"] if row["prompt_tokens"] is not None else row["_prompt_est"]) if row else None,
                          "processed_tokens": row["_processed"] if row else None,
                          "cached_tokens": row["cached_tokens"] if row else None,
                          "output_tokens": row["_dec"] if row else None,
                          "tok_s": row["_live_tok_s"] if row else None,
                          "ctx_used": row["_ctx"] if row else None, "n_ctx": None,
                          "started": row["started"] if row else None, "last_done": self.last_done.get(i)})
        return views

    def open_rows(self) -> list:
        return [{k: v for k, v in r.items() if not k.startswith("_")} for r in self.open.values()]

    def pop_finished(self) -> list:
        out, self.finished = self.finished, []
        return out
