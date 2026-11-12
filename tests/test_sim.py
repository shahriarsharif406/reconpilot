"""The PayPal API simulator and the real client talking to each other."""
import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient

from reconpilot.ai import AIClient
from reconpilot.audit import AuditLog
from reconpilot.paypal_client import PayPalError, PayPalSandbox, safe_base
from reconpilot.paypal_sim import SIM_CLIENT_ID, SIM_CLIENT_SECRET, create_sim_app, sim_transport
from reconpilot.server import create_app


def client(seed=42, log=None, secret=SIM_CLIENT_SECRET):
    return PayPalSandbox(SIM_CLIENT_ID, secret, transport=sim_transport(seed, log), base_url="http://127.0.0.1:1")


def window():
    end = dt.datetime.now(dt.timezone.utc)
    return end - dt.timedelta(days=30), end


def test_wrong_secret_is_rejected():
    with pytest.raises(PayPalError, match="OAuth failed"):
        client(secret="nope").list_transactions(*window())


def test_search_skips_noise_and_returns_engine_types():
    txns, skipped = client().list_transactions(*window())
    assert {t["type"] for t in txns} == {"sale", "refund", "dispute_hold"}
    assert skipped == {"status": 1, "type": 1}  # one pending payment, one withdrawal


def test_order_capture_refund_flow_shows_up_in_search():
    log = []
    c = client(log=log)
    o = c.create_order("49.00", "INV-X-1")
    assert o["status"] == "PAYER_ACTION_REQUIRED"
    assert c.get_order(o["id"])["purchase_units"][0]["invoice_id"] == "INV-X-1"
    cap = c.capture_order(o["id"])
    cid = cap["purchase_units"][0]["payments"]["captures"][0]["id"]
    c.refund_capture(cid, "10.00")
    txns, _ = c.list_transactions(*window())
    mine = [t for t in txns if t["invoice_ref"] == "INV-X-1"]
    assert sorted(t["gross"] for t in mine) == [-1000, 4900]
    assert next(t for t in mine if t["type"] == "refund")["original_id"] == cid
    assert log[0] == "POST /v1/oauth2/token -> 200"


def test_double_capture_and_over_refund_are_refused():
    c = client()
    o = c.create_order("20.00", "INV-X-2")
    cid = c.capture_order(o["id"])["purchase_units"][0]["payments"]["captures"][0]["id"]
    with pytest.raises(PayPalError, match="ORDER_ALREADY_CAPTURED"):
        c.capture_order(o["id"])
    with pytest.raises(PayPalError, match="REFUND_AMOUNT_EXCEEDED"):
        c.refund_capture(cid, "25.00")


def test_protocol_rules_paging_auth_and_window():
    app = TestClient(create_sim_app(42))
    assert app.get("/v1/reporting/transactions", params={"start_date": "x", "end_date": "y"}).status_code == 401
    tok = app.post("/v1/oauth2/token", data={"grant_type": "client_credentials"},
                   auth=(SIM_CLIENT_ID, SIM_CLIENT_SECRET)).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    end = dt.datetime.now(dt.timezone.utc)
    f = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S+0000")
    p1 = app.get("/v1/reporting/transactions", headers=h, params={
        "start_date": f(end - dt.timedelta(days=30)), "end_date": f(end), "fields": "all", "page_size": 10}).json()
    assert p1["total_pages"] > 1 and len(p1["transaction_details"]) == 10
    too_long = app.get("/v1/reporting/transactions", headers=h, params={
        "start_date": f(end - dt.timedelta(days=40)), "end_date": f(end)})
    assert too_long.status_code == 400 and too_long.json()["name"] == "DATA_RANGE_TOO_LARGE"


def test_client_refuses_any_host_but_sandbox_or_local_simulator():
    assert safe_base(None).startswith("https://api-m.sandbox.paypal.com")
    assert safe_base("http://127.0.0.1:9000/") == "http://127.0.0.1:9000"
    for bad in ("https://api-m.paypal.com", "http://example.com", "https://127.0.0.1:9000"):
        with pytest.raises(PayPalError):
            safe_base(bad)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_simulator_run_reconciles_end_to_end(seed):
    c = TestClient(create_app(ai=AIClient(provider="offline"), paypal=PayPalSandbox("", ""), audit=AuditLog()))
    assert c.post("/api/run", json={"source": "simulator", "seed": seed}).status_code == 200
    r = c.get("/api/result").json()
    assert r["meta"]["source"] == "simulator" and "not PayPal" in r["meta"]["paypal"]
    assert r["summary"]["unexplained_residual"] == 0 and r["bridge"]["closes"]
    calls = r["meta"]["api_calls"]
    assert calls[0].startswith("POST /v1/oauth2/token") and any("/v1/reporting/transactions" in x for x in calls)
    assert any("/capture" in x for x in calls) and any("/refund" in x for x in calls)
