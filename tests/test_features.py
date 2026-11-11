import json

import pytest
from fastapi.testclient import TestClient

from reconpilot import datagen, importer
from reconpilot.ai import AIClient
from reconpilot.audit import AuditLog
from reconpilot.paypal_client import PayPalSandbox
from reconpilot.server import create_app
from tests.test_api import anthropic_mock


def make_client(ai=None):
    return TestClient(create_app(ai=ai or AIClient(provider="offline"), paypal=PayPalSandbox("", ""), audit=AuditLog()))


def dispute_id(c):
    c.post("/api/run", json={"source": "demo"})
    return next(x for x in c.get("/api/result").json()["exceptions"] if x["type"] == "DISPUTE_OPEN")["id"]


# ---------------- dispute reply draft ----------------
def test_dispute_draft_template_uses_run_facts_and_placeholders():
    c = make_client()
    ex = dispute_id(c)
    d = c.post(f"/api/exceptions/{ex}/dispute-draft").json()
    assert d["source"] == "template" and "INV-" in d["body"] and "[attach proof of delivery" in d["body"]
    assert not any(n in d["body"] for n in datagen.CUSTOMERS)
    assert any(e["action"] == "dispute_draft" for e in c.get("/api/audit").json()["entries"])


def test_dispute_draft_only_for_disputes():
    c = make_client()
    dispute_id(c)
    other = next(x for x in c.get("/api/result").json()["exceptions"] if x["type"] != "DISPUTE_OPEN")
    assert c.post(f"/api/exceptions/{other['id']}/dispute-draft").status_code == 400


def test_model_draft_is_accepted_only_if_it_cites_the_real_ids(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    c0 = make_client()
    ex = dispute_id(c0)
    facts_ref = c0.post(f"/api/exceptions/{ex}/dispute-draft").json()["body"]
    ref = facts_ref.split("invoice ")[1].split(" ")[0]
    txn = facts_ref.split("original transaction ")[1].split(")")[0]

    good = json.dumps({"subject": "Dispute reply", "body": f"Re {ref}, transaction {txn}. [attach proof of delivery]"})
    transport, seen = anthropic_mock(good)
    c = make_client(AIClient(provider="anthropic", transport=transport))
    c.post("/api/run", json={"source": "demo"})
    out = c.post(f"/api/exceptions/{ex}/dispute-draft").json()
    assert out["source"].startswith("anthropic:") and ref in out["body"]
    assert not any(n in seen["body"]["messages"][0]["content"] for n in datagen.CUSTOMERS)

    bad_transport, _ = anthropic_mock(json.dumps({"subject": "x", "body": "We never heard of this sale."}))
    c2 = make_client(AIClient(provider="anthropic", transport=bad_transport))
    c2.post("/api/run", json={"source": "demo"})
    out2 = c2.post(f"/api/exceptions/{ex}/dispute-draft").json()
    assert out2["source"] == "template" and "note" in out2


# ---------------- printable report ----------------
def test_report_shows_tie_out_decisions_and_escapes_html():
    c = make_client()
    assert c.get("/api/report.html").status_code == 409
    c.post("/api/run", json={"source": "demo"})
    c.post("/api/exceptions/EX-001/decision", json={"decision": "approve", "note": "<script>alert(1)</script>"})
    html = c.get("/api/report.html").text
    assert "Unexplained difference: $0.00" in html and "PO-20261005" in html and "1 of 18 reviewed" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "hash chain intact" in html


# ---------------- PayPal activity CSV ----------------
HEAD = "Date,Time,TimeZone,Name,Type,Status,Currency,Gross,Fee,Net,Transaction ID,Reference Txn ID,Invoice Number\n"


def test_activity_parser_maps_types_and_flips_fee_sign():
    text = HEAD + (
        '10/01/2026,10:00:00,PST,Ada Lovelace,Express Checkout Payment,Completed,USD,"1,000.00",-34.90,965.10,T1,,INV-1\n'
        "10/02/2026,11:30:00,PST,Ada Lovelace,Payment Refund,Completed,USD,-50.00,1.74,-48.26,T2,T1,INV-1\n"
        "10/03/2026,09:00:00,PST,Bob,Hold on Balance for Dispute Investigation,Completed,USD,-99.00,0.00,-99.00,T3,T1,INV-1\n"
        "10/04/2026,09:00:00,PST,,General Withdrawal,Completed,USD,-500.00,0.00,-500.00,T4,,\n"
        "10/04/2026,09:00:00,PST,Eve,Express Checkout Payment,Pending,USD,10.00,-0.84,9.16,T5,,INV-5\n")
    txns, meta = importer.parse_paypal_activity(text)
    by = {t["id"]: t for t in txns}
    assert set(by) == {"T1", "T2", "T3"} and meta["skipped"] == 2
    assert by["T1"]["type"] == "sale" and by["T1"]["gross"] == 100000 and by["T1"]["fee"] == 3490
    assert by["T2"]["type"] == "refund" and by["T2"]["fee"] == -174 and by["T2"]["original_id"] == "T1"
    assert by["T3"]["type"] == "dispute_hold" and by["T1"]["date"] == "2026-10-01T10:00:00"


@pytest.mark.parametrize("text,needle", [
    ("Foo,Bar\n1,2\n", "could not find column"),
    (HEAD + "10/01/2026,10:00:00,PST,A,Express Checkout Payment,Completed,EUR,10.00,-0.5,9.5,T1,,I\n", "Only USD"),
    (HEAD + "10/04/2026,09:00:00,PST,,General Withdrawal,Completed,USD,-5.00,0.00,-5.00,T4,,\n", "no completed payments"),
])
def test_activity_parser_errors_are_plain(text, needle):
    with pytest.raises(importer.ImportProblem) as e:
        importer.parse_paypal_activity(text)
    assert needle in str(e.value)


def demo_as_activity_csv():
    d = datagen.generate_demo(42)
    kinds = {"sale": "Express Checkout Payment", "refund": "Payment Refund", "dispute_hold": "Hold on Balance for Dispute Investigation"}
    lines = [HEAD.strip()]
    for t in d["paypal"]:
        day, clock = t["date"].split("T")
        mm = f"{day[5:7]}/{day[8:10]}/{day[:4]}"
        g, f = t["gross"] / 100, -t["fee"] / 100
        lines.append(f'{mm},{clock},PST,"{t["customer"]}",{kinds[t["type"]]},Completed,USD,{g:.2f},{f:.2f},{g + f:.2f},'
                     f'{t["id"]},{t.get("original_id") or ""},{t.get("invoice_ref") or ""}')
    return "\n".join(lines) + "\n"


def test_upload_with_all_three_files_gives_back_the_demo_result():
    c = make_client()
    ledger, bank = c.get("/api/sample/ledger.csv").text, c.get("/api/sample/bank.csv").text
    r = c.post("/api/run/upload", json={"ledger_csv": ledger, "bank_csv": bank, "paypal": "csv",
                                        "paypal_csv": demo_as_activity_csv()})
    assert r.status_code == 200, r.text
    s = r.json()["summary"]
    base = c.post("/api/run", json={"source": "demo"}).json()["summary"]
    assert s["exception_count"] == 18 and s["by_type"] == base["by_type"] and s["unexplained_residual"] == 0
    c.post("/api/run/upload", json={"ledger_csv": ledger, "bank_csv": bank, "paypal": "csv", "paypal_csv": demo_as_activity_csv()})
    assert "your PayPal activity CSV" in c.get("/api/result").json()["meta"]["paypal"]


def test_upload_paypal_csv_option_needs_the_file():
    c = make_client()
    ledger, bank = c.get("/api/sample/ledger.csv").text, c.get("/api/sample/bank.csv").text
    r = c.post("/api/run/upload", json={"ledger_csv": ledger, "bank_csv": bank, "paypal": "csv"})
    assert r.status_code == 422 and "PayPal activity" in r.json()["detail"]


def test_money_bridge_closes_on_demo_and_uploaded_data():
    from reconpilot import datagen, engine
    r = engine.reconcile(datagen.generate_demo(42))
    b = r["bridge"]
    assert b["closes"] and b["end"] == b["bank"]
    assert b["start"] + sum(s["delta"] for s in b["steps"]) == b["bank"]
    assert {s["leg"] for s in b["steps"]} <= {"books", "bank"}


def test_money_bridge_closes_for_another_seed():
    from reconpilot import datagen, engine
    r = engine.reconcile(datagen.generate_demo(7))
    assert r["bridge"]["closes"] is True
