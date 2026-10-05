"""Reference solution for T5 (never copied into a worker's workspace)."""
import csv
import io
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_REQUIRED = ("account", "amount", "currency")


def _rows(csv_text):
    reader = csv.reader(io.StringIO(csv_text))
    header = [h.strip() for h in next(reader)]
    missing = [c for c in _REQUIRED if c not in header]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    idx = {c: header.index(c) for c in _REQUIRED}
    for row in reader:
        if not row:
            continue
        yield {c: row[idx[c]].strip() for c in _REQUIRED}


def _parse(csv_text):
    totals, skipped = {}, 0
    for r in _rows(csv_text):
        if not r["amount"]:
            skipped += 1
            continue
        try:
            amount = Decimal(r["amount"])
        except InvalidOperation:
            raise ValueError(f"bad amount: {r['amount']!r}") from None
        acc = totals.setdefault(r["account"], {"currency": r["currency"], "total": Decimal(0), "count": 0})
        if acc["currency"] != r["currency"]:
            raise ValueError(f"account {r['account']} has two currencies")
        acc["total"] += amount
        acc["count"] += 1
    return totals, skipped


def summarize(csv_text):
    totals, _ = _parse(csv_text)
    return [{"account": a, "currency": v["currency"],
             "total": str(v["total"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
             "count": v["count"]} for a, v in sorted(totals.items())]


def skipped_rows(csv_text):
    return _parse(csv_text)[1]
