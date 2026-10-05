# T5 — Ledger report

Implement `summarize(csv_text)` and `skipped_rows(csv_text)` in `ledger.py`. Every rule below is
tested; nothing else is. Use the standard `csv` module and `decimal.Decimal`; never use floats.

**Input.** CSV text with a header row. The required columns are `account`, `amount` and
`currency`.
- They may appear in **any order**. Extra columns are ignored.
- Header names are matched after stripping whitespace, case-sensitively.
- A missing required column raises `ValueError`.

**Rows.**
- `account` and `currency` are stripped.
- `amount` is stripped and parsed as `Decimal`.
- A row whose `amount` is empty after stripping is **skipped**.
- A non-empty `amount` that is not a valid decimal raises `ValueError`.
- Negative amounts are valid.

**`summarize(csv_text)`** returns a `list` of dicts, one per account, **sorted by account
ascending**:
`{"account": str, "currency": str, "total": str, "count": int}`
- `total` is the sum of the account's amounts, formatted with **exactly two decimal places**,
  rounded `ROUND_HALF_UP`. For example, `"10.00"`, `"-0.50"` and `"2.68"` (from `2.675`).
- `count` is the number of non-skipped rows for the account.
- If one account appears with **two different currencies**, raise `ValueError`. The message must
  contain the account name.

**`skipped_rows(csv_text)`** returns the number of skipped rows, as an int.
