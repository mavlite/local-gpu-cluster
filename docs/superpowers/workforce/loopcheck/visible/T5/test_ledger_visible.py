from ledger import skipped_rows, summarize

CSV = "account,amount,currency\nacme,10.00,USD\nacme,2.50,USD\nbeta,,USD\n"


def test_summary_and_skips():
    assert summarize(CSV) == [{"account": "acme", "currency": "USD", "total": "12.50", "count": 2}]
    assert skipped_rows(CSV) == 1
