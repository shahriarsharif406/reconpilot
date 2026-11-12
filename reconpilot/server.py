"""FastAPI app: run reconciliations, review exceptions, keep a tamper-evident audit trail."""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import datagen, importer
from .ai import AIClient
from .audit import AuditLog
from .engine import money, norm_ref, reconcile
from .report import build_report
from .paypal_client import PayPalError, PayPalSandbox
from .paypal_sim import SIM_CLIENT_ID, SIM_CLIENT_SECRET, sim_transport

STATIC = Path(__file__).parent / "static"
DECISIONS = {"approve": "approved", "reject": "rejected", "escalate": "escalated", "reopen": "open"}


class RunBody(BaseModel):
    source: str = Field("demo", pattern="^(demo|sandbox|simulator)$")
    seed: int = 42


class UploadBody(BaseModel):
    ledger_csv: str = Field(min_length=1, max_length=2_000_000)
    bank_csv: str = Field(min_length=1, max_length=2_000_000)
    paypal: str = Field("demo", pattern="^(demo|sandbox|csv)$")
    paypal_csv: str = Field("", max_length=4_000_000)
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

    def paypal_side(client: PayPalSandbox, seed: int, exercise_orders: bool = False) -> tuple[dict, dict]:
        """PayPal side read through the client: transactions, grouped into weekly payouts."""
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(days=30)
        try:
            if exercise_orders:  # simulator only: create, read, capture and refund one order first
                o = client.create_order("49.00", "INV-SIM-1")
                client.get_order(o["id"])
                cap = client.capture_order(o["id"])
                client.refund_capture(cap["purchase_units"][0]["payments"]["captures"][0]["id"], "10.00")
            txns, skipped = client.list_transactions(start, dt.datetime.now(dt.timezone.utc))
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

    def sandbox_paypal(seed: int) -> tuple[dict, dict]:
        if not paypal.configured:
            raise HTTPException(400, "Sandbox credentials missing. Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.")
        return paypal_side(paypal, seed)

    def simulator_paypal(seed: int) -> tuple[dict, dict]:
        calls: list[str] = []
        client = PayPalSandbox(SIM_CLIENT_ID, SIM_CLIENT_SECRET, transport=sim_transport(seed, calls),
                               base_url="http://127.0.0.1:1")
        data, extra = paypal_side(client, seed, exercise_orders=True)
        return data, {**extra, "api_calls": calls}

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
        elif body.source == "simulator":
            pp, extra = simulator_paypal(body.seed)
            start = dt.date.fromisoformat(pp["period"]["start"])
            end = dt.date.fromisoformat(pp["period"]["end"])
            data = datagen.build_books(pp["paypal"], body.seed, start, end, inject=True)
            data.update({"period": pp["period"], "source": "simulator"})
            meta = {"source": "simulator", "paypal": "PayPal API simulator (local; not PayPal)",
                    "books": "synthetic (built around the simulator's data)", **extra}
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
        if body.paypal == "csv":
            if not body.paypal_csv.strip():
                raise HTTPException(422, "Choose your PayPal activity CSV file, or pick another PayPal source.")
            try:
                txns, pmeta = importer.parse_paypal_activity(body.paypal_csv)
            except importer.ImportProblem as exc:
                raise HTTPException(422, str(exc))
            start, end = dt.date.fromisoformat(pmeta["start"]), dt.date.fromisoformat(pmeta["end"])
            payouts = datagen.assign_payouts(txns, start, end)
            payouts = [p for p in payouts if any(t.get("payout_id") == p["id"] for t in txns)]
            pp = {"paypal": txns, "payouts": payouts, "period": {"start": start.isoformat(), "end": end.isoformat()}}
            extra, paypal_label = {"paypal_skipped_rows": pmeta["skipped"]}, f"your PayPal activity CSV ({pmeta['rows']} transactions)"
        elif body.paypal == "sandbox":
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

    @app.post("/api/exceptions/{ex_id}/dispute-draft")
    def dispute_draft(ex_id: str):
        x = find(ex_id)
        if x["type"] != "DISPUTE_OPEN":
            raise HTTPException(400, "Dispute drafts are only available for open-dispute exceptions.")
        hold = x["evidence"]["paypal"][0]
        pp = {t["id"]: t for t in state["data"]["paypal"]}
        sale = pp.get(hold.get("original_id")) or {}
        ref = hold.get("invoice_ref") or sale.get("invoice_ref") or hold["id"]
        entry = next((e for e in state["data"]["ledger"] if e["type"] == "sale" and norm_ref(e["ref"]) == norm_ref(ref)), None)
        facts = {"invoice_ref": ref, "hold_transaction_id": hold["id"],
                 "original_transaction_id": sale.get("id") or hold.get("original_id") or hold["id"],
                 "amount": money(x["amount"]), "hold_date": hold["date"][:10],
                 "sale_date": (sale.get("date") or hold["date"])[:10], "sale_gross": money(sale.get("gross", x["amount"])),
                 "books_entry_id": entry["id"] if entry else None, "books_amount": money(entry["amount"]) if entry else None,
                 "books_date": entry["date"][:10] if entry else None}
        out = ai.dispute_draft(facts)
        audit.append("ai", "dispute_draft", ex_id, {"source": out["source"]})
        return out

    @app.get("/api/report.html", response_class=HTMLResponse)
    def report():
        r = need_result()
        audit.append("system", "report", "report.html", {})
        return HTMLResponse(build_report(r, state, audit.verify(), len(audit.entries)))

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
                "payouts": r["payouts"], "bridge": r["bridge"], "generated_at": r["generated_at"],
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
