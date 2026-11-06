"""Three-way reconciliation engine: PayPal  <->  books (ledger)  <->  bank.

Pure, deterministic and side-effect free. All money is integer cents.
The AI layer never changes anything computed here; it only explains it.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

COA = {
    "1000": "Bank",
    "1100": "PayPal Clearing",
    "1200": "Accounts Receivable",
    "1300": "Disputed Funds Receivable",
    "4000": "Sales Revenue",
    "4100": "Sales Returns & Refunds",
    "5100": "PayPal Processing Fees",
    "5200": "Bank Charges",
}

TITLES = {
    "MISSING_IN_LEDGER": "Sale not recorded in books",
    "MISSING_IN_PAYPAL": "Book entry with no PayPal payment",
    "AMOUNT_MISMATCH": "Amount differs between PayPal and books",
    "FEE_VARIANCE": "Processing fee booked incorrectly",
    "DUPLICATE_LEDGER": "Duplicate entry in books",
    "DUPLICATE_PAYMENT": "Possible duplicate customer charge",
    "REF_MISMATCH": "Invoice reference mismatch",
    "UNRECORDED_REFUND": "Refund not recorded in books",
    "DISPUTE_OPEN": "Open dispute: funds held",
    "BANK_VARIANCE": "Bank deposit differs from PayPal payout",
    "BANK_MISSING_DEPOSIT": "Payout missing from bank",
    "UNKNOWN_DEPOSIT": "Bank deposit with no matching payout",
}

HIGH_TYPES = {"DISPUTE_OPEN", "BANK_MISSING_DEPOSIT", "DUPLICATE_PAYMENT"}
MEDIUM_TYPES = {"BANK_VARIANCE", "AMOUNT_MISMATCH", "MISSING_IN_LEDGER",
                "MISSING_IN_PAYPAL", "UNRECORDED_REFUND", "UNKNOWN_DEPOSIT"}


def money(cents: int | None) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def norm_ref(ref) -> str:
    return (ref or "").strip().upper()


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance (insert/delete/substitute/transpose)."""
    a, b = a.upper(), b.upper()
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def _dt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def find_duplicate_payments(txns: list[dict], window_s: int = 600) -> dict[str, str]:
    """Return {duplicate_txn_id: original_txn_id} for same-customer, same-amount
    sales that happen within `window_s` seconds of each other."""
    sales = sorted((t for t in txns if t["type"] == "sale"), key=lambda t: t["date"])
    last: dict[tuple, dict] = {}
    out: dict[str, str] = {}
    for t in sales:
        key = (t.get("customer"), t["gross"])
        prev = last.get(key)
        if prev and (_dt(t["date"]) - _dt(prev["date"])).total_seconds() <= window_s:
            out[t["id"]] = prev["id"]
        else:
            last[key] = t
    return out


def journal(desc: str, *lines) -> dict:
    out = [{"account": c, "name": COA[c], "debit": d, "credit": cr} for c, d, cr in lines]
    assert sum(x["debit"] for x in out) == sum(x["credit"] for x in out), "unbalanced journal"
    return {"description": desc, "lines": out}


def _severity(etype: str, amount: int) -> str:
    sev = "high" if etype in HIGH_TYPES else "medium" if etype in MEDIUM_TYPES else "low"
    if amount >= 100_000:
        sev = "high"
    return sev


def _risk(sev: str, amount: int) -> int:
    return {"high": 60, "medium": 35, "low": 15}[sev] + min(40, int(amount / 2500))


def reconcile(data: dict) -> dict:
    pp = [dict(t) for t in data["paypal"]]
    ledger = [dict(e) for e in data["ledger"]]
    bank = [dict(b) for b in data["bank"]]
    payout_dates = {p["id"]: p["date"] for p in data.get("payouts", [])}
    pp_by_id = {t["id"]: t for t in pp}

    exc: list[dict] = []
    ledger_net: dict[str, int] = {}
    matched: set[str] = set()
    used: set[str] = set()
    matched_refs: set[str] = set()

    def add(etype, *, amount, txns=(), entries=(), lines=(), impact=None, payout=None,
            confidence=0.95, subtype=None, reason="", action="", jrn=None):
        exc.append({
            "type": etype, "title": TITLES[etype], "subtype": subtype,
            "amount": abs(int(amount)), "impact": impact, "payout_id": payout,
            "confidence": confidence, "reason": reason, "action": action,
            "journal": jrn, "status": "open",
            "evidence": {"paypal": list(txns), "ledger": list(entries), "bank": list(lines)},
            "txn_ids": [t["id"] for t in txns], "entry_ids": [e["id"] for e in entries],
        })

    def lnet(e):
        return e["amount"] - e.get("fee_booked", 0)

    sales = [t for t in pp if t["type"] == "sale"]
    by_ref: dict[str, list[dict]] = defaultdict(list)
    for e in ledger:
        if e["type"] == "sale":
            by_ref[norm_ref(e["ref"])].append(e)

    # --- 1. possible duplicate charges (PayPal side) ---------------------------------
    dups = find_duplicate_payments(pp)
    for dup_id, orig_id in dups.items():
        d, o = pp_by_id[dup_id], pp_by_id[orig_id]
        ledger_net[dup_id] = 0
        add("DUPLICATE_PAYMENT", amount=d["gross"], txns=[o, d], impact=-(d["gross"] - d["fee"]),
            payout=d.get("payout_id"), confidence=0.9,
            reason=(f"{d.get('customer')} was charged {money(d['gross'])} twice within "
                    f"{int((_dt(d['date']) - _dt(o['date'])).total_seconds())} seconds "
                    f"(transactions …{o['id'][-6:]} and …{d['id'][-6:]}). Only one sale is recorded in the books."),
            action="Refund the second charge in PayPal, then record the refund.")

    # --- 2. sales vs ledger: pass 1 = exact reference ---------------------------------
    unresolved: list[dict] = []
    pp_refs = {norm_ref(t.get("invoice_ref")) for t in sales if t.get("invoice_ref")}
    for t in sales:
        if t["id"] in dups:
            continue
        ref = norm_ref(t.get("invoice_ref"))
        cands = [e for e in by_ref.get(ref, []) if e["id"] not in used] if ref else []
        if cands:
            e = next((c for c in cands if c["amount"] == t["gross"]), cands[0])
            used.add(e["id"])
            if "fee_booked" not in e:  # books that do not track fees (e.g. an uploaded CSV without a fee column)
                e["fee_booked"] = t["fee"]
            matched_refs.add(ref)
            ledger_net[t["id"]] = lnet(e)
            if e["amount"] == t["gross"]:
                if e.get("fee_booked", 0) != t["fee"]:
                    diff = t["fee"] - e.get("fee_booked", 0)
                    jr = (journal(f"Correct fee on {t['invoice_ref']}", ("5100", diff, 0), ("1100", 0, diff))
                          if diff > 0 else
                          journal(f"Correct fee on {t['invoice_ref']}", ("1100", -diff, 0), ("5100", 0, -diff)))
                    add("FEE_VARIANCE", amount=abs(diff), txns=[t], entries=[e], impact=t["fee"] - e.get("fee_booked", 0),
                        payout=t.get("payout_id"), confidence=0.97,
                        reason=(f"PayPal charged a fee of {money(t['fee'])} on {t['invoice_ref']} but the books show "
                                f"{money(e.get('fee_booked', 0))}."),
                        action="Post the fee difference to PayPal Processing Fees.", jrn=jr)
                else:
                    matched.add(t["id"])
            else:
                diff = t["gross"] - e["amount"]
                digits_same = sorted(str(abs(t["gross"]))) == sorted(str(abs(e["amount"])))
                if digits_same:
                    sub, why = "transposed_digits", "The digits look transposed (a typing slip)."
                elif e["amount"] == t["gross"] - t["fee"]:
                    sub, why = "net_instead_of_gross", "The books recorded the net amount (after PayPal's fee) instead of the gross sale."
                else:
                    sub, why = "other", "The difference does not match a known pattern, so a person should review it."
                jr = (journal(f"Adjust {t['invoice_ref']} to PayPal gross", ("1100", diff, 0), ("4000", 0, diff))
                      if diff > 0 else
                      journal(f"Adjust {t['invoice_ref']} to PayPal gross", ("4000", -diff, 0), ("1100", 0, -diff)))
                add("AMOUNT_MISMATCH", amount=abs(diff), subtype=sub, txns=[t], entries=[e],
                    impact=lnet(e) - (t["gross"] - t["fee"]), payout=t.get("payout_id"),
                    confidence=0.97 if sub != "other" else 0.6,
                    reason=(f"{t['invoice_ref']}: PayPal shows {money(t['gross'])}, books show {money(e['amount'])} "
                            f"(difference {money(diff)}). {why}"),
                    action="Post an adjusting entry to bring the books to the PayPal amount after checking the invoice.",
                    jrn=jr)
            continue

        unresolved.append(t)

    # --- 2b. second pass: fuzzy / probable matches for sales with no exact reference ----
    for t in unresolved:
        ref = norm_ref(t.get("invoice_ref"))
        # no exact reference match -> fuzzy / probable match
        cand, sub, conf = None, None, 0.0
        if ref:
            for e in ledger:
                if (e["type"] == "sale" and e["id"] not in used and e["amount"] == t["gross"]
                        and norm_ref(e["ref"]) not in pp_refs
                        and edit_distance(norm_ref(e["ref"]), ref) <= 1):
                    cand, sub, conf = e, "near_reference", 0.92
                    break
        if cand is None:
            for e in ledger:
                if (e["type"] == "sale" and e["id"] not in used and e["amount"] == t["gross"]
                        and norm_ref(e["ref"]) not in pp_refs
                        and e.get("customer") == t.get("customer")
                        and abs((_dt(e["date"]) - _dt(t["date"])).days) <= 3):
                    cand, sub, conf = e, "probable", 0.6
                    break
        if cand is not None:
            used.add(cand["id"])
            if "fee_booked" not in cand:
                cand["fee_booked"] = t["fee"]
            matched_refs.add(norm_ref(cand["ref"]))
            ledger_net[t["id"]] = lnet(cand)
            add("REF_MISMATCH", subtype=sub, amount=t["gross"], txns=[t], entries=[cand], impact=0,
                payout=t.get("payout_id"), confidence=conf,
                reason=(f"PayPal transaction {t['id']} carries reference {t.get('invoice_ref') or '(none)'}, "
                        f"while the matching-looking book entry {cand['id']} uses {cand['ref']}; amount "
                        f"{money(t['gross'])} is identical."),
                action=f"Confirm they are the same sale, then correct the reference in the books to {t.get('invoice_ref') or 'the PayPal reference'}.")
        else:
            ledger_net[t["id"]] = 0
            net = t["gross"] - t["fee"]
            add("MISSING_IN_LEDGER", amount=t["gross"], txns=[t], impact=-net, payout=t.get("payout_id"),
                confidence=0.95,
                reason=(f"PayPal received {money(t['gross'])} from {t.get('customer')} ({t.get('invoice_ref') or 'no reference'}) "
                        f"on {t['date'][:10]}, but no matching entry exists in the books."),
                action="Record the sale, with the PayPal fee as an expense.",
                jrn=journal(f"Record missing sale {t.get('invoice_ref') or t['id']}",
                            ("1100", net, 0), ("5100", t["fee"], 0), ("4000", 0, t["gross"])))

    # --- 3. refunds -------------------------------------------------------------------
    for r in (t for t in pp if t["type"] == "refund"):
        orig = pp_by_id.get(r.get("original_id"))
        ref = norm_ref(orig["invoice_ref"] if orig else r.get("invoice_ref"))
        entry = next((e for e in ledger if e["type"] == "refund" and e["id"] not in used
                      and norm_ref(e["ref"]) == ref and e["amount"] == r["gross"]), None)
        if entry:
            used.add(entry["id"])
            ledger_net[r["id"]] = entry["amount"]
            matched.add(r["id"])
        else:
            ledger_net[r["id"]] = 0
            amt = -r["gross"]
            add("UNRECORDED_REFUND", amount=amt, txns=[x for x in (orig, r) if x], impact=-(r["gross"] - r["fee"]),
                payout=r.get("payout_id"), confidence=0.96,
                reason=(f"PayPal refunded {money(amt)} to {r.get('customer')} on {r['date'][:10]} "
                        f"(original sale {orig['invoice_ref'] if orig else 'unknown'}), but the books have no refund entry."),
                action="Record the refund against Sales Returns.",
                jrn=journal(f"Record refund of {orig['invoice_ref'] if orig else r['id']}",
                            ("4100", amt, 0), ("1100", 0, amt)))

    # --- 4. disputes ------------------------------------------------------------------
    for h in (t for t in pp if t["type"] == "dispute_hold"):
        ledger_net[h["id"]] = 0
        amt = -h["gross"]
        add("DISPUTE_OPEN", amount=amt, txns=[h], impact=-(h["gross"] - h["fee"]), payout=h.get("payout_id"),
            confidence=0.98,
            reason=(f"PayPal is holding {money(amt)} for a dispute on {h['date'][:10]} "
                    f"({h.get('invoice_ref') or h['id']}). Until it is resolved those funds are not available."),
            action="Respond to the dispute with evidence in PayPal, and move the held amount out of cash.",
            jrn=journal(f"Reclass disputed funds {h.get('invoice_ref') or h['id']}",
                        ("1300", amt, 0), ("1100", 0, amt)))

    # --- 5. leftover ledger entries ---------------------------------------------------
    for e in ledger:
        if e["id"] in used:
            continue
        if e["type"] == "sale" and norm_ref(e["ref"]) in matched_refs:
            add("DUPLICATE_LEDGER", amount=e["amount"], entries=[e], impact=None, confidence=0.95,
                reason=(f"Book entry {e['id']} repeats reference {e['ref']} for {money(e['amount'])}; "
                        f"the same sale is already recorded once."),
                action="Reverse the duplicate entry.",
                jrn=journal(f"Reverse duplicate {e['ref']}", ("4000", e["amount"], 0), ("1200", 0, e["amount"])))
        else:
            add("MISSING_IN_PAYPAL", amount=abs(e["amount"]), entries=[e], impact=None,
                confidence=0.8,
                reason=(f"Book entry {e['id']} ({e['ref']}, {money(e['amount'])}) has no PayPal transaction. "
                        f"It may have been paid by another channel or recorded in error."),
                action="Check how the customer actually paid; reverse the entry if it was not a PayPal sale.")

    # --- 6. payouts vs bank (the third leg) ------------------------------------------
    by_payout: dict[str, list[dict]] = defaultdict(list)
    pending = []
    for t in pp:
        (by_payout[t["payout_id"]] if t.get("payout_id") else pending).append(t)

    free_bank = list(bank)
    payouts_out = []
    for pid in sorted(by_payout, key=lambda p: payout_dates.get(p, p)):
        txs = by_payout[pid]
        expected = sum(t["gross"] - t["fee"] for t in txs)
        implied = sum(ledger_net.get(t["id"], 0) for t in txs)
        line = next((b for b in free_bank if pid in (b.get("memo") or "")), None)
        if line is None and pid in payout_dates:
            pd_ = dt.date.fromisoformat(payout_dates[pid][:10])
            near = [b for b in free_bank
                    if 0 <= (dt.date.fromisoformat(b["date"][:10]) - pd_).days <= 4]
            line = min(near, key=lambda b: abs(b["amount"] - expected), default=None)
        if line:
            free_bank.remove(line)
        bank_amt = line["amount"] if line else None
        gap = implied - expected
        explained = sum(x["impact"] for x in exc if x["payout_id"] == pid and x["impact"] is not None)
        row = {"id": pid, "date": payout_dates.get(pid), "txn_count": len(txs), "paypal_expected": expected,
               "bank_actual": bank_amt, "ledger_implied": implied, "bank_variance": None if line is None else bank_amt - expected,
               "ledger_gap": gap, "explained_by_exceptions": explained, "residual": gap - explained,
               "bank_line_id": line["id"] if line else None}
        payouts_out.append(row)
        if line is None:
            add("BANK_MISSING_DEPOSIT", amount=expected, txns=txs[:5], payout=pid, confidence=0.9,
                reason=f"PayPal payout {pid} of {money(expected)} has no matching deposit on the bank statement.",
                action="Check the payout status in PayPal and the bank statement; contact PayPal if it was not sent.")
        elif bank_amt != expected:
            v = bank_amt - expected
            jr = (journal(f"Bank shortfall on {pid}", ("5200", -v, 0), ("1100", 0, -v)) if v < 0 else None)
            add("BANK_VARIANCE", amount=abs(v), payout=pid, lines=[line], confidence=0.5, impact=None,
                reason=(f"Payout {pid}: PayPal sent {money(expected)} but the bank received {money(bank_amt)} "
                        f"({'short' if v < 0 else 'over'} by {money(abs(v))}). "
                        f"The books agree with PayPal, so this difference sits with the bank leg."),
                action=("Check the bank statement for a wire or transfer fee. If confirmed, book it as a bank charge."
                        if v < 0 else "Check whether another credit was netted into this deposit."),
                jrn=jr)
    for b in free_bank:
        add("UNKNOWN_DEPOSIT", amount=b["amount"], lines=[b], confidence=0.7,
            reason=f"Bank line {b['id']} ({money(b['amount'])} on {b['date'][:10]}) matches no PayPal payout.",
            action="Identify the source of this deposit.")

    # --- ranking & ids ---------------------------------------------------------------
    for x in exc:
        x["severity"] = _severity(x["type"], x["amount"])
        x["risk"] = _risk(x["severity"], x["amount"])
    exc.sort(key=lambda x: (-x["risk"], x["type"], x["amount"]))
    for i, x in enumerate(exc, 1):
        x["id"] = f"EX-{i:03d}"

    total_txn = len(pp)
    by_type: dict[str, int] = defaultdict(int)
    by_sev: dict[str, int] = defaultdict(int)
    for x in exc:
        by_type[x["type"]] += 1
        by_sev[x["severity"]] += 1
    sale_gross = sum(t["gross"] for t in pp if t["type"] == "sale")
    summary = {
        "txn_count": total_txn,
        "matched_count": len(matched),
        "auto_match_rate": round(100 * len(matched) / total_txn, 1) if total_txn else 0.0,
        "exception_count": len(exc),
        "by_type": dict(by_type), "by_severity": dict(by_sev),
        "amount_at_risk": sum(x["amount"] for x in exc if x["type"] != "REF_MISMATCH"),
        "paypal_gross_sales": sale_gross,
        "paypal_fees": sum(t["fee"] for t in pp if t["type"] == "sale"),
        "refunds_total": -sum(t["gross"] for t in pp if t["type"] == "refund"),
        "ledger_sales_total": sum(e["amount"] for e in ledger if e["type"] == "sale"),
        "payouts_paypal_total": sum(p["paypal_expected"] for p in payouts_out),
        "payouts_bank_total": sum(p["bank_actual"] or 0 for p in payouts_out),
        "in_transit": sum(t["gross"] - t["fee"] for t in pending),
        "unexplained_residual": sum(abs(p["residual"]) for p in payouts_out),
        "ledger_entry_count": len(ledger), "bank_line_count": len(bank),
    }
    return {"summary": summary, "exceptions": exc, "payouts": payouts_out,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
