import pytest
from fastapi.testclient import TestClient

from reconpilot import importer
from reconpilot.ai import AIClient
from reconpilot.audit import AuditLog
from reconpilot.paypal_client import PayPalSandbox
from reconpilot.server import create_app


def make_client():
    return TestClient(create_app(ai=AIClient(provider="offline"), paypal=PayPalSandbox("", ""), audit=AuditLog()))


def samples(c):
    return c.get("/api/sample/ledger.csv").text, c.get("/api/sample/bank.csv").text


def upload(c, ledger, bank, **kw):
    return c.post("/api/run/upload", json={"ledger_csv": ledger, "bank_csv": bank, "paypal": "demo", **kw})


def test_sample_files_round_trip_to_the_same_18_exceptions():
    c = make_client()
    ledger, bank = samples(c)
    r = upload(c, ledger, bank)
    assert r.status_code == 200
    s = r.json()["summary"]
    assert s["exception_count"] == 18 and s["unexplained_residual"] == 0
    assert r.json()["meta"]["source"] == "upload"
    base = c.post("/api/run", json={"source": "demo"}).json()["summary"]
    assert base["by_type"] == s["by_type"]


def test_other_headers_dates_and_amount_formats_are_understood():
    ledger = ("Invoice Number;Transaction Date;Gross;Customer Name\n"
              "INV-1;10/05/2026;$1,250.00;Ada\nINV-2;2026-10-06;(30.50);Ada\n")
    entries, meta = importer.parse_ledger(ledger)
    assert [(e["ref"], e["amount"], e["type"]) for e in entries] == [("INV-1", 125000, "sale"), ("INV-2", -3050, "refund")]
    assert entries[0]["date"].startswith("2026-10-05") and "fee_booked" not in entries[0] and meta["fee_column"] is False


def test_bank_keeps_deposits_and_skips_withdrawals():
    bank = "Date,Description,Debit,Credit\n2026-10-06,PAYPAL TRANSFER,,100.00\n2026-10-07,RENT,500.00,\n"
    lines, meta = importer.parse_bank(bank)
    assert len(lines) == 1 and lines[0]["amount"] == 10000 and meta["skipped"] == 1


def test_non_sales_rows_are_skipped_not_misread():
    ledger = "date,reference,amount,type\n2026-10-01,A,10.00,sale\n2026-10-01,B,5.00,bank fee\n"
    entries, meta = importer.parse_ledger(ledger)
    assert len(entries) == 1 and meta["skipped"] == 1


def test_ledger_without_fee_column_does_not_flood_fee_variances():
    c = make_client()
    ledger, bank = samples(c)
    stripped = "\n".join(",".join(v for i, v in enumerate(line.split(",")) if i != 4) for line in ledger.splitlines())
    r = upload(c, stripped, bank)
    assert r.status_code == 200
    by = r.json()["summary"]["by_type"]
    assert by.get("FEE_VARIANCE", 0) == 0 and "no fee column" in r.json()["meta"]["books"]


@pytest.mark.parametrize("ledger,needle", [
    ("foo,bar\n1,2\n", "could not find a column"),
    ("date,reference,amount\n2026-10-01,A,twelve\n", "line 2"),
    ("date,reference,amount\nnot-a-date,A,1.00\n", "cannot read date"),
    ("date,reference,amount\n", "no data rows"),
])
def test_bad_books_files_give_a_clear_422(ledger, needle):
    c = make_client()
    r = upload(c, ledger, samples(c)[1])
    assert r.status_code == 422 and needle in r.json()["detail"]


def test_bad_bank_file_is_a_422():
    c = make_client()
    r = upload(c, samples(c)[0], "date,amount\n2026-10-06,-5.00\n")
    assert r.status_code == 422 and "no deposit rows" in r.json()["detail"]


def test_upload_with_sandbox_but_no_credentials_is_a_clear_error():
    c = make_client()
    ledger, bank = samples(c)
    assert upload(c, ledger, bank, paypal="sandbox").status_code == 400
