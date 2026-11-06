from collections import Counter

from reconpilot.audit import AuditLog
from reconpilot.datagen import generate_demo
from reconpilot.engine import edit_distance, find_duplicate_payments, journal, reconcile


def run(seed=42):
    d = generate_demo(seed)
    return d, reconcile(d)


def test_engine_finds_exactly_the_injected_defects():
    for seed in (42, 7, 2026):
        d, r = run(seed)
        found = Counter(x["type"] for x in r["exceptions"])
        assert dict(found) == d["truth"], f"seed {seed}: {dict(found)} != {d['truth']}"


def test_every_dollar_of_ledger_gap_is_explained():
    for seed in (42, 7, 2026):
        _, r = run(seed)
        assert r["summary"]["unexplained_residual"] == 0
        assert all(p["residual"] == 0 for p in r["payouts"])


def test_bank_leg_detects_shortfall():
    _, r = run()
    short = [p for p in r["payouts"] if p["bank_variance"]]
    assert len(short) == 1 and short[0]["bank_variance"] == -3750


def test_all_proposed_journals_balance():
    _, r = run()
    n = 0
    for x in r["exceptions"]:
        if x["journal"]:
            n += 1
            assert sum(l["debit"] for l in x["journal"]["lines"]) == sum(l["credit"] for l in x["journal"]["lines"])
    assert n >= 10


def test_clean_data_has_no_exceptions():
    from reconpilot.datagen import build_books, synth_paypal
    import datetime as dt
    start = dt.date(2026, 10, 1)
    txns, _ = synth_paypal(5, start, 31, 60)
    txns = [t for t in txns if t["type"] == "sale"]
    txns = [t for t in txns if t["id"] not in find_duplicate_payments(txns)]
    books = build_books(txns, 5, start, dt.date(2026, 10, 31), inject=False)
    r = reconcile(books)
    assert r["summary"]["exception_count"] == 0
    assert r["summary"]["auto_match_rate"] == 100.0


def test_near_reference_does_not_steal_another_sales_entry():
    # INV-1043 is missing from the books; INV-1044 has the same amount and is one edit away.
    base = {"fee": 49, "customer": "A", "original_id": None, "payout_id": None, "type": "sale", "gross": 1000}
    pp = [dict(base, id="T1", date="2026-10-02T10:00:00", invoice_ref="INV-1043"),
          dict(base, id="T2", date="2026-10-03T10:00:00", invoice_ref="INV-1044", customer="B")]
    led = [{"id": "L1", "date": "2026-10-03T00:00:00", "type": "sale", "ref": "INV-1044", "amount": 1000,
            "fee_booked": 49, "customer": "B"}]
    r = reconcile({"paypal": pp, "ledger": led, "bank": [], "payouts": []})
    types = [x["type"] for x in r["exceptions"]]
    assert types == ["MISSING_IN_LEDGER"]


def test_edit_distance_and_duplicates():
    assert edit_distance("INV-1043", "INV-1034") == 1
    assert edit_distance("INV-1043", "INV1043") == 1
    assert edit_distance("INV-1043", "INV-2000") > 1
    tx = [{"id": "a", "type": "sale", "customer": "x", "gross": 5, "date": "2026-10-01T10:00:00"},
          {"id": "b", "type": "sale", "customer": "x", "gross": 5, "date": "2026-10-01T10:01:00"},
          {"id": "c", "type": "sale", "customer": "x", "gross": 5, "date": "2026-10-02T10:00:00"}]
    assert find_duplicate_payments(tx) == {"b": "a"}


def test_unbalanced_journal_is_rejected():
    import pytest
    with pytest.raises(AssertionError):
        journal("bad", ("1000", 100, 0), ("4000", 0, 99))


def test_audit_chain_detects_tampering():
    a = AuditLog()
    for i in range(4):
        a.append("u", "act", f"t{i}", {"i": i})
    assert a.verify()["ok"]
    a.entries[1]["detail"]["i"] = 999
    v = a.verify()
    assert not v["ok"] and v["broken_at"] == 2
