import pytest

from ledger import skipped_rows, summarize

CSV = """\
 currency ,note,amount,account
EUR,x,10,acme
EUR,,2.675,beta
EUR,y,-0.5,acme
EUR,z,   ,beta
EUR,,0.005,gamma
"""


def test_any_column_order_extra_columns_and_sorting():
    out = summarize(CSV)
    assert [r["account"] for r in out] == ["acme", "beta", "gamma"]
    acme = out[0]
    assert acme == {"account": "acme", "currency": "EUR", "total": "9.50", "count": 2}


def test_round_half_up_two_places():
    out = {r["account"]: r for r in summarize(CSV)}
    assert out["beta"]["total"] == "2.68"
    assert out["gamma"]["total"] == "0.01"


def test_blank_amounts_skipped_and_counted():
    assert skipped_rows(CSV) == 1
    beta = [r for r in summarize(CSV) if r["account"] == "beta"][0]
    assert beta["count"] == 1


def test_negative_total_formatting():
    out = summarize("account,amount,currency\nx,-0.5,USD\n")
    assert out == [{"account": "x", "currency": "USD", "total": "-0.50", "count": 1}]


def test_two_currencies_rejected_with_account_name():
    with pytest.raises(ValueError, match="acme"):
        summarize("account,amount,currency\nacme,1,EUR\nacme,2,USD\n")


def test_missing_column_and_bad_amount():
    with pytest.raises(ValueError):
        summarize("account,amount\nx,1\n")
    with pytest.raises(ValueError):
        summarize("account,amount,currency\nx,abc,EUR\n")


def test_no_float_drift():
    rows = "account,amount,currency\n" + "a,0.1,EUR\n" * 3
    assert summarize(rows)[0]["total"] == "0.30"
