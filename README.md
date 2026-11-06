# ReconPilot

**Three-way reconciliation for PayPal, your books and your bank, with AI explanations and a human in the loop.**

Built for the *PayPal AI Hackathon: Build What's Next with PayPal and AI*.

PayPal pays out the net of many sales, fees, refunds and holds in one lump. Your books record each sale separately. Your bank shows only the lump. At month end someone has to prove all three agree. ReconPilot does that tie-out, finds every difference, explains each one in plain language, proposes a balanced journal entry, and waits for a person to approve it.

![status](https://img.shields.io/badge/tests-21%20passing-1f7a4d) ![license](https://img.shields.io/badge/license-MIT-blue)

## What it does

| Step | What happens | Who does it |
|---|---|---|
| 1. Collect | Reads PayPal transactions (sandbox Transaction Search API, or built-in demo data) | Code |
| 2. Tie out | Matches PayPal to the books by reference, amount, fee and customer; groups transactions into weekly payouts and compares each payout to the bank deposit | Deterministic engine |
| 3. Classify | 12 exception types: missing entries, transposed digits, net-for-gross, duplicates, near-miss references, unrecorded refunds, open disputes, wrong fees, bank shortfalls and more | Deterministic engine |
| 4. Explain | Plain-English reason, likely root cause and next step for each exception; free-form questions about the run | AI model (Claude or Gemini), with a rule-based fallback |
| 5. Propose | A balanced journal entry (debits = credits, checked in code) | Deterministic engine |
| 6. Approve | Approve, reject or escalate with a note; only approved entries reach the CSV export | Person |
| 7. Prove | Every action is written to a hash-chained audit trail; tampering is detected | Code |

**The tie-out check.** For every payout, ReconPilot computes what PayPal sent, what the bank received, and what the books imply. The gap between PayPal and the books must be fully accounted for by listed exceptions. The headline number is the *unexplained difference*, and it should be `$0.00`.

## Why the AI is built this way

- **The model explains; it never decides.** Matching, classification and journal arithmetic are deterministic and tested. The model cannot approve, post or change an exception.
- **Structured output.** Explanations must be JSON in a fixed schema. Invalid output falls back to the rule-based explanation, labelled as such in the UI.
- **Grounded chat.** Questions are answered from the run's data only. Exception IDs the model cites are checked against real IDs; invented ones are flagged.
- **Privacy.** Customer names are replaced with opaque tokens (`CUST-1a2b3c`) before anything is sent to a model.
- **Works without a key.** With no API key, every feature runs in rule-based mode.

## Quick start (demo data, no accounts needed)

```bash
git clone https://github.com/shahriarsharif406/reconpilot.git
cd reconpilot
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn reconpilot.server:app --port 8000
```

Open <http://localhost:8000>. A demo run starts automatically: 104 PayPal transactions, 100 book entries, 4 bank deposits, and 18 deliberately planted problems for the engine to find.

### Turn on the AI

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # or GEMINI_API_KEY=...
uvicorn reconpilot.server:app --port 8000
```

The header shows which model is active. Default model: `claude-sonnet-5-5` (override with `ANTHROPIC_MODEL`).

### Use your PayPal sandbox

1. Create a sandbox app at <https://developer.paypal.com/dashboard/applications/sandbox> and enable **Transaction Search** for it.
2. Export credentials:
   ```bash
   export PAYPAL_CLIENT_ID=...
   export PAYPAL_CLIENT_SECRET=...
   ```
3. Create real sandbox sales:
   ```bash
   python -m reconpilot.sandbox_seed create --count 8   # prints buyer approval links
   # open each link, sign in as your sandbox *personal* account, approve
   python -m reconpilot.sandbox_seed capture
   python -m reconpilot.sandbox_seed refund --count 2
   ```
4. In the UI choose **PayPal sandbox** and press **Run reconciliation**. Sandbox activity can take a while to appear in Transaction Search.

### Use your own books and bank statement

Choose **Your CSV** in the UI, pick a books file and a bank statement file, and press **Run reconciliation**. Column names are detected from the header row (date, reference, amount; fee, customer and type are optional for books; date, amount or credit, and description for the bank). Dates in `YYYY-MM-DD` or `MM/DD/YYYY`, amounts like `1,250.00`, `$12.50` or `(12.50)`. Rows that are not sales or refunds (fees, journals) and bank withdrawals are skipped and counted. A bad file returns a plain message with the line number. Files are read in memory and not stored, up to 5,000 rows each.

The PayPal side can be the demo transactions or the live sandbox. To try it without your own files, download `/api/sample/ledger.csv` and `/api/sample/bank.csv`, upload both with "Demo transactions", and you get the same 18 exceptions as demo mode. If your books have no fee column, fees are assumed to match PayPal, so no fee variances are reported.

> **What is real and what is synthetic.** In sandbox mode the PayPal side is read live from the PayPal sandbox API. PayPal does not provide your accounting books or bank statement, so those two sides are generated around the real transactions, with planted errors, and the UI says so ("books: synthetic"). Demo mode is synthetic end to end. Nothing in this repository is real customer or bank data.

## Architecture

```
PayPal sandbox API ──┐
 (Transaction Search)│        ┌────────────────────┐       ┌──────────────┐
                     ├──────► │ engine.py          │ ────► │ FastAPI      │ ──► Web UI
 books + bank        │        │ 3-way tie-out,     │       │ server.py    │     (single page)
 (synthetic/CSV)  ───┘        │ 12 exception types,│       └──────┬───────┘
                              │ balanced journals  │              │
                              └────────────────────┘      ┌───────┴────────┐
                                                          │ ai.py          │ explain + chat
                                                          │ (Claude/Gemini)│ (redacted, schema-checked)
                                                          ├────────────────┤
                                                          │ audit.py       │ hash-chained trail
                                                          └────────────────┘
```

| File | Role |
|---|---|
| `reconpilot/engine.py` | Pure reconciliation logic, money in integer cents |
| `reconpilot/paypal_client.py` | Sandbox-only PayPal client: OAuth2, Transaction Search, Orders v2, refunds |
| `reconpilot/sandbox_seed.py` | CLI to create real sandbox sales for testing |
| `reconpilot/datagen.py` | Synthetic data with known ground truth |
| `reconpilot/ai.py` | Model calls, redaction, validation, offline fallback |
| `reconpilot/audit.py` | Tamper-evident audit log |
| `reconpilot/server.py` | REST API and static UI |
| `tests/` | 21 tests |

### API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/run` | `{"source":"demo"\|"sandbox"}` run a reconciliation |
| POST | `/api/run/upload` | `{"ledger_csv","bank_csv","paypal":"demo"\|"sandbox"}` reconcile your own CSV text |
| GET | `/api/sample/ledger.csv`, `/api/sample/bank.csv` | Sample files that round-trip to the demo result |
| GET | `/api/result` | Summary, payouts, exceptions |
| GET | `/api/exceptions/{id}` | Full evidence and proposed journal |
| POST | `/api/exceptions/{id}/explain` | AI (or rule-based) explanation |
| POST | `/api/exceptions/{id}/decision` | `approve` / `reject` / `escalate` / `reopen` |
| POST | `/api/chat` | Ask a question about the run |
| GET | `/api/audit` | Audit trail and chain verification |
| GET | `/api/export/journal.csv` | Approved journal lines only |
| GET | `/api/export/exceptions.csv` | All exceptions with status |

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite (32 tests) checks that the engine finds exactly the planted defects across several random seeds, that the unexplained difference is zero, that every journal balances, that tampering with the audit trail is detected, that customer names never reach the model, that malformed model output falls back safely, and that the PayPal client works against recorded response shapes.

## Deploy

`render.yaml` is included. On Render: **New > Blueprint**, point at this repo, then set `ANTHROPIC_API_KEY`, `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` as environment variables.

## Limitations

- The PayPal client is verified against PayPal's documented response shapes with a mocked transport. Event-code mapping (`T00xx` payments, `T11xx` refunds, `T12xx` disputes) is best effort; check it against your own sandbox data.
- Reconciliation state is held in memory. A restart clears it. Set `AUDIT_LOG_PATH` to keep a copy of the audit trail on disk.
- In demo and sandbox modes the books and bank statement are synthetic. In **Your CSV** mode they are yours, but the PayPal side is still the demo data or the sandbox, and payouts are grouped into weekly Monday sweeps by ReconPilot (PayPal's own payout data is not read).
- There is no login. Run it locally or behind your own access control.
- Sandbox only: the PayPal client has no production endpoint.

## Roadmap

PayPal activity CSV import; push approved journals to accounting software; dispute-evidence drafting; multi-currency; scheduled daily runs with alerts.

## License

MIT. See [LICENSE](LICENSE).
