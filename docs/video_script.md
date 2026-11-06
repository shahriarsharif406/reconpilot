# Demo video script (target 2:45, hard limit 3:00)

Record your screen at 1080p in the browser, running locally or on the hosted URL. Use your own voice. No music, or music you have the rights to. Do not show any third-party logos beyond the PayPal sandbox pages you actually use.

Before recording: run the app, choose **PayPal sandbox** if your sandbox has transactions (otherwise **Demo data**, and say so), set an AI key so the header shows the model name, and close other tabs.

| Time | On screen | Say |
|---|---|---|
| 0:00 to 0:15 | Full page, tie-out table at the top | "Every business that sells through PayPal has three records that should agree: PayPal, the books, and the bank. They almost never do. ReconPilot proves whether they do, and fixes what they don't." |
| 0:15 to 0:40 | Point at one payout row, the red bank row, then the $0.00 line | "Each row is a PayPal payout. PayPal sent this, the bank received this, and the books imply this. The bank is short by $37.50 on this one. And the line here shows the unexplained difference: zero. Every dollar of gap between PayPal and the books is traced to a listed exception." |
| 0:40 to 1:00 | Header (AI model name, PayPal status), then Exceptions list | "The PayPal side comes from the PayPal sandbox API. A deterministic engine found these eighteen exceptions and ranked them by risk. The AI never decides anything. It explains." |
| 1:00 to 1:40 | Open the refund exception; scroll AI analysis, evidence, journal | "A refund of one thousand two hundred fifty dollars went out in PayPal but was never booked. Here is the evidence from both sides. Claude explains it in plain language, names the likely cause, and gives the next step. The proposed journal entry is balanced: debits equal credits, checked in code." |
| 1:40 to 2:05 | Type a note, click Approve, show toast | "A person decides. I add a note and approve. Only approved entries can be exported." |
| 2:05 to 2:25 | Open the transposed-digits exception, then Ask tab: "Which refunds are missing from the books?" | "It catches typing slips like $380 entered as $830. And I can ask questions in plain language. Answers use only this run's data, and any exception ID the model cites is verified." |
| 2:25 to 2:45 | Audit trail tab, then click the approved-journal CSV | "Every action is in a hash-chained audit trail, so tampering is detectable. The export contains only what a person approved. ReconPilot: three records, one tie-out, a human in charge." |

Checklist: under 3:00; shows the app running on your device; uploaded to YouTube as Public; link pasted in the Devpost form; no copyrighted music.
