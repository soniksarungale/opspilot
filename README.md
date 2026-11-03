# OpsPilot

OpsPilot helps a retail operations team handle requests that arrive by email or are typed in: delayed deliveries,
stock shortages, supplier problems, urgent approvals and customer commitments. It reads the request, pulls out the
facts, checks them against inventory, rates the risk, proposes options, routes the cost to the right approver and
tracks the case to closure. Every step by an agent or a person is written to an audit trail.

Live demo: https://opspilot.soniksarungale.com

Documents: `docs/PRD.pdf` (source `docs/PRD.md`) and `docs/OpsPilot.pptx`.

Demo accounts (password for all: `demo1234`):

| User | Role | Can approve |
|---|---|---|
| analyst | analyst | nothing |
| manager | manager | up to Rs 50,000 |
| head | regional head | up to Rs 5,00,000 |
| admin | admin | any amount, plus the admin page |

## Run locally

```
uv sync
uv run uvicorn app.main:app
uv run pytest -q
```

The app listens on http://127.0.0.1:8000. The SQLite file is created at `data/opspilot.db` on first start and filled
with demo users, stores, inventory, suppliers and six sample emails in the inbox (dated today, IST).

Settings are environment variables (see `.env.example`): `LLM_PROVIDER`, `LLM_MODEL`, `CLAUDE_BIN`, `LLM_TIMEOUT`,
`DB_PATH`.

The language model is reached through the `claude` command line tool in print mode, with tools, settings and
session storage switched off and one process at a time. It must be installed and logged in on the machine.
Tests replace the model with a canned fake, so they need neither. The Dockerfile builds and serves the app, but the
container has no `claude` login, so analysis steps will report the model as unavailable; the live demo runs on the
host instead.

## How it works

```
email / manual entry
        |
   intake agent  ->  facts + evidence quotes
        |
 deterministic checks (rules.py): injection patterns, missing fields, evidence grounding,
 inventory conflict, duplicates, rule-based risk
        |
   risk agent  +  rule risk  ->  final level (the higher one)
        |
   planner agent  ->  2-3 options with cost
        |
 confidence score  ->  review by a person  ->  choose / override
        |
 cost > 0 ?  -> approval routed by amount  -> approve / reject / escalate when overdue
        |
 in progress -> resolved -> closed        comms agent drafts emails, reporter agent writes summaries
```

**How agents share context.** There is no hidden shared memory. Each request row holds the facts, evidence, risk,
options and flags, and each agent receives only the slice it needs as a JSON payload (the planner gets facts, risk,
the verification result, inventory rows, suppliers and the options an approver already rejected). Agents never call
each other; `workflow.py` runs them in order and stores their output.

**When human approval is mandatory.** Any option with an extra cost above zero creates an approval. It goes to the
lowest role whose limit covers the amount. Agents cannot approve anything. Choosing an option other than the
recommended one, or writing a custom one, needs a written reason. Rejections need a reason too.

**How confidence is determined.** It is computed in code, not asked of the model: start at 1.0 and subtract 0.1 per
missing required field, 0.15 per value the email does not support, 0.15 per inventory conflict, 0.2 if the risk agent
and the rules differ by two levels or more, 0.3 for suspected injection and 0.3 if the planner says the evidence is
insufficient. Below 0.6 the recommendation is not preselected and the screen asks the user to choose or write an
action.

**How unsupported or wrong AI output is handled.**
- Each agent must return JSON with the expected keys and types. Anything else gets one retry that includes the
  error, then the step fails and the request waits for a person.
- Every extracted value must come with an exact quote from the email. A value whose quote is not in the text, or whose
  number is not in the quote, is removed and shown in a flag.
- Risk is rated twice, by the agent and by plain rules. The higher level is used and a large disagreement is flagged.
- Email text is passed as untrusted data. Pattern checks run in code as well, so a hostile email is flagged even if the
  model misses it.
- Users can edit any extracted field; the edit is recorded with the reason and the analysis is re-run.

**Monitoring and audit.** The `audit` table is append-only. It records each agent run (model, duration, attempt,
success, error, prompt version), every user action with before and after values, system actions such as escalation,
and refused attempts to approve. The request page shows it, newest last.

## Edge cases

| Case | How it is detected | What the user sees | Recovery | Audit action |
|---|---|---|---|---|
| Missing information | Required fields empty | "Missing: ..." flag, status needs info, draft email to the sender | Edit fields or send the draft | `assessed`, `field_corrected` |
| Conflicting information | Email quantity differs from inventory | Conflict flag with both numbers | Correct a field or continue | `assessed` |
| Duplicate request | Same place and item, or similar text | Duplicate flag linking the earlier request | Cancel the duplicate | `assessed` |
| Unclear intent | Several intents, no clear ask | Intents in the summary, missing-field flags | Edit fields, choose or write an action | `extracted` |
| Incorrect extraction | User edits a field | Value marked "set by <user>" | Analysis re-runs | `field_corrected` |
| Low-confidence recommendation | Confidence below 0.6 | Warning, no preselected option | Choose or write an action | `assessed`, `recommendation_overridden` |
| Approval delay | Due time passes | Approval escalated to the next role | Next role decides | `approval_escalated` |
| Approval rejection | Approver rejects | Option marked rejected, new options | Choose again | `approval_rejected` |
| Changing circumstances | Supplier update form | Pending approval cancelled, analysis re-run | Choose again | `circumstances_changed` |
| Unavailable AI service | Call fails or admin outage switch | "AI step failed" flag, status needs info | Retry button | `processing_failed`, agent `run` with error |
| Unauthorized action | Role limit check | Error banner, 403 | Ask the right approver | `unauthorized_attempt` |
| Prompt injection | Patterns plus model flag | Red flag, lower confidence | Human review | `assessed` |
| Request cancellation | Cancel button | Approvals cancelled, approver gets a draft notice | None needed | `status_changed`, `approval_cancelled` |
| Contradictory agents | Risk levels two apart | Flag with both levels, higher used | Review | `assessed` |

Not covered: communication failure. The outbox is a mock and always succeeds.

The admin page can move the clock forward (to trigger escalation without waiting) and switch the AI service off
(to show the outage path).

## Tests

`uv run pytest -q` runs 65 tests against a temporary database with a fake model: the rules (limits, confidence,
grounding, duplicates), the workflow (happy path and each edge case above) and the API (login, roles, error codes).
No test calls the real model.

## Assumptions and limits

- Email and the outbox are mocked: the inbox is seeded from `data/sample_emails.json` and "Send" only marks a message
  as sent.
- Extra costs are estimates produced by the planner from the data given; nobody checks them against a price list.
- Sessions do not expire, there is no rate limiting, and cookies are not marked secure by the app itself (TLS ends at
  the web server).
- One model process runs at a time, so analysis takes tens of seconds and requests queue.
- SQLite with a single writer lock is enough for a demo, not for many users.

Next steps: read a real mailbox (IMAP), move storage to a hosted database, retry and report failed notifications,
and add session expiry.

## Use of AI tools

I used Claude Code to help write the code, tests and documents in this repository. I chose the scope, the stack,
the approval limits and the edge cases to cover, reviewed what it produced, and checked the behaviour by running the
tests and by processing the sample emails with the real model.
