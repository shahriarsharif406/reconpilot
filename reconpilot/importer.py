"""Read a books (ledger) CSV and a bank statement CSV into the shapes the engine uses.

Column names are detected from the header row, so exports from common accounting tools
usually work without editing. Anything that cannot be read raises ImportProblem with the
row number, so the reviewer can fix the file instead of getting a silent wrong answer.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re

MAX_ROWS = 5000

ALIASES = {
    "date": ["date", "transaction date", "txn date", "posting date", "posted", "booking date", "value date"],
    "ref": ["ref", "reference", "invoice", "invoice ref", "invoice_ref", "invoice number", "invoice no", "number", "doc no"],
    "amount": ["amount", "gross", "total", "gross amount", "value", "credit amount"],
    "fee": ["fee", "fee booked", "fee_booked", "processing fee", "paypal fee", "fees"],
    "customer": ["customer", "name", "payer", "client", "customer name"],
    "type": ["type", "kind", "transaction type", "category"],
    "memo": ["memo", "description", "details", "narrative", "note", "notes", "particulars"],
    "debit": ["debit", "withdrawal", "withdrawals", "paid out"],
    "credit": ["credit", "deposit", "deposits", "paid in"],
}
DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%Y/%m/%d"]


class ImportProblem(ValueError):
    pass


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", h.strip().lower().replace("_", " "))


def _find(headers: list[str], key: str) -> int | None:
    names = [_norm(a) for a in ALIASES[key]]
    normed = [_norm(h) for h in headers]
    for n in names:
        if n in normed:
            return normed.index(n)
    return None


def parse_amount(raw: str) -> int:
    """'$1,234.56', '(12.00)', '-12', '1.234,56' is not supported on purpose. Returns cents."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty amount")
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.endswith("-")
    s = re.sub(r"[()$,\s+\-]|USD", "", s)
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        raise ValueError(f"cannot read amount '{raw}'")
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    return -cents if neg else cents


def parse_date(raw: str) -> str:
    s = (raw or "").strip()
    for f in DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, f).date().isoformat() + "T00:00:00"
        except ValueError:
            pass
    raise ValueError(f"cannot read date '{raw}' (use YYYY-MM-DD)")


def _rows(text: str, what: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    text = text.lstrip("﻿")
    if not text.strip():
        raise ImportProblem(f"The {what} file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [(i, r) for i, r in enumerate(csv.reader(io.StringIO(text), dialect), 1) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ImportProblem(f"The {what} file has a header but no data rows.")
    if len(rows) - 1 > MAX_ROWS:
        raise ImportProblem(f"The {what} file has more than {MAX_ROWS} rows. Split it by month.")
    return rows[0][1], rows[1:]


def parse_ledger(text: str) -> tuple[list[dict], dict]:
    headers, rows = _rows(text, "books")
    col = {k: _find(headers, k) for k in ("date", "ref", "amount", "fee", "customer", "type", "memo")}
    missing = [k for k in ("date", "ref", "amount") if col[k] is None]
    if missing:
        raise ImportProblem(f"Books file: could not find a column for {', '.join(missing)}. "
                            f"Found columns: {', '.join(headers)}. Expected something like date, reference, amount.")
    entries, skipped = [], 0
    for ln, r in rows:
        def cell(k):
            i = col[k]
            return r[i].strip() if i is not None and i < len(r) else ""
        try:
            amount = parse_amount(cell("amount"))
            date = parse_date(cell("date"))
            fee = parse_amount(cell("fee")) if cell("fee") else None
        except ValueError as exc:
            raise ImportProblem(f"Books file, line {ln}: {exc}")
        kind = cell("type").lower()
        if not kind:
            kind = "refund" if amount < 0 else "sale"
        if kind in ("sale", "payment", "invoice", "income"):
            kind = "sale"
        elif kind in ("refund", "credit note", "return"):
            kind = "refund"
            amount = -abs(amount)
        else:
            skipped += 1  # fees, payouts, journals etc. are not part of the sales tie-out
            continue
        e = {"date": date, "type": kind, "ref": cell("ref"), "amount": amount,
             "customer": cell("customer") or None, "memo": cell("memo") or "uploaded"}
        if fee is not None:
            e["fee_booked"] = abs(fee)
        entries.append(e)
    if not entries:
        raise ImportProblem("Books file: no sale or refund rows were found.")
    entries.sort(key=lambda e: (e["date"], e["ref"]))
    for i, e in enumerate(entries, 1):
        e["id"] = f"L-{i:04d}"
    return entries, {"rows": len(entries), "skipped": skipped, "fee_column": col["fee"] is not None}


def parse_bank(text: str) -> tuple[list[dict], dict]:
    headers, rows = _rows(text, "bank")
    col = {k: _find(headers, k) for k in ("date", "amount", "memo", "debit", "credit")}
    if col["date"] is None or (col["amount"] is None and col["credit"] is None):
        raise ImportProblem(f"Bank file: need a date column and an amount (or credit) column. "
                            f"Found columns: {', '.join(headers)}.")
    lines, skipped = [], 0
    for ln, r in rows:
        def cell(k):
            i = col[k]
            return r[i].strip() if i is not None and i < len(r) else ""
        try:
            date = parse_date(cell("date"))
            if col["credit"] is not None and cell("credit"):
                amount = parse_amount(cell("credit"))
            elif col["amount"] is not None and cell("amount"):
                amount = parse_amount(cell("amount"))
            else:
                skipped += 1  # a debit-only row
                continue
        except ValueError as exc:
            raise ImportProblem(f"Bank file, line {ln}: {exc}")
        if amount <= 0:
            skipped += 1  # only deposits can be PayPal payouts
            continue
        lines.append({"date": date, "amount": amount, "memo": cell("memo")})
    if not lines:
        raise ImportProblem("Bank file: no deposit rows were found.")
    lines.sort(key=lambda b: b["date"])
    for i, b in enumerate(lines, 1):
        b["id"] = f"B-{i:03d}"
    return lines, {"rows": len(lines), "skipped": skipped}


def ledger_to_csv(entries: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "type", "reference", "amount", "fee", "customer", "memo"])
    for e in entries:
        w.writerow([e["date"][:10], e["type"], e["ref"], f"{e['amount'] / 100:.2f}",
                    f"{e.get('fee_booked', 0) / 100:.2f}", e.get("customer") or "", e.get("memo") or ""])
    return buf.getvalue()


def bank_to_csv(lines: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "amount", "description"])
    for b in lines:
        w.writerow([b["date"][:10], f"{b['amount'] / 100:.2f}", b.get("memo") or ""])
    return buf.getvalue()
