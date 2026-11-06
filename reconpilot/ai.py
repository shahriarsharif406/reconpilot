"""AI layer: explains exceptions and answers questions, grounded in engine output.

Design rules
  * The deterministic engine finds and classifies exceptions and builds balanced
    journal entries. The model only *explains*; it cannot approve, post or edit anything.
  * Customer names/emails are replaced with opaque tokens before anything is sent out.
  * Model output must be valid JSON in a fixed schema; otherwise we fall back to the
    rule-based explanation and say so (`source: "rules"`).
  * Providers: Anthropic Messages API or Google Gemini, chosen from environment keys.
    With no key, everything still works in rule-based mode.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import httpx

from .engine import money

ROOT_CAUSES = ["data_entry_error", "timing_difference", "processing_fee", "customer_refund",
               "customer_dispute", "duplicate_submission", "system_sync_gap", "bank_charge", "unknown"]

DEFAULT_CAUSE = {
    "MISSING_IN_LEDGER": "system_sync_gap", "MISSING_IN_PAYPAL": "data_entry_error",
    "AMOUNT_MISMATCH": "data_entry_error", "FEE_VARIANCE": "processing_fee",
    "DUPLICATE_LEDGER": "duplicate_submission", "DUPLICATE_PAYMENT": "duplicate_submission",
    "REF_MISMATCH": "data_entry_error", "UNRECORDED_REFUND": "customer_refund",
    "DISPUTE_OPEN": "customer_dispute", "BANK_VARIANCE": "bank_charge",
    "BANK_MISSING_DEPOSIT": "timing_difference", "UNKNOWN_DEPOSIT": "unknown",
}

EXPLAIN_SYSTEM = (
    "You are the analyst inside ReconPilot, a payment reconciliation tool. A deterministic engine has "
    "already found and classified an exception. Explain it for a finance reviewer.\n"
    "Rules: use ONLY facts in the JSON provided; never invent amounts, dates, names or IDs; never say "
    "anything has been posted, approved or fixed. Reply with ONE JSON object and nothing else, with keys: "
    '"summary" (max 2 plain-English sentences), "root_cause" (one of ' + json.dumps(ROOT_CAUSES) + '), '
    '"recommended_action" (max 2 sentences), "confidence" (number 0 to 1).'
)

CHAT_SYSTEM = (
    "You are the analyst inside ReconPilot. Answer the reviewer's question using ONLY the reconciliation "
    "data in the JSON context. Cite exception IDs like EX-003 where relevant. If the data does not contain "
    "the answer, say so. Never invent figures. You cannot approve, reject or post anything. Be concise."
)


def _token(name: str | None) -> str | None:
    if not name:
        return None
    return "CUST-" + hashlib.sha1(name.encode()).hexdigest()[:6]


def _compact_txn(t: dict) -> dict:
    return {"id": t["id"], "date": t["date"][:10], "type": t["type"], "gross": money(t["gross"]),
            "fee": money(t["fee"]), "customer": _token(t.get("customer")), "ref": t.get("invoice_ref")}


def _compact_entry(e: dict) -> dict:
    return {"id": e["id"], "date": e["date"][:10], "type": e["type"], "ref": e["ref"],
            "amount": money(e["amount"]), "fee_booked": money(e.get("fee_booked", 0)),
            "customer": _token(e.get("customer"))}


def scrub_names(x: dict, text: str) -> str:
    """Replace any customer name tied to this exception with its opaque token."""
    ev = x["evidence"]
    for n in {r.get("customer") for r in ev["paypal"] + ev["ledger"] if r.get("customer")}:
        text = text.replace(n, _token(n))
    return text


def exception_context(x: dict) -> dict:
    ev = x["evidence"]
    scrub = lambda text: scrub_names(x, text)  # noqa: E731

    return {"id": x["id"], "type": x["type"], "subtype": x.get("subtype"), "severity": x["severity"],
            "amount": money(x["amount"]), "engine_reason": scrub(x["reason"]), "engine_action": scrub(x["action"]),
            "paypal": [_compact_txn(t) for t in ev["paypal"]],
            "ledger": [_compact_entry(e) for e in ev["ledger"]],
            "bank": [{"id": b["id"], "date": b["date"][:10], "amount": money(b["amount"]), "memo": b.get("memo")}
                     for b in ev["bank"]],
            "proposed_journal": x["journal"]}


def rule_explain(x: dict) -> dict:
    """Deterministic fallback explanation (no model involved)."""
    cause = DEFAULT_CAUSE.get(x["type"], "unknown")
    sub = x.get("subtype")
    extra = {"transposed_digits": " Two digits appear swapped, which is a common typing slip.",
             "net_instead_of_gross": " The books seem to have recorded the amount after PayPal's fee.",
             "near_reference": " The references differ by a single character.",
             "probable": " This is a lower-confidence match based on amount, customer and date."}.get(sub, "")
    return {"summary": x["reason"] + extra, "root_cause": cause, "recommended_action": x["action"],
            "confidence": x["confidence"], "source": "rules"}


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _validate(obj: dict | None) -> dict | None:
    if not isinstance(obj, dict):
        return None
    try:
        conf = float(obj.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    summary, action = obj.get("summary"), obj.get("recommended_action")
    if not isinstance(summary, str) or not isinstance(action, str) or not summary.strip():
        return None
    cause = obj.get("root_cause") if obj.get("root_cause") in ROOT_CAUSES else "unknown"
    return {"summary": summary.strip()[:700], "root_cause": cause,
            "recommended_action": action.strip()[:500], "confidence": max(0.0, min(1.0, conf))}


class AIClient:
    def __init__(self, provider: str | None = None, transport: httpx.BaseTransport | None = None):
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.gemini_key = os.getenv("GEMINI_API_KEY", "")
        want = (provider or os.getenv("AI_PROVIDER", "")).lower()
        if want == "anthropic" and self.anthropic_key:
            self.provider = "anthropic"
        elif want == "gemini" and self.gemini_key:
            self.provider = "gemini"
        elif want in ("", "auto"):
            self.provider = "anthropic" if self.anthropic_key else "gemini" if self.gemini_key else "offline"
        else:
            self.provider = "offline"
        self.model = (os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5") if self.provider == "anthropic"
                      else os.getenv("GEMINI_MODEL", "gemini-2.5-flash") if self.provider == "gemini" else None)
        self._http = httpx.Client(timeout=45, transport=transport)
        self._cache: dict[str, dict] = {}

    @property
    def mode(self) -> str:
        return "offline" if self.provider == "offline" else f"{self.provider}:{self.model}"

    def _complete(self, system: str, user: str, max_tokens: int = 500) -> str:
        if self.provider == "anthropic":
            r = self._http.post("https://api.anthropic.com/v1/messages",
                                headers={"x-api-key": self.anthropic_key, "anthropic-version": "2023-06-01",
                                         "content-type": "application/json"},
                                json={"model": self.model, "max_tokens": max_tokens, "system": system,
                                      "messages": [{"role": "user", "content": user}]})
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json().get("content", []))
        if self.provider == "gemini":
            r = self._http.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                headers={"x-goog-api-key": self.gemini_key, "content-type": "application/json"},
                json={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": user}]}],
                      "generationConfig": {"maxOutputTokens": max_tokens}})
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        raise RuntimeError("no provider")

    # ---------------------------------------------------------------------------------
    def explain(self, x: dict) -> dict:
        ctx = exception_context(x)
        key = hashlib.sha1(json.dumps(ctx, sort_keys=True).encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        out = None
        if self.provider != "offline":
            try:
                text = self._complete(EXPLAIN_SYSTEM, json.dumps(ctx), 450)
                out = _validate(_parse_json(text))
                if out:
                    out["source"] = self.mode
            except (httpx.HTTPError, KeyError, IndexError, ValueError):
                out = None
        if out is None:
            out = rule_explain(x)
            if self.provider != "offline":
                out["note"] = "Model unavailable or returned an invalid response; showing rule-based explanation."
        self._cache[key] = out
        return out

    def chat(self, question: str, result: dict, decisions: dict) -> dict:
        exceptions = result["exceptions"]
        ids = {x["id"] for x in exceptions}
        ctx = {"summary": {k: (money(v) if k in ("amount_at_risk", "paypal_gross_sales", "paypal_fees",
                                                  "refunds_total", "in_transit", "unexplained_residual") else v)
                           for k, v in result["summary"].items()},
               "exceptions": [{"id": x["id"], "type": x["type"], "severity": x["severity"], "amount": money(x["amount"]),
                               "payout": x["payout_id"], "status": decisions.get(x["id"], {}).get("status", "open"),
                               "reason": scrub_names(x, x["reason"])} for x in exceptions],
               "payouts": [{"id": p["id"], "paypal": money(p["paypal_expected"]), "bank": money(p["bank_actual"]),
                            "books_implied": money(p["ledger_implied"]), "residual": money(p["residual"])}
                           for p in result["payouts"]]}
        if self.provider != "offline":
            try:
                text = self._complete(CHAT_SYSTEM, f"Question: {question}\n\nContext JSON:\n{json.dumps(ctx)}", 600)
                cited = sorted(set(re.findall(r"EX-\d{3}", text)))
                return {"answer": text.strip(), "cited": [c for c in cited if c in ids],
                        "unverified_ids": [c for c in cited if c not in ids], "source": self.mode}
            except (httpx.HTTPError, KeyError, IndexError, ValueError):
                pass
        ans = self._offline_chat(question, ctx)
        ans["source"] = "rules"
        return ans

    @staticmethod
    def _offline_chat(question: str, ctx: dict) -> dict:
        q = question.lower()
        exs = ctx["exceptions"]
        m = re.search(r"ex-\d{3}", q)
        if m:
            hit = next((e for e in exs if e["id"].lower() == m.group(0)), None)
            if hit:
                return {"answer": f"{hit['id']} ({hit['type']}, {hit['severity']}, {hit['amount']}): {hit['reason']}",
                        "cited": [hit["id"]], "unverified_ids": []}
        kw = {"refund": "UNRECORDED_REFUND", "dispute": "DISPUTE_OPEN", "duplicate": "DUPLICATE",
              "fee": "FEE_VARIANCE", "bank": "BANK", "missing": "MISSING", "amount": "AMOUNT_MISMATCH",
              "reference": "REF_MISMATCH"}
        for word, t in kw.items():
            if word in q:
                hits = [e for e in exs if t in e["type"]]
                if hits:
                    lines = [f"{e['id']}: {e['type']} {e['amount']}" for e in hits]
                    return {"answer": f"{len(hits)} related exception(s): " + "; ".join(lines),
                            "cited": [e["id"] for e in hits], "unverified_ids": []}
        s = ctx["summary"]
        return {"answer": (f"{s['exception_count']} exceptions across {s['txn_count']} PayPal transactions; "
                           f"{s['auto_match_rate']}% matched automatically; {s['amount_at_risk']} touched by exceptions; "
                           f"unexplained residual {s['unexplained_residual']}. Ask about a type (refunds, disputes, "
                           f"fees, bank) or an ID such as EX-001."),
                "cited": [], "unverified_ids": []}
