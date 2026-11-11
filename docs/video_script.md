# Demo video script (target 2:45, hard limit 3:00)

Record your screen at 1080p in the browser, running locally or on the hosted URL. Use your own voice. No music, or music you have the rights to. Do not show any third-party logos beyond the PayPal sandbox pages you actually use.

Before recording: run the app on **Demo data** (the header must say so on screen), set an AI key so the header shows the model name, and close other tabs. Do not show the "PayPal sandbox" toggle as connected.

| Time | On screen | Say |
|---|---|---|
| 0:00 to 0:15 | Full page, tie-out table | "I'm building from Bangladesh, where PayPal sign-up isn't available, so this demo runs on demo data and my own CSV files. The PayPal sandbox integration is in the code and the tests. Every business that sells through PayPal has three records that should agree: PayPal, the books, and the bank. ReconPilot shows whether they do." |
| 0:15 to 0:40 | Payout rows, red bank row, then the $0.00 line | "Each row is a payout. PayPal sent this, the bank received this, the books imply this. The bank is short by $37.50 here. And the unexplained difference is zero: every dollar of gap traces to a listed exception." |
| 0:40 to 1:05 | Scroll to "Where the difference went" | "Here is the whole story on one screen. The books say $20,570. The bank received $19,336. The biggest piece is a $1,325 refund that went out in PayPal but was never booked. Each step is a real exception, and the steps land exactly on the bank total." |
| 1:05 to 1:45 | Open the refund exception; AI analysis, evidence, journal | "The engine is deterministic. The AI only explains: plain-language cause, next step, confidence. The journal entry is balanced, checked in code." |
| 1:45 to 2:05 | Note, Approve, toast | "A person decides. I add a note and approve. Only approved entries can be exported." |
| 2:05 to 2:30 | Open the dispute exception, click Draft a reply; then Your CSV toggle | "For an open dispute it drafts a reply to PayPal from this run's facts, with blanks for the evidence I must supply. Nothing is sent. And it works on your own files: upload books, bank and a PayPal activity CSV." |
| 2:30 to 2:50 | Audit trail tab, then Report link | "Every action is in a hash-chained audit trail, and one click gives a printable report. ReconPilot: three records, one tie-out, a human in charge." |

Checklist: under 3:00; shows the app running on your device; uploaded to YouTube as Public; link pasted in the Devpost form; no copyrighted music.
