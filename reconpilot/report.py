"""One-page, print-friendly reconciliation report (plain HTML, no scripts, everything escaped)."""
from __future__ import annotations

from html import escape as e

from .engine import money

CSS = """
body{font:14px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif;color:#0e2238;max-width:960px;margin:28px auto;padding:0 20px}
h1{font-size:24px;margin:0 0 4px} h2{font-size:16px;margin:26px 0 8px;border-bottom:1px solid #c3ccd5;padding-bottom:4px}
table{border-collapse:collapse;width:100%} th,td{padding:6px 8px;border-bottom:1px solid #d9e0e6;text-align:left;vertical-align:top}
th{font-size:12px;color:#5a6b7c} td.n,th.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.off{color:#b3281f;font-weight:600} .ok{color:#13694a;font-weight:600} .note{color:#5a6b7c;font-size:13px}
.box{border:1px solid #0e2238;padding:10px 14px;margin:14px 0} @media print{body{margin:0} a{display:none}}
"""


def build_report(result: dict, state: dict, verify: dict, audit_count: int) -> str:
    s, meta, decisions = result["summary"], state["meta"], state["decisions"]
    period = state["data"].get("period") or {}
    reviewed = sum(1 for x in result["exceptions"] if x["id"] in decisions)
    approved = sum(1 for d in decisions.values() if d["status"] == "approved")
    rows = ""
    for p in result["payouts"]:
        bank = "no deposit" if p["bank_actual"] is None else money(p["bank_actual"])
        off = p["bank_variance"] not in (None, 0) or p["bank_actual"] is None
        rows += (f"<tr><td>{e(p['id'])}</td><td class='n'>{money(p['paypal_expected'])}</td>"
                 f"<td class='n {'off' if off else ''}'>{e(bank)}</td><td class='n'>{money(p['ledger_implied'])}</td>"
                 f"<td class='n'>{money(p['residual'])}</td></tr>")
    ex_rows = ""
    for x in result["exceptions"]:
        d = decisions.get(x["id"])
        ex_rows += (f"<tr><td>{e(x['id'])}</td><td>{e(x['title'])}</td><td>{e(x['severity'])}</td>"
                    f"<td class='n'>{money(x['amount'])}</td><td>{e(d['status'] if d else 'open')}</td>"
                    f"<td>{e((d['actor'] + ': ' + d['note']).strip(': ') if d else '')}</td></tr>")
    chain = ("intact" if verify["ok"] else f"BROKEN at entry {verify['broken_at']}")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReconPilot reconciliation report</title><style>{CSS}</style></head><body>
<h1>Reconciliation report</h1>
<p class="note">Period {e(str(period.get('start', '')))} to {e(str(period.get('end', '')))} (USD). Generated {e(result['generated_at'])}.
PayPal: {e(meta.get('paypal', ''))}. Books: {e(meta.get('books', ''))}{('. Bank: ' + e(meta['bank'])) if meta.get('bank') else ''}.</p>
<div class="box"><b class="{'ok' if s['unexplained_residual'] == 0 else 'off'}">Unexplained difference: {money(s['unexplained_residual'])}</b><br>
{s['txn_count']} PayPal transactions, {s['auto_match_rate']}% matched automatically, {s['exception_count']} exceptions
({s['by_severity'].get('high', 0)} high), {money(s['amount_at_risk'])} touched by exceptions.
{reviewed} of {s['exception_count']} reviewed by a person, {approved} approved.</div>
<h2>Payout tie-out</h2>
<table><tr><th>Payout</th><th class="n">PayPal sent</th><th class="n">Bank received</th><th class="n">Books imply</th><th class="n">Unexplained</th></tr>{rows}</table>
<p class="note">In transit, not yet paid out: {money(s['in_transit'])}. The unexplained column is the gap between PayPal and the books after subtracting every listed exception; it should be $0.00.</p>
<h2>Exceptions and decisions</h2>
<table><tr><th>ID</th><th>Type</th><th>Severity</th><th class="n">Amount</th><th>Status</th><th>Reviewer note</th></tr>{ex_rows}</table>
<h2>Audit trail</h2>
<p>{audit_count} entries; hash chain {e(chain)}. Every run, explanation, decision and export is recorded, and altering any entry breaks the chain.</p>
<p class="note">An AI wrote the explanations and any drafts; a person made every decision. Journal entries listed as approved are in the approved-journal CSV; nothing is posted to any accounting system.</p>
</body></html>"""
