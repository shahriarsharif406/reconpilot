"""Create real sales in your PayPal SANDBOX so ReconPilot has live data to read.

Usage:
  export PAYPAL_CLIENT_ID=...  PAYPAL_CLIENT_SECRET=...
  python -m reconpilot.sandbox_seed create --count 8      # creates orders, prints buyer approval links
  # open each link, log in as your sandbox *personal* (buyer) account and approve
  python -m reconpilot.sandbox_seed capture               # captures every approved order
  python -m reconpilot.sandbox_seed refund --count 2      # refunds two captured orders

Sandbox only: the client in paypal_client.py has no production endpoint.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from .paypal_client import PayPalError, PayPalSandbox

STATE = Path(".sandbox_orders.json")
AMOUNTS = ["19.00", "29.00", "49.00", "75.00", "99.00", "125.00", "249.00", "380.00"]


def load() -> list[dict]:
    return json.loads(STATE.read_text()) if STATE.exists() else []


def save(rows: list[dict]) -> None:
    STATE.write_text(json.dumps(rows, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["create", "capture", "refund"])
    ap.add_argument("--count", type=int, default=8)
    a = ap.parse_args()
    pp = PayPalSandbox()
    rows = load()
    try:
        if a.cmd == "create":
            start = 2001 + len(rows)
            for i in range(a.count):
                inv = f"INV-{start + i}"
                o = pp.create_order(random.choice(AMOUNTS), inv)
                link = next((l["href"] for l in o.get("links", []) if l.get("rel") in ("payer-action", "approve")), "")
                rows.append({"order_id": o["id"], "invoice": inv, "status": o.get("status"), "approve": link})
                print(f"{inv}  {o['id']}  open this link as the sandbox buyer:\n  {link}")
            save(rows)
        elif a.cmd == "capture":
            for r in rows:
                if r.get("capture_id"):
                    continue
                st = pp.get_order(r["order_id"]).get("status")
                if st == "APPROVED":
                    cap = pp.capture_order(r["order_id"])
                    r["capture_id"] = cap["purchase_units"][0]["payments"]["captures"][0]["id"]
                    r["status"] = "COMPLETED"
                    print("captured", r["invoice"], r["capture_id"])
                else:
                    print("skipped", r["invoice"], "status:", st)
            save(rows)
        else:
            todo = [r for r in rows if r.get("capture_id") and not r.get("refunded")][: a.count]
            for r in todo:
                pp.refund_capture(r["capture_id"])
                r["refunded"] = True
                print("refunded", r["invoice"])
            save(rows)
    except PayPalError as exc:
        raise SystemExit(f"PayPal error: {exc}")


if __name__ == "__main__":
    main()
