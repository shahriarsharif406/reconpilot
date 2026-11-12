"""A local PayPal API **simulator**. It is not PayPal and holds no real data.

It exists so the real client in `paypal_client.py` can be exercised end to end without PayPal
sandbox credentials: OAuth2 token, Transaction Search (paged, 31-day limit, bearer auth), Orders v2
(create, get, capture) and capture refunds, with JSON shaped like PayPal's documented payloads.

Two ways to use it:
  * in process  - `sim_transport()` returns an httpx transport; every call still goes through the
                  client's real request building, auth headers, paging and response parsing.
  * over HTTP   - `python -m reconpilot.paypal_sim --port 9000`, then set
                  PAYPAL_BASE_URL=http://127.0.0.1:9000 PAYPAL_CLIENT_ID=sim-client PAYPAL_CLIENT_SECRET=sim-secret

What it cannot tell you: whether real PayPal behaves like this. It mirrors the documentation as
understood by the author; the live sandbox remains the real test.
"""
from __future__ import annotations

import base64
import datetime as dt
import itertools
import secrets

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import datagen

SIM_CLIENT_ID = "sim-client"
SIM_CLIENT_SECRET = "sim-secret"
PAGE_MAX = 500


def _val(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _stamp(s: str) -> str:
    return dt.datetime.fromisoformat(s).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _detail(t: dict) -> dict:
    """Engine-shaped transaction -> Transaction Search `transaction_details` entry."""
    code = {"sale": "T0006", "refund": "T1107", "dispute_hold": "T1201"}[t["type"]]
    info = {"transaction_id": t["id"], "transaction_event_code": code, "transaction_status": "S",
            "transaction_initiation_date": _stamp(t["date"]),
            "transaction_amount": {"currency_code": "USD", "value": _val(t["gross"])},
            "fee_amount": {"currency_code": "USD", "value": _val(-t["fee"])},
            "invoice_id": t.get("invoice_ref")}
    if t.get("original_id"):
        info.update({"paypal_reference_id": t["original_id"], "paypal_reference_id_type": "TXN"})
    name = t.get("customer") or "Customer"
    return {"transaction_info": info,
            "payer_info": {"email_address": name.lower().replace(" ", ".") + "@example.test",
                           "payer_name": {"alternate_full_name": name}}}


def _noise(now: dt.datetime) -> list[dict]:
    """Rows a real account also has and the client must skip: a withdrawal and a pending payment."""
    d = _stamp((now - dt.timedelta(days=3)).isoformat(timespec="seconds"))
    return [
        {"transaction_info": {"transaction_id": "SIMWITHDRAW0001", "transaction_event_code": "T0400",
                              "transaction_status": "S", "transaction_initiation_date": d,
                              "transaction_amount": {"currency_code": "USD", "value": "-500.00"}}, "payer_info": {}},
        {"transaction_info": {"transaction_id": "SIMPENDING0001", "transaction_event_code": "T0006",
                              "transaction_status": "P", "transaction_initiation_date": d,
                              "transaction_amount": {"currency_code": "USD", "value": "42.00"},
                              "fee_amount": {"currency_code": "USD", "value": "-1.95"}}, "payer_info": {}},
    ]


def create_sim_app(seed: int = 42, now: dt.datetime | None = None) -> FastAPI:
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(tzinfo=None, microsecond=0)
    start = (now - dt.timedelta(days=29)).date()
    txns, _ = datagen.synth_paypal(seed, start, 28, 60)
    store = {"details": [_detail(t) for t in txns] + _noise(now), "orders": {}, "captures": {}, "tokens": set()}
    counter = itertools.count(1)
    app = FastAPI(title="PayPal API simulator (not PayPal)", docs_url=None, redoc_url=None)

    def err(status: int, name: str, message: str) -> JSONResponse:
        return JSONResponse({"name": name, "message": message, "debug_id": secrets.token_hex(6)}, status_code=status)

    def authed(request: Request) -> JSONResponse | None:
        h = request.headers.get("authorization", "")
        if not h.startswith("Bearer ") or h[7:] not in store["tokens"]:
            return err(401, "AUTHENTICATION_FAILURE", "Authentication failed due to invalid authentication credentials.")
        return None

    @app.post("/v1/oauth2/token")
    async def token(request: Request):
        h = request.headers.get("authorization", "")
        try:
            cid, _, sec = base64.b64decode(h.removeprefix("Basic ")).decode().partition(":")
        except Exception:
            cid = sec = ""
        form = (await request.body()).decode()
        if (cid, sec) != (SIM_CLIENT_ID, SIM_CLIENT_SECRET) or "grant_type=client_credentials" not in form:
            return err(401, "invalid_client", "Client Authentication failed")
        tok = "SIM-" + secrets.token_urlsafe(24)
        store["tokens"].add(tok)
        return {"scope": "https://uri.paypal.com/services/reporting/search/read", "access_token": tok,
                "token_type": "Bearer", "app_id": "APP-SIMULATOR", "expires_in": 32400, "nonce": secrets.token_hex(8)}

    @app.get("/v1/reporting/transactions")
    def transactions(request: Request, start_date: str, end_date: str, page: int = 1, page_size: int = 100,
                     fields: str = "transaction_info"):
        if (bad := authed(request)):
            return bad
        try:
            a = dt.datetime.strptime(start_date, "%Y-%m-%dT%H:%M:%S%z")
            b = dt.datetime.strptime(end_date, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return err(400, "INVALID_REQUEST", "Dates must look like 2026-10-01T00:00:00+0000.")
        if (b - a).days > 31:
            return err(400, "DATA_RANGE_TOO_LARGE", "Date range is larger than 31 days.")
        if not 1 <= page_size <= PAGE_MAX:
            return err(400, "INVALID_REQUEST", f"page_size must be between 1 and {PAGE_MAX}.")
        inside = [d for d in store["details"]
                  if a <= dt.datetime.strptime(d["transaction_info"]["transaction_initiation_date"], "%Y-%m-%dT%H:%M:%S%z") <= b]
        pages = max(1, -(-len(inside) // page_size))
        chunk = inside[(page - 1) * page_size: page * page_size]
        if fields != "all":
            chunk = [{"transaction_info": d["transaction_info"]} for d in chunk]
        return {"transaction_details": chunk, "account_number": "SIMULATOR", "page": page, "total_items": len(inside),
                "total_pages": pages, "start_date": start_date, "end_date": end_date,
                "last_refreshed_datetime": _stamp(now.isoformat())}

    # --- Orders v2 -----------------------------------------------------------------------
    @app.post("/v2/checkout/orders", status_code=201)
    async def create_order(request: Request):
        if (bad := authed(request)):
            return bad
        body = await request.json()
        try:
            unit = body["purchase_units"][0]
            value, inv = unit["amount"]["value"], unit.get("invoice_id")
            float(value)
        except (KeyError, IndexError, ValueError, TypeError):
            return err(422, "INVALID_REQUEST", "purchase_units[0].amount.value is required.")
        oid = "SIMORDER" + str(next(counter)).zfill(6)
        store["orders"][oid] = {"id": oid, "status": "PAYER_ACTION_REQUIRED", "value": value, "invoice_id": inv}
        return {"id": oid, "status": "PAYER_ACTION_REQUIRED", "links": [
            {"href": f"/v2/checkout/orders/{oid}", "rel": "self", "method": "GET"},
            {"href": f"https://simulator.invalid/approve/{oid}", "rel": "payer-action", "method": "GET"}]}

    @app.get("/v2/checkout/orders/{oid}")
    def get_order(oid: str, request: Request):
        if (bad := authed(request)):
            return bad
        o = store["orders"].get(oid)
        if not o:
            return err(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
        return {"id": oid, "status": o["status"], "intent": "CAPTURE",
                "purchase_units": [{"invoice_id": o["invoice_id"], "amount": {"currency_code": "USD", "value": o["value"]}}]}

    @app.post("/v2/checkout/orders/{oid}/capture", status_code=201)
    async def capture(oid: str, request: Request):
        if (bad := authed(request)):
            return bad
        o = store["orders"].get(oid)
        if not o:
            return err(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
        if o["status"] == "COMPLETED":
            return err(422, "ORDER_ALREADY_CAPTURED", "Order already captured.")
        cents = round(float(o["value"]) * 100)
        fee = datagen.fee_for(cents)
        tid = "SIMCAP" + secrets.token_hex(6).upper()
        o["status"] = "COMPLETED"
        stamp = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()
        sale = {"id": tid, "date": stamp, "type": "sale", "gross": cents, "fee": fee, "customer": "Sandbox Buyer",
                "invoice_ref": o["invoice_id"], "original_id": None}
        store["details"].append(_detail(sale))
        store["captures"][tid] = {"gross": cents, "invoice": o["invoice_id"], "refunded": 0}
        return {"id": oid, "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
            {"id": tid, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": o["value"]},
             "seller_receivable_breakdown": {"paypal_fee": {"currency_code": "USD", "value": _val(fee)}}}]}}]}

    @app.post("/v2/payments/captures/{cid}/refund", status_code=201)
    async def refund(cid: str, request: Request):
        if (bad := authed(request)):
            return bad
        c = store["captures"].get(cid)
        if not c:
            return err(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
        raw = await request.body()
        body = (await request.json()) if raw.strip() else {}
        cents = round(float(body["amount"]["value"]) * 100) if body.get("amount") else c["gross"] - c["refunded"]
        if cents <= 0 or c["refunded"] + cents > c["gross"]:
            return err(422, "REFUND_AMOUNT_EXCEEDED", "Refund amount exceeds the captured amount.")
        c["refunded"] += cents
        rid = "SIMREF" + secrets.token_hex(6).upper()
        stamp = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()
        store["details"].append(_detail({"id": rid, "date": stamp, "type": "refund", "gross": -cents, "fee": 0,
                                         "customer": "Sandbox Buyer", "invoice_ref": c["invoice"], "original_id": cid}))
        return {"id": rid, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": _val(cents)}}

    return app


def sim_transport(seed: int = 42, log: list[str] | None = None) -> httpx.BaseTransport:
    """Sync httpx transport that serves the simulator in process.

    `log` collects "METHOD path -> status" lines, so a UI can show which API calls really happened."""
    from fastapi.testclient import TestClient

    inner = TestClient(create_sim_app(seed), base_url="http://paypal-simulator.invalid")

    def handler(request: httpx.Request) -> httpx.Response:
        r = inner.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers),
                          content=request.content)
        if log is not None:
            log.append(f"{request.method} {request.url.path} -> {r.status_code}")
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)

    return httpx.MockTransport(handler)


def main() -> None:
    import argparse

    import uvicorn
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    uvicorn.run(create_sim_app(a.seed), host="127.0.0.1", port=a.port)


if __name__ == "__main__":
    main()
