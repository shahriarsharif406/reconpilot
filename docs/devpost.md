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
- **Ties three records together.** For every PayPal payout: what PayPal sent, what the bank received, what the books imply.
- **Shows where the difference went.** A step-by-step chart walks from "your books say $20,570" to "the bank received $19,336", one labelled step per cause, and the steps land on the bank total exactly.
- **Proves nothing is hidden.** The unexplained difference is one number that should read $0.00.
- **Finds 12 kinds of problems** (unrecorded sales and refunds, typing slips, duplicates, wrong fees, open disputes, bank shortfalls, missing deposits) and explains each in plain English with evidence and a balanced journal entry.
- **Keeps a person in charge.** Approve, reject or escalate with a note; only approved entries reach the export. Every action goes into a tamper-evident audit trail.
- **Works on your own files.** Upload books, bank statement and a PayPal activity CSV; get a printable report and a draft reply to PayPal for open disputes.

## How we use PayPal
- ReconPilot integrates the PayPal REST API (sandbox base URL `https://api-m.sandbox.paypal.com`): OAuth2 client credentials, the Transaction Search API (`/v1/reporting/transactions`, 31-day window, paging), Orders v2 (create, get, capture) and capture refunds. The client refuses any other host, so it can never touch live PayPal.
- The PayPal side of every reconciliation comes from these APIs, or from a PayPal Activity report CSV with the same fields.
- Honest status: I live in Bangladesh, where PayPal sign-up is not available, so I could not create sandbox credentials and have not run the client against the live sandbox. The PayPal Developer team confirmed on the hackathon Discord that Bangladesh is eligible but PayPal does not operate there.
- What I did instead: I wrote a **local PayPal API simulator** that serves the same endpoints with PayPal's documented JSON shapes (token auth, paged Transaction Search with the 31-day limit, order create/get/capture, refunds, plus noise rows such as withdrawals and pending payments). In "PayPal simulator" mode the real client code runs against it, and the UI lists every API call it made. The simulator is clearly labelled everywhere; it is a stand-in, not PayPal, and it proves the client's request building, paging and parsing, not PayPal's real behaviour.
- Anyone with sandbox keys can run the same client live with two environment variables and `python -m reconpilot.sandbox_seed` (README, "Use your PayPal sandbox").
- The demo video uses synthetic data, labelled as such on screen.

## How we use AI
The model is the explainer and the analyst, not the decision maker.
1. **Explanations.** For each exception the model receives the engine's evidence (customer names replaced by tokens) and returns JSON with a summary, a root cause from a fixed list, a recommended action and a confidence. Output is validated; invalid output falls back to a rule-based explanation, labelled as such.
2. **Grounded chat.** Reviewers ask questions in plain language. The model sees only the run's data. Any exception ID it cites is checked against the real list; invented IDs are flagged.
3. **Dispute drafts.** For an open dispute the model drafts a reply to PayPal from the run's facts only. The draft is accepted only if it cites the real invoice and transaction ids; otherwise a template is shown. A person edits and sends it.
4. **Guardrails.** The model cannot approve, post or edit anything. Journal arithmetic and matching are deterministic and tested.

Models: Anthropic Claude (default `claude-sonnet-5-5`) or Google Gemini, selected by environment key. With no key, ReconPilot runs in rule-based mode.

## How we built it
Python 3, FastAPI, httpx, and a single-page vanilla JavaScript front end. The reconciliation engine is pure Python with money held as integer cents. The test suite (53 tests) checks the engine against planted ground truth across several random seeds. Built with Claude Code.

## Challenges
- No sandbox access from Bangladesh. PayPal sign-up is unavailable here. I built the integration against PayPal's documentation, wrote a local PayPal API simulator so the real client code could be exercised end to end (and tested against it), and added CSV import so the reconciliation engine works on real PayPal data without credentials. The live sandbox path is untested by me.
- Fuzzy matching can steal another sale's entry. A missing INV-1043 looks one edit away from INV-1044 with the same amount. The fix: match exact references first, and never let a fuzzy match consume an entry whose reference belongs to another PayPal sale.
- Proving completeness. Listing differences is not enough; the tool shows that every cent of the gap between PayPal and the books traces to a listed exception.
- Keeping customer data out of prompts. A test caught names leaking through the engine's own explanation text; they are now scrubbed before any model call.

## Accomplishments
- The engine finds exactly the planted problems on every tested seed, and the "where the difference went" chart is checked to land on the bank total.
- A balanced journal for every fixable exception, and a verifiable audit trail.
- Works with no API key, so judges can run it in one command.

## What we learned
Payment reconciliation is mostly about trust: show the evidence, keep the math deterministic, and keep a person in charge of the books.

## What's next
Push approved journals to accounting software, draft dispute evidence, handle multiple currencies, and run daily with alerts.

## Built with
python, fastapi, httpx, javascript, paypal-rest-api, anthropic-claude, google-gemini, claude-code, render

## Tools (model or AI service used and how)
- Anthropic Claude (or Google Gemini): explains each reconciliation exception as validated JSON and answers grounded questions about the run.
- Claude Code: used to write and test the codebase.
- PayPal REST API client (sandbox endpoints): Transaction Search for data, Orders v2 and refunds for test data. Exercised against a local PayPal simulator and mocked responses; not run live by the author.

## Try it
- Repo: https://github.com/shahriarsharif406/reconpilot (public, MIT license)
- Hosted demo: <paste your Render URL>
- Setup: `pip install -r requirements.txt` then `uvicorn reconpilot.server:app --port 8000`
