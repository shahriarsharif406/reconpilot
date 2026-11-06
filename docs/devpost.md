# Devpost submission text

## Project name
ReconPilot

## Tagline (under 60 characters)
Prove PayPal, your books and your bank all agree.

## Elevator pitch
ReconPilot ties every PayPal payout to the bank deposit and to your books, explains each difference in plain language, proposes a balanced journal entry, and waits for a person to approve it.

## Inspiration
When a business sells through PayPal, three records are supposed to agree: what PayPal says it collected, what the books say, and what the bank received. They rarely do. PayPal pays out one lump that nets sales, fees, refunds and disputes. The books record each sale one by one. Someone then spends hours hunting for the difference. I wanted a tool that finds every difference, shows its evidence, and gets a person to a decision in seconds, without letting an AI quietly change financial records.

## What it does
- Reads PayPal transactions and groups them into payouts.
- Reconciles your own books, bank statement and PayPal activity from CSV files (column names detected automatically), or runs on built-in demo data or the live PayPal sandbox.
- Ties each payout three ways: PayPal sent, bank received, books imply.
- Finds 12 kinds of problems: unrecorded sales, transposed digits, net-instead-of-gross entries, duplicate entries, near-miss invoice references, unrecorded refunds, open disputes, wrong processing fees, duplicate customer charges, bank shortfalls, missing deposits, and unknown deposits.
- Proves nothing is hidden: the unexplained difference is shown as one number that should be $0.00.
- Explains each exception in plain English with a likely root cause and next step.
- Proposes a balanced journal entry (debits equal credits, enforced in code).
- Lets a person approve, reject or escalate with a note. Only approved entries reach the CSV export.
- Records every action in a hash-chained audit trail that detects tampering.
- Drafts a reply to PayPal for an open dispute from the run's facts, with placeholders for evidence the business must supply. Nothing is sent.
- Produces a one-page printable report: tie-out, every decision, and the audit-chain status.
- Answers questions about the run ("Which refunds are missing from the books?") using only that run's data.

## How we use PayPal
- PayPal REST API, sandbox environment: OAuth2 client credentials.
- Transaction Search API (`/v1/reporting/transactions`) supplies the PayPal side of the tie-out: payments, refunds, fees and dispute holds.
- Orders v2 and the refund API (`/v2/checkout/orders`, `/v2/payments/captures/{id}/refund`) create real sandbox sales and refunds for testing, through the included `sandbox_seed` command.
- Payout, fee and refund semantics from PayPal drive the matching logic.

## How we use AI
The model is the explainer and the analyst, not the decision maker.
1. **Explanations.** For each exception the model receives the engine's evidence (customer names replaced by tokens) and returns JSON with a summary, a root cause from a fixed list, a recommended action and a confidence. Output is validated; invalid output falls back to a rule-based explanation, labelled as such.
2. **Grounded chat.** Reviewers ask questions in plain language. The model sees only the run's data. Any exception ID it cites is checked against the real list; invented IDs are flagged.
3. **Dispute drafts.** For an open dispute the model drafts a reply to PayPal from the run's facts only. The draft is accepted only if it cites the real invoice and transaction ids; otherwise a template is shown. A person edits and sends it.
4. **Guardrails.** The model cannot approve, post or edit anything. Journal arithmetic and matching are deterministic and tested.

Models: Anthropic Claude (default `claude-sonnet-5-5`) or Google Gemini, selected by environment key. With no key, ReconPilot runs in rule-based mode.

## How we built it
Python 3, FastAPI, httpx, and a single-page vanilla JavaScript front end. The reconciliation engine is pure Python with money held as integer cents. The test suite (42 tests) checks the engine against planted ground truth across several random seeds. Built with Claude Code.

## Challenges
- Fuzzy matching can steal another sale's entry. A missing INV-1043 looks one edit away from INV-1044 with the same amount. The fix: match exact references first, and never let a fuzzy match consume an entry whose reference belongs to another PayPal sale.
- Proving completeness. Listing differences is not enough; the tool shows that every cent of the gap between PayPal and the books traces to a listed exception.
- Keeping customer data out of prompts. A test caught names leaking through the engine's own explanation text; they are now scrubbed before any model call.

## Accomplishments
- The engine finds exactly the planted problems on every tested seed.
- A balanced journal for every fixable exception, and a verifiable audit trail.
- Works with no API key, so judges can run it in one command.

## What we learned
Payment reconciliation is mostly about trust: show the evidence, keep the math deterministic, and keep a person in charge of the books.

## What's next
Push approved journals to accounting software, draft dispute evidence, handle multiple currencies, and run daily with alerts.

## Built with
python, fastapi, httpx, javascript, paypal-rest-api, paypal-sandbox, anthropic-claude, google-gemini, claude-code, render

## Tools (model or AI service used and how)
- Anthropic Claude (or Google Gemini): explains each reconciliation exception as validated JSON and answers grounded questions about the run.
- Claude Code: used to write and test the codebase.
- PayPal sandbox REST APIs: Transaction Search for data, Orders v2 and refunds for test data.

## Try it
- Repo: https://github.com/shahriarsharif406/reconpilot (public, MIT license)
- Hosted demo: <paste your Render URL>
- Setup: `pip install -r requirements.txt` then `uvicorn reconpilot.server:app --port 8000`
