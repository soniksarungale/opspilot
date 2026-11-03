---
title: "OpsPilot: Product Requirements"
---

## 1. Problem and target users

A growing retail company runs its operations on email, spreadsheets and personal follow-ups. A request such as
"500 display units for the Pune launch will be 10 days late, approve the extra cost by 4 PM" has to be read,
checked against stock, assessed, costed, approved and reported by hand. This is slow, depends on who happens to be
online, and leaves no reliable record of who decided what.

| User | Need |
|---|---|
| Operations analyst | Turn an email into checked facts and options quickly; chase missing information |
| Manager, regional head | See only what needs their decision, with the evidence and the cost, and decide within their limit |
| Admin | Control the system, test failure paths, see the audit trail |
| Senior leadership | A short summary of open cases, risks and overdue approvals |

## 2. Proposed workflow

1. **Receive.** An email is imported from the inbox, or a request is typed in.
2. **Extract.** The intake agent returns the key facts (place, item, quantities, supplier, delay, deadline, ask) and an exact quote for each.
3. **Verify.** Code checks required fields, whether each quote is really in the email, inventory figures, duplicates and prompt-injection patterns.
4. **Assess.** The risk agent rates urgency and impact; a rule-based score is computed beside it; the higher level wins.
5. **Recommend.** The planner proposes two or three options with extra cost, timing, rationale and risks.
6. **Review.** A person accepts the recommendation, picks another option or writes their own. Overrides need a reason.
7. **Approve.** A cost above zero, a high or critical risk, or low confidence creates an approval for the lowest role whose limit covers the cost. The person who chose the action cannot approve it. Overdue approvals escalate to the next role.
8. **Execute and close.** Work starts once the action is chosen and approved; then resolve and close. Draft emails for the requester, supplier, store team or approver are written by the communications agent; a person sends them.
9. **Report.** The reporting agent summarises one case or the whole portfolio.

Every step is written to an append-only audit trail.

## 3. Requirements

### Functional

| # | Requirement |
|---|---|
| F1 | Read a request from a sample inbox or a manual form |
| F2 | Extract facts, dates and dependencies, each with supporting evidence |
| F3 | Assess urgency, impact and risk; show agent level, rule level and final level |
| F4 | Flag missing, conflicting, duplicate, unsupported and unsafe content |
| F5 | Recommend options with cost and rationale; allow review and override |
| F6 | Route approvals by amount; escalate when overdue; reject with reason and re-plan |
| F7 | Track status: processing, needs info, review, pending approval, in progress, resolved, closed, cancelled |
| F8 | Draft stakeholder emails and management summaries |
| F9 | Role-based login; analyst cannot approve; limits Rs 50,000 / Rs 5,00,000 / unlimited |
| F10 | Audit trail of agent runs and human actions |
| F11 | Admin tools: advance the clock, simulate an AI outage |

### Non-functional

| Area | Requirement |
|---|---|
| Safety | Agents never approve or send; email text is treated as untrusted data; model output is validated before use |
| Auditability | Audit rows are never updated or deleted; agent runs record model, duration, attempt and prompt version |
| Security | Hashed passwords, session cookie (HttpOnly, SameSite), server-side role checks, parameterised SQL, escaped output in the UI |
| Availability | If the AI service fails, requests stay usable: the user can retry or fill fields by hand |
| Performance | Analysis in under a minute per request; the interface stays responsive while it runs |
| Operability | One process, one SQLite file, health endpoint, container image |

## 4. Assumptions and trade-offs

| Decision | Reason | Cost |
|---|---|---|
| Own small pipeline instead of an agent framework | Five agents with fixed order; easier to explain, test and audit | No built-in tooling for dynamic planning |
| Code decides what the model must not (grounding, limits, final risk, confidence) | Model output can be wrong or manipulated | More rules to maintain |
| SQLite and a single process | Fast to run and demo | Not for many concurrent users |
| Model reached through a local command line tool, one call at a time | No API key handling; small server | Slow under load |
| Mock inbox and outbox | The brief does not require real mail | Real mail needs IMAP/SMTP and retries |
| Costs are model estimates | No price list in scope | Approvers must check the figures |
| Indian rupees, IST | Matches the scenario | No multi-currency |

## 5. Edge-case handling

All fifteen listed scenarios are handled. Mail sending is a mock with a switch that makes it fail, to show the failure path.

| Scenario | Detection | Response |
|---|---|---|
| Missing information | Required-field check | Flag, status "needs info", draft email asking for the fields |
| Conflicting information | Email quantity vs inventory | Flag with both numbers; confidence lowered |
| Duplicate request | Same place and item, or similar text | Flag linking the earlier request |
| Unclear intent | Several intents, no clear ask | All intents listed; missing fields flagged |
| Incorrect extraction | User edits a field | Edit logged with reason; analysis re-runs |
| Low-confidence recommendation | Confidence below 0.6 | No preselected option; choice needs a reason and an approver |
| Approval delay | Due time passes | Escalation to next role; new approval; audit entry |
| Approval rejection | Approver rejects with reason | Option marked rejected; planner proposes alternatives |
| Changing circumstances | Supplier update | Pending approval cancelled; analysis re-run |
| Unavailable AI service | Call error, timeout, bad output twice | Flag; request stays open; retry |
| Unauthorized action | Role limit check; approving own choice | Refused with 403; attempt in audit |
| Communication failure | Mail server error | Message marked failed; retry send; audit entry |
| Prompt injection | Patterns and model flag | Flag; instructions ignored; human review |
| Request cancellation | Cancel button | All approvals cancelled; approver notified by draft |
| Contradictory agents | Risk levels two or more apart | Flag; higher level used |

## 6. Success metrics and MVP scope

Targets for a pilot (not measured results):

| Metric | Target |
|---|---|
| Time from receipt to a reviewed recommendation | Under 10 minutes, from about an hour by hand |
| Extracted fields corrected by users | Under 10% |
| Recommendations accepted without override | Above 60% |
| Approvals escalated for delay | Falling month on month |
| Requests with a complete audit trail | 100% |
| Unauthorized approvals completed | 0 |

**In the MVP:** everything in section 3, the six sample emails, the demo roles, the admin tools, tests and a container image.

**Out of the MVP:** live mailbox and real outbound email, single sign-on, session expiry, price and
supplier catalogues, multi-currency, mobile app, notifications by chat, analytics dashboards.
