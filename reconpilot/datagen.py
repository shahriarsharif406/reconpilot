"""Synthetic data for demos and tests. Nothing here is real customer or bank data.

`generate_demo()` builds a month of PayPal activity, then a set of books and a bank
statement for it, with a known set of deliberate defects (returned as `truth`) so the
engine can be tested against ground truth.

`build_books()` is also used in sandbox mode: PayPal transactions come from the real
PayPal sandbox, and the books/bank side is synthesised around them (clearly labelled).
"""
from __future__ import annotations

import datetime as dt
import random
import string

from .engine import find_duplicate_payments

CUSTOMERS = [
    "Aria Patel", "Noor Rahman", "Lucas Meyer", "Mei Tanaka", "Sofia Rossi", "Omar Haddad",
    "Priya Nair", "Ethan Brooks", "Lina Park", "Mateo Silva", "Zara Khan", "Hugo Laurent",
    "Amara Okafor", "Kenji Sato", "Isla Murray", "Tariq Aziz", "Nora Berg", "Diego Torres",
    "Hana Kim", "Felix Wagner", "Maya Singh", "Leo Costa", "Elif Demir", "Jonas Ek",
    "Rhea Das", "Victor Hale", "Anya Volkov", "Samir Ali", "Clara Dubois", "Ravi Menon",
]
PRICES = [1900, 2900, 4900, 7500, 9900, 12500, 19900, 24900, 38000, 52500, 125000]


def fee_for(gross: int) -> int:
    """PayPal-style fee: 3.49% + $0.49 (rounded half up), in cents."""
    return int(gross * 349 / 10000 + 0.5) + 49


def _tid(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(17))


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def _transpose(amount: int) -> int:
    s = str(amount)
    for i in range(len(s) - 1):
        if s[i] != s[i + 1]:
            return int(s[:i] + s[i + 1] + s[i] + s[i + 2:])
    return amount


# ---------------------------------------------------------------------------------------
def synth_paypal(seed: int, start: dt.date, days: int, n_sales: int) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    base = dt.datetime.combine(start, dt.time())
    raw = []
    for _ in range(n_sales):
        when = base + dt.timedelta(days=rng.randrange(days), seconds=rng.randrange(8 * 3600, 22 * 3600))
        raw.append((when, rng.choice(PRICES), rng.choice(CUSTOMERS)))
    raw.sort()
    txns = []
    for i, (when, gross, cust) in enumerate(raw):
        txns.append({"id": _tid(rng), "date": when.isoformat(timespec="seconds"), "type": "sale",
                     "gross": gross, "fee": fee_for(gross), "customer": cust,
                     "invoice_ref": f"INV-{1001 + i}", "original_id": None, "payout_id": None})

    early = [t for t in txns if _d(t["date"]) < start + dt.timedelta(days=days - 12)]
    picks = rng.sample(early, 8)
    refund_src, dispute_src, dup_src = picks[:6], picks[6], picks[7]
    truth = {"UNRECORDED_REFUND_candidates": 6}
    for t in refund_src:
        when = dt.datetime.fromisoformat(t["date"]) + dt.timedelta(days=rng.randint(2, 4), hours=1)
        txns.append({"id": _tid(rng), "date": when.isoformat(timespec="seconds"), "type": "refund",
                     "gross": -t["gross"], "fee": 0, "customer": t["customer"],
                     "invoice_ref": t["invoice_ref"], "original_id": t["id"], "payout_id": None})
    when = dt.datetime.fromisoformat(dispute_src["date"]) + dt.timedelta(days=5, hours=2)
    txns.append({"id": _tid(rng), "date": when.isoformat(timespec="seconds"), "type": "dispute_hold",
                 "gross": -dispute_src["gross"], "fee": 0, "customer": dispute_src["customer"],
                 "invoice_ref": dispute_src["invoice_ref"], "original_id": dispute_src["id"], "payout_id": None})
    when = dt.datetime.fromisoformat(dup_src["date"]) + dt.timedelta(seconds=95)
    txns.append({"id": _tid(rng), "date": when.isoformat(timespec="seconds"), "type": "sale",
                 "gross": dup_src["gross"], "fee": dup_src["fee"], "customer": dup_src["customer"],
                 "invoice_ref": dup_src["invoice_ref"], "original_id": None, "payout_id": None})
    txns.sort(key=lambda t: t["date"])
    truth.update({"DISPUTE_OPEN": 1, "DUPLICATE_PAYMENT": 1})
    return txns, truth


def assign_payouts(txns: list[dict], start: dt.date, end: dt.date) -> list[dict]:
    """Weekly Monday sweeps. Mutates txns (payout_id); returns payout list."""
    paydates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)
                if (start + dt.timedelta(days=i)).weekday() == 0 and start < start + dt.timedelta(days=i) <= end]
    for t in txns:
        nxt = [p for p in paydates if p > _d(t["date"])]
        t["payout_id"] = f"PO-{nxt[0]:%Y%m%d}" if nxt else None
    return [{"id": f"PO-{p:%Y%m%d}", "date": dt.datetime.combine(p, dt.time(6)).isoformat(timespec="seconds")}
            for p in paydates]


def build_books(txns: list[dict], seed: int, start: dt.date, end: dt.date, inject: bool = True) -> dict:
    """Build ledger + payouts + bank statement around PayPal transactions."""
    rng = random.Random(seed + 1)
    txns = [dict(t) for t in txns]
    payouts = assign_payouts(txns, start, end)
    dups = find_duplicate_payments(txns)
    by_id = {t["id"]: t for t in txns}

    entries: list[dict] = []
    entry_of: dict[str, dict] = {}
    for t in txns:
        if t["type"] == "sale" and t["id"] not in dups:
            e = {"date": t["date"][:10] + "T00:00:00", "type": "sale",
                 "ref": t.get("invoice_ref") or f"PP-{t['id'][-8:]}", "amount": t["gross"],
                 "fee_booked": t["fee"], "customer": t.get("customer"), "memo": "PayPal sale"}
            entries.append(e)
            entry_of[t["id"]] = e
        elif t["type"] == "refund":
            orig = by_id.get(t.get("original_id"))
            e = {"date": t["date"][:10] + "T00:00:00", "type": "refund",
                 "ref": (orig or t).get("invoice_ref") or f"PP-{t['id'][-8:]}", "amount": t["gross"],
                 "fee_booked": 0, "customer": t.get("customer"), "memo": "PayPal refund"}
            entries.append(e)
            entry_of[t["id"]] = e

    truth: dict[str, int] = {}
    if inject:
        touched = {t.get("original_id") for t in txns if t["type"] in ("refund", "dispute_hold")}
        touched |= set(dups) | set(dups.values())
        eligible = [t for t in txns if t["type"] == "sale" and t.get("payout_id")
                    and t["id"] not in touched and t["id"] in entry_of]
        rng.shuffle(eligible)
        plan = [("MISSING_IN_LEDGER", 3), ("TRANSPOSE", 1), ("NET_FOR_GROSS", 2), ("LEDGER_DUP", 2),
                ("NEAR_REF", 2), ("FEE_VARIANCE", 2)]
        existing_refs = {e["ref"] for e in entries}
        extra: list[dict] = []
        drop: set[int] = set()
        for kind, count in plan:
            done = 0
            while done < count and eligible:
                t = eligible.pop()
                e = entry_of[t["id"]]
                if kind == "TRANSPOSE" and _transpose(t["gross"]) == t["gross"]:
                    continue
                if kind == "MISSING_IN_LEDGER":
                    drop.add(id(e))
                elif kind == "TRANSPOSE":
                    e["amount"] = _transpose(t["gross"])
                elif kind == "NET_FOR_GROSS":
                    e["amount"], e["fee_booked"] = t["gross"] - t["fee"], 0
                elif kind == "LEDGER_DUP":
                    dd = dict(e)
                    dd["date"] = (dt.datetime.fromisoformat(e["date"]) + dt.timedelta(days=1)).isoformat(timespec="seconds")
                    dd["memo"] = "PayPal sale (re-keyed)"
                    extra.append(dd)
                elif kind == "NEAR_REF":
                    alt = e["ref"].replace("-", "") if done == 0 else e["ref"][:-2] + e["ref"][-1] + e["ref"][-2]
                    if alt in existing_refs or alt == e["ref"]:
                        alt = e["ref"] + "A"
                    e["ref"] = alt
                    existing_refs.add(alt)
                elif kind == "FEE_VARIANCE":
                    e["fee_booked"] = 0 if done == 0 else t["fee"] + 49
                done += 1
            truth_key = {"TRANSPOSE": "AMOUNT_MISMATCH", "NET_FOR_GROSS": "AMOUNT_MISMATCH",
                         "LEDGER_DUP": "DUPLICATE_LEDGER", "NEAR_REF": "REF_MISMATCH"}.get(kind, kind)
            truth[truth_key] = truth.get(truth_key, 0) + done
        entries = [e for e in entries if id(e) not in drop] + extra
        # a customer paid by bank transfer but the books say PayPal
        entries.append({"date": (start + dt.timedelta(days=14)).isoformat() + "T00:00:00", "type": "sale",
                        "ref": "INV-9001", "amount": 18500, "fee_booked": 0, "customer": "Walk-in (bank transfer)",
                        "memo": "Recorded as PayPal in error"})
        truth["MISSING_IN_PAYPAL"] = 1
        # two refunds were never booked
        refund_entries = [e for e in entries if e["type"] == "refund"]
        for e in refund_entries[:2]:
            entries.remove(e)
        truth["UNRECORDED_REFUND"] = min(2, len(refund_entries))

    entries.sort(key=lambda e: (e["date"], e["ref"]))
    for i, e in enumerate(entries, 1):
        e["id"] = f"L-{i:04d}"

    bank = []
    for p in payouts:
        net = sum(t["gross"] - t["fee"] for t in txns if t.get("payout_id") == p["id"])
        if net == 0 and not any(t.get("payout_id") == p["id"] for t in txns):
            continue
        bank.append({"date": (dt.datetime.fromisoformat(p["date"]) + dt.timedelta(days=1)).isoformat(timespec="seconds"),
                     "amount": net, "memo": f"PAYPAL TRANSFER {p['id']}"})
    if inject and len(bank) > 1:
        bank[1]["amount"] -= 3750
        truth["BANK_VARIANCE"] = 1
    for i, b in enumerate(bank, 1):
        b["id"] = f"B-{i:03d}"
    payouts = [p for p in payouts if any(t.get("payout_id") == p["id"] for t in txns)]
    return {"paypal": txns, "ledger": entries, "bank": bank, "payouts": payouts, "truth": truth}


def generate_demo(seed: int = 42) -> dict:
    start, days = dt.date(2026, 10, 1), 31
    end = start + dt.timedelta(days=days - 1)
    txns, t1 = synth_paypal(seed, start, days, 96)
    books = build_books(txns, seed, start, end, inject=True)
    truth = {**books.pop("truth"), "DISPUTE_OPEN": t1["DISPUTE_OPEN"], "DUPLICATE_PAYMENT": t1["DUPLICATE_PAYMENT"]}
    books.update({"period": {"start": start.isoformat(), "end": end.isoformat()}, "source": "demo", "truth": truth})
    return books
