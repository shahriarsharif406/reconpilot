import json

import httpx
from fastapi.testclient import TestClient

from reconpilot.ai import AIClient
from reconpilot.audit import AuditLog
from reconpilot.paypal_client import PayPalSandbox, normalize
from reconpilot.server import create_app


def make_client(ai=None, paypal=None):
    app = create_app(ai=ai or AIClient(provider="offline"), paypal=paypal or PayPalSandbox("", ""), audit=AuditLog())
    return TestClient(app)


def test_flow_run_review_export_audit():
    c = make_client()
    assert c.get("/api/result").status_code == 409
    r = c.post("/api/run", json={"source": "demo"})
    assert r.status_code == 200 and r.json()["summary"]["exception_count"] == 18
    res = c.get("/api/result").json()
    assert "evidence" not in res["exceptions"][0]
    refund = next(x for x in res["exceptions"] if x["type"] == "UNRECORDED_REFUND")
    d = c.get(f"/api/exceptions/{refund['id']}").json()
    assert d["evidence"]["paypal"] and d["journal"]["lines"]
    ex = c.post(f"/api/exceptions/{refund['id']}/explain").json()
    assert ex["source"] == "rules" and ex["root_cause"] == "customer_refund"
    dec = c.post(f"/api/exceptions/{refund['id']}/decision", json={"decision": "approve", "note": "checked", "actor": "sam"})
    assert dec.json()["status"] == "approved"
    csv_text = c.get("/api/export/journal.csv").text
    assert "Sales Returns" in csv_text and "sam" in csv_text and csv_text.count("\n") == 3
    a = c.get("/api/audit").json()
    assert a["verify"]["ok"] and [e["action"] for e in a["entries"]][-3:] == ["explain", "approve", "export"]


def test_unapproved_items_never_reach_export():
    c = make_client()
    c.post("/api/run", json={"source": "demo"})
    assert c.get("/api/export/journal.csv").text.count("\n") == 1  # header only


def test_sandbox_without_credentials_is_a_clear_error():
    c = make_client()
    r = c.post("/api/run", json={"source": "sandbox"})
    assert r.status_code == 400 and "PAYPAL_CLIENT_ID" in r.json()["detail"]


def test_validation():
    c = make_client()
    c.post("/api/run", json={"source": "demo"})
    assert c.post("/api/exceptions/EX-001/decision", json={"decision": "delete"}).status_code == 422
    assert c.post("/api/exceptions/EX-999/explain").status_code == 404
    assert c.post("/api/run", json={"source": "production"}).status_code == 422


def test_chat_offline_and_ui_served():
    c = make_client()
    c.post("/api/run", json={"source": "demo"})
    a = c.post("/api/chat", json={"question": "Any open disputes?"}).json()
    assert a["cited"] and a["source"] == "rules"
    page = c.get("/")
    assert page.status_code == 200 and "ReconPilot" in page.text


# ---- AI provider path, exercised against a mocked Anthropic endpoint -------------------
def anthropic_mock(payload_text):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"content": [{"type": "text", "text": payload_text}]})
    return httpx.MockTransport(handler), seen


def test_anthropic_explanation_is_validated_and_customer_names_are_redacted(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    good = json.dumps({"summary": "The refund was issued in PayPal but never booked.",
                       "root_cause": "customer_refund", "recommended_action": "Record it.", "confidence": 0.93})
    transport, seen = anthropic_mock("Here you go:\n" + good)
    c = make_client(ai=AIClient(provider="anthropic", transport=transport))
    c.post("/api/run", json={"source": "demo"})
    refund = next(x for x in c.get("/api/result").json()["exceptions"] if x["type"] == "UNRECORDED_REFUND")
    out = c.post(f"/api/exceptions/{refund['id']}/explain").json()
    assert out["source"].startswith("anthropic:") and out["root_cause"] == "customer_refund"
    assert seen["url"] == "https://api.anthropic.com/v1/messages" and seen["key"] == "test-key"
    sent = seen["body"]["messages"][0]["content"]
    from reconpilot.datagen import CUSTOMERS
    assert not any(name in sent for name in CUSTOMERS) and "CUST-" in sent


def test_bad_model_output_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport, _ = anthropic_mock("I think it is a refund, probably.")
    c = make_client(ai=AIClient(provider="anthropic", transport=transport))
    c.post("/api/run", json={"source": "demo"})
    out = c.post("/api/exceptions/EX-001/explain").json()
    assert out["source"] == "rules" and "note" in out


def test_chat_flags_ids_the_model_made_up(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport, _ = anthropic_mock("Look at EX-001 and EX-777.")
    c = make_client(ai=AIClient(provider="anthropic", transport=transport))
    c.post("/api/run", json={"source": "demo"})
    a = c.post("/api/chat", json={"question": "what first?"}).json()
    assert a["cited"] == ["EX-001"] and a["unverified_ids"] == ["EX-777"]


# ---- PayPal sandbox client against a mocked PayPal ---------------------------------------
SAMPLE = {"transaction_details": [
    {"transaction_info": {"transaction_id": "T1", "transaction_event_code": "T0006", "transaction_status": "S",
                          "transaction_initiation_date": "2026-10-03T10:00:00+0000",
                          "transaction_amount": {"currency_code": "USD", "value": "125.00"},
                          "fee_amount": {"currency_code": "USD", "value": "-4.85"}, "invoice_id": "INV-2001"},
     "payer_info": {"email_address": "b@example.com", "payer_name": {"alternate_full_name": "Test Buyer"}}},
    {"transaction_info": {"transaction_id": "T2", "transaction_event_code": "T1107", "transaction_status": "S",
                          "transaction_initiation_date": "2026-10-05T10:00:00+0000",
                          "transaction_amount": {"currency_code": "USD", "value": "-125.00"},
                          "paypal_reference_id": "T1", "paypal_reference_id_type": "TXN"}},
    {"transaction_info": {"transaction_id": "T3", "transaction_event_code": "T0400", "transaction_status": "S",
                          "transaction_initiation_date": "2026-10-06T10:00:00+0000",
                          "transaction_amount": {"currency_code": "USD", "value": "-50.00"}}},
    {"transaction_info": {"transaction_id": "T4", "transaction_event_code": "T0006", "transaction_status": "P",
                          "transaction_initiation_date": "2026-10-06T11:00:00+0000",
                          "transaction_amount": {"currency_code": "USD", "value": "10.00"}}}],
    "total_pages": 1}


def test_normalize_transaction_search_shape():
    txns, skipped = normalize(SAMPLE["transaction_details"])
    assert [t["type"] for t in txns] == ["sale", "refund"]
    assert txns[0]["gross"] == 12500 and txns[0]["fee"] == 485 and txns[0]["invoice_ref"] == "INV-2001"
    assert txns[1]["gross"] == -12500 and txns[1]["original_id"] == "T1"
    assert skipped == {"status": 1, "type": 1}


def test_sandbox_client_oauth_and_paging():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        assert request.url.host == "api-m.sandbox.paypal.com"
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=SAMPLE)
    import datetime as dt
    pp = PayPalSandbox("id", "secret", transport=httpx.MockTransport(handler))
    txns, _ = pp.list_transactions(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc))
    assert len(txns) == 2 and calls[0] == ("POST", "/v1/oauth2/token")


def test_sandbox_run_end_to_end_with_mocked_paypal():
    def handler(request: httpx.Request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json=SAMPLE)
    pp = PayPalSandbox("id", "secret", transport=httpx.MockTransport(handler))
    c = make_client(paypal=pp)
    r = c.post("/api/run", json={"source": "sandbox"})
    assert r.status_code == 200
    meta = c.get("/api/result").json()["meta"]
    assert meta["source"] == "sandbox" and "synthetic" in meta["books"]


def test_chat_context_is_redacted_too(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    transport, seen = anthropic_mock("ok")
    c = make_client(ai=AIClient(provider="anthropic", transport=transport))
    c.post("/api/run", json={"source": "demo"})
    c.post("/api/chat", json={"question": "summary?"})
    from reconpilot.datagen import CUSTOMERS
    sent = seen["body"]["messages"][0]["content"]
    assert not any(name in sent for name in CUSTOMERS)
