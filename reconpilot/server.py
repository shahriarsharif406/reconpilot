"""FastAPI app: run reconciliations, review exceptions, keep a tamper-evident audit trail."""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import datagen, importer
from .ai import AIClient
from .audit import AuditLog
from .engine import money, reconcile
from .paypal_client import PayPalError, PayPalSandbox

STATIC = Path(__file__).parent / "static"
DECISIONS = {"approve": "approved", "reject": "rejected", "escalate": "escalated", "reopen": "open"}


class RunBody(BaseModel):
    source: str = Field("demo", pattern="^(demo|sandbox)$")
    seed: int = 42


class UploadBody(BaseModel):
    ledger_csv: str = Field(min_length=1, max_length=2_000_000)
    bank_csv: str = Field(min_length=1, max_length=2_000_000)
    paypal: str = Field("demo", pattern="^(demo|sandbox)$")
    seed: int = 42


class DecisionBody(BaseModel):
    decision: str = Field(pattern="^(approve|reject|escalate|reopen)$")
    note: str = Field("", max_length=500)
    actor: str = Field("reviewer", max_length=60)


class ChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=500)


def create_app(ai: AIClient | None = None, paypal: PayPalSandbox | None = None,
               audit: AuditLog | None = None) -> FastAPI:
    app = FastAPI(title="ReconPilot", version="1.0.0")
    ai = ai or AIClient()
    paypal = paypal or PayPalSandbox()
    audit = audit or AuditLog(os.getenv("AUDIT_LOG_PATH") or None)
    state: dict = {"data": None, "result": None, "decisions": {}, "meta": {}}

    def need_result() -> dict:
        if not state["result"]:
            raise HTTPException(409, "No reconciliation has been run yet. POST /api/run first.")
        return state["result"]

    def view(x: dict) -> dict:
        d = state["decisions"].get(x["id"])
        return {**x, "status": d["status"] if d else "open", "decision": d}

    def find(ex_id: str) -> dict:
        x = next((e for e in need_result()["exceptions"] if e["id"] == ex_id), None)
        if not x:
            raise HTTPException(404, f"Unknown exception {ex_id}")
        return x

    @app.get("/api/health")
    def health():
        return {"ok": True, "ai": ai.mode, "paypal_sandbox_configured": paypal.configured,
                "has_run": state["result"] is not None}

    def sandbox_paypal(seed: int) -> tuple[dict, dict]:
        """PayPal side from the live sandbox: transactions, grouped into weekly payouts."""
        if not paypal.configured:
            raise HTTPException(400, "Sandbox credentials missing. Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.")
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(days=30)
        try:
            txns, skipped = paypal.list_transactions(start, end)
        except PayPalError as exc:
            raise HTTPException(502, str(exc))
        if not txns:
            raise HTTPException(404, "No usable transactions in the last 30 days of this sandbox account. "
                                     "Run `python -m reconpilot.sandbox_seed` first.")
        txns = [dict(t) for t in txns]
        payouts = datagen.assign_payouts(txns, start.date(), end.date())
        payouts = [p for p in payouts if any(t.get("payout_id") == p["id"] for t in txns)]
        data = {"paypal": txns, "payouts": payouts,
                "period": {"start": start.date().isoformat(), "end": end.date().isoformat()}}
        return data, {"skipped": skipped}

    def finish_run(data: dict, meta: dict) -> dict:
        state.update({"data": data, "result": reconcile(data), "decisions": {}, "meta": meta})
        audit.reset()
        s = state["result"]["summary"]
        audit.append("system", "run", meta["source"], {"transactions": s["txn_count"],
                                                          "exceptions": s["exception_count"], "ai": ai.mode})
        return {"meta": meta, "summary": s}

    @app.post("/api/run")
    def run(body: RunBody):
        if body.source == "demo":
            data = datagen.generate_demo(body.seed)
            meta = {"source": "demo", "paypal": "synthetic demo data", "books": "synthetic"}
        else:
            pp, extra = sandbox_paypal(body.seed)
            start = dt.date.fromisoformat(pp["period"]["start"])
            end = dt.date.fromisoformat(pp["period"]["end"])
            data = datagen.build_books(pp["paypal"], body.seed, start, end, inject=True)
            data.update({"period": pp["period"], "source": "sandbox"})
            meta = {"source": "sandbox", "paypal": "PayPal sandbox (live API)",
                    "books": "synthetic (built around real sandbox data)", **extra}
        return finish_run(data, meta)

    @app.post("/api/run/upload")
    def run_upload(body: UploadBody):
        """Books and bank statement come from the reviewer's own CSV files."""
        try:
            ledger, lmeta = importer.parse_ledger(body.ledger_csv)
            bank, bmeta = importer.parse_bank(body.bank_csv)
        except importer.ImportProblem as exc:
            raise HTTPException(422, str(exc))
        if body.paypal == "sandbox":
            pp, extra = sandbox_paypal(body.seed)
            paypal_label = "PayPal sandbox (live API)"
        else:
            d = datagen.generate_demo(body.seed)
            pp = {"paypal": d["paypal"], "payouts": d["payouts"], "period": d["period"]}
            extra, paypal_label = {}, "synthetic demo data"
        data = {"paypal": pp["paypal"], "payouts": pp["payouts"], "period": pp["period"],
                "ledger": ledger, "bank": bank, "source": "upload"}
        books = f"your CSV ({lmeta['rows']} rows" + ("" if lmeta["fee_column"] else ", no fee column, fees assumed to match PayPal") + ")"
        meta = {"source": "upload", "paypal": paypal_label, "books": books,
                "bank": f"your CSV ({bmeta['rows']} deposits)", "skipped_rows": lmeta["skipped"] + bmeta["skipped"], **extra}
        return finish_run(data, meta)

    @app.get("/api/sample/ledger.csv", response_class=PlainTextResponse)
    def sample_ledger():
        d = datagen.generate_demo(42)
        return PlainTextResponse(importer.ledger_to_csv(d["ledger"]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=sample_books.csv"})

    @app.get("/api/sample/bank.csv", response_class=PlainTextResponse)
    def sample_bank():
        d = datagen.generate_demo(42)
        return PlainTextResponse(importer.bank_to_csv(d["bank"]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=sample_bank.csv"})

    @app.get("/api/result")
    def result():
        r = need_result()
        return {"meta": state["meta"], "period": state["data"].get("period"), "summary": r["summary"],
                "payouts": r["payouts"], "generated_at": r["generated_at"],
                "exceptions": [{k: v for k, v in view(x).items() if k != "evidence"} for x in r["exceptions"]]}

    @app.get("/api/exceptions/{ex_id}")
    def detail(ex_id: str):
        return view(find(ex_id))

    @app.post("/api/exceptions/{ex_id}/explain")
    def explain(ex_id: str):
        x = find(ex_id)
        out = ai.explain(x)
        audit.append("ai", "explain", ex_id, {"source": out["source"], "root_cause": out["root_cause"]})
        return out

    @app.post("/api/exceptions/{ex_id}/decision")
    def decide(ex_id: str, body: DecisionBody):
        x = find(ex_id)
        status = DECISIONS[body.decision]
        if status == "open":
            state["decisions"].pop(ex_id, None)
        else:
            state["decisions"][ex_id] = {"status": status, "note": body.note, "actor": body.actor,
                                         "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        audit.append(body.actor, body.decision, ex_id,
                     {"type": x["type"], "amount": money(x["amount"]), "note": body.note,
                      "journal": x["journal"]["description"] if (x["journal"] and status == "approved") else None})
        return view(x)

    @app.post("/api/chat")
    def chat(body: ChatBody):
        r = need_result()
        out = ai.chat(body.question, r, state["decisions"])
        audit.append("ai", "chat", "question", {"q": body.question[:120], "source": out["source"]})
        return out

    @app.get("/api/audit")
    def audit_trail():
        return {"verify": audit.verify(), "entries": audit.entries}

    @app.get("/api/export/journal.csv", response_class=PlainTextResponse)
    def export_journal():
        r = need_result()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["exception_id", "description", "account", "account_name", "debit", "credit", "approved_by", "approved_at"])
        for x in r["exceptions"]:
            d = state["decisions"].get(x["id"])
            if d and d["status"] == "approved" and x["journal"]:
                for line in x["journal"]["lines"]:
                    w.writerow([x["id"], x["journal"]["description"], line["account"], line["name"],
                                f"{line['debit'] / 100:.2f}", f"{line['credit'] / 100:.2f}", d["actor"], d["ts"]])
        audit.append("system", "export", "journal.csv", {})
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=approved_journal.csv"})

    @app.get("/api/export/exceptions.csv", response_class=PlainTextResponse)
    def export_exceptions():
        r = need_result()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "type", "severity", "amount", "status", "payout", "reason", "recommended_action"])
        for x in r["exceptions"]:
            v = view(x)
            w.writerow([x["id"], x["type"], x["severity"], f"{x['amount'] / 100:.2f}", v["status"],
                        x["payout_id"] or "", x["reason"], x["action"]])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=exceptions.csv"})

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
