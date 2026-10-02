import json, os, datetime

class Ledger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def append(self, record: dict) -> None:
        rec = dict(record)
        rec.setdefault("ts", datetime.datetime.now(datetime.timezone.utc).isoformat())
        line = json.dumps(rec, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    def read_all(self) -> list:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
