"""PayPal **sandbox** client (OAuth2, Transaction Search, Orders v2, refunds).

Safety: this module only ever talks to https://api-m.sandbox.paypal.com, or to a local PayPal
simulator on 127.0.0.1 / localhost (see paypal_sim.py) when PAYPAL_BASE_URL says so. There is no
switch for the live environment. Credentials come from environment variables.
"""
from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import httpx

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


class PayPalError(RuntimeError):
    pass


def safe_base(url: str | None) -> str:
    """Sandbox host, or a local simulator. Anything else (including live PayPal) is refused."""
    if not url:
        return SANDBOX_BASE
    u = httpx.URL(url)
    if url.rstrip("/") == SANDBOX_BASE or (u.scheme == "http" and u.host in ("127.0.0.1", "localhost")):
        return url.rstrip("/")
    raise PayPalError("PAYPAL_BASE_URL must be the PayPal sandbox or a local simulator (127.0.0.1 / localhost).")


def _cents(value) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1")))


def _fmt(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def classify(info: dict) -> str | None:
    """Map a PayPal transaction_event_code to the engine's transaction types.

    Best-effort mapping (T00xx payments, T11xx refunds, T12xx disputes/chargebacks,
    T03/T04xx funding/withdrawals are skipped). Verify against your own sandbox data.
    """
    code = info.get("transaction_event_code", "") or ""
    amt = _cents(info.get("transaction_amount", {}).get("value", "0"))
    if code.startswith("T12"):
        return "dispute_hold" if amt < 0 else None
    if code.startswith("T11"):
        return "refund" if amt < 0 else None
    if code.startswith("T00") and amt > 0:
        return "sale"
    return None


def normalize(raw_details: list[dict]) -> tuple[list[dict], dict]:
    """Convert Transaction Search `transaction_details` to engine transactions."""
    out, skipped = [], {"status": 0, "type": 0}
    for d in raw_details:
        info = d.get("transaction_info", {})
        if info.get("transaction_status") not in (None, "S"):
            skipped["status"] += 1
            continue
        kind = classify(info)
        if kind is None:
            skipped["type"] += 1
            continue
        payer = d.get("payer_info", {})
        name = (payer.get("payer_name") or {}).get("alternate_full_name") or payer.get("email_address")
        fee = -_cents(info.get("fee_amount", {}).get("value", "0"))
        out.append({
            "id": info["transaction_id"],
            "date": (info.get("transaction_initiation_date") or "")[:19],
            "type": kind,
            "gross": _cents(info["transaction_amount"]["value"]),
            "fee": fee,
            "customer": name,
            "invoice_ref": info.get("invoice_id") or info.get("custom_field"),
            "original_id": info.get("paypal_reference_id") if info.get("paypal_reference_id_type") == "TXN" else None,
            "payout_id": None,
        })
    out.sort(key=lambda t: t["date"])
    return out, skipped


class PayPalSandbox:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 transport: httpx.BaseTransport | None = None, base_url: str | None = None):
        self.client_id = client_id or os.getenv("PAYPAL_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("PAYPAL_CLIENT_SECRET", "")
        self._http = httpx.Client(base_url=safe_base(base_url or os.getenv("PAYPAL_BASE_URL")), timeout=30, transport=transport)
        self._token: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _auth(self) -> dict:
        if not self.configured:
            raise PayPalError("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox app credentials).")
        if not self._token:
            r = self._http.post("/v1/oauth2/token", data={"grant_type": "client_credentials"},
                                auth=(self.client_id, self.client_secret))
            if r.status_code != 200:
                raise PayPalError(f"OAuth failed ({r.status_code}): {r.text[:200]}")
            self._token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self._http.request(method, path, headers=self._auth(), **kw)
        if r.status_code >= 400:
            raise PayPalError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # --- reporting -------------------------------------------------------------------
    def list_transactions(self, start: dt.datetime, end: dt.datetime) -> tuple[list[dict], dict]:
        if (end - start).days > 31:
            raise PayPalError("Transaction Search allows at most a 31-day window.")
        details, page, total = [], 1, 1
        while page <= total:
            data = self._req("GET", "/v1/reporting/transactions", params={
                "start_date": _fmt(start), "end_date": _fmt(end), "fields": "all",
                "page_size": 500, "page": page})
            details += data.get("transaction_details", [])
            total = data.get("total_pages", 1)
            page += 1
        return normalize(details)

    # --- seeding helpers (sandbox only) ------------------------------------------------
    def create_order(self, amount: str, invoice_id: str, description: str = "ReconPilot sandbox sale") -> dict:
        body = {"intent": "CAPTURE",
                "purchase_units": [{"invoice_id": invoice_id, "description": description,
                                    "amount": {"currency_code": "USD", "value": amount}}],
                "payment_source": {"paypal": {"experience_context": {
                    "user_action": "PAY_NOW",
                    "return_url": "https://example.com/return", "cancel_url": "https://example.com/cancel"}}}}
        return self._req("POST", "/v2/checkout/orders", json=body)

    def get_order(self, order_id: str) -> dict:
        return self._req("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str) -> dict:
        return self._req("POST", f"/v2/checkout/orders/{order_id}/capture", json={})

    def refund_capture(self, capture_id: str, amount: str | None = None) -> dict:
        body = {"amount": {"currency_code": "USD", "value": amount}} if amount else {}
        return self._req("POST", f"/v2/payments/captures/{capture_id}/refund", json=body)
