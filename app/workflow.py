import logging
import os
import threading
from datetime import timedelta

from app import agents, clock, db, rules
from app.agents import AgentError

log = logging.getLogger(__name__)

JSON_COLUMNS = {"facts", "evidence", "flags", "risk", "options", "chosen"}
INT_FIELDS = {"quantity_required", "quantity_available", "delay_days"}
FACT_FIELDS = ["location", "store", "item", "quantity_required", "quantity_available", "supplier", "delay_days",
               "deadline", "event_date", "requested_action"]
KEEP_FLAGS = {"ungrounded", "approval_overdue"}
AUDIENCES = ("requester", "supplier", "store team", "approver")
OPEN = ("processing", "needs_info", "review", "pending_approval", "in_progress")


class InputError(ValueError):
    """Invalid input from the user, reported as 400 rather than a state conflict."""


class RateLimited(Exception):
    pass


# Actions that start model calls; capped per user because every demo user shares one model login.
MODEL_ACTIONS = ("created", "retry", "field_corrected", "circumstances_changed", "message_drafted",
                 "summary_generated", "report_generated")
MODEL_ACTIONS_PER_HOUR = 30


def check_rate(user: dict) -> None:
    since = clock.iso(clock.now() - timedelta(hours=1))
    marks = ",".join("?" * len(MODEL_ACTIONS))
    used = db.one(f"SELECT COUNT(*) AS n FROM audit WHERE actor=? AND actor_type='user' AND ts>=? "
                  f"AND action IN ({marks})", (user["username"], since, *MODEL_ACTIONS))["n"]
    if used >= MODEL_ACTIONS_PER_HOUR:
        raise RateLimited(f"Limit of {MODEL_ACTIONS_PER_HOUR} analysis actions per hour reached. Try again later.")


def inr(amount: int) -> str:
    digits = str(int(amount))
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "Rs " + ",".join(groups + [tail])


def get(request_id: int) -> dict:
    row = db.one("SELECT * FROM requests WHERE id=?", (request_id,))
    if not row:
        raise LookupError("Request not found")
    for col in JSON_COLUMNS:
        row[col] = db.loads(row[col], [] if col in ("flags", "options") else {} if col != "chosen" else None)
    return row


def _save(request_id: int, **cols) -> None:
    cols["updated_at"] = clock.iso(clock.now())
    names = list(cols)
    values = [db.dumps(v) if k in JSON_COLUMNS else v for k, v in cols.items()]
    db.execute(f"UPDATE requests SET {', '.join(n + '=?' for n in names)} WHERE id=?", (*values, request_id))


def _background(fn, *args) -> None:
    def guarded():
        try:
            fn(*args)
        except Exception as e:
            log.exception("background step failed")
            db.log(args[0], "system", "system", "processing_failed", {"error": str(e)})
            _save(args[0], status="needs_info")

    if os.getenv("SYNC_PROCESSING") == "1":
        guarded()
    else:
        threading.Thread(target=guarded, daemon=True).start()


def _name(user: dict) -> str:
    return user["username"]


def create_request(subject: str, body: str, sender: str, channel: str, received_at: str, user: dict | None) -> int:
    if user:
        check_rate(user)
    now = clock.iso(clock.now())
    request_id = db.execute(
        "INSERT INTO requests(subject, sender, channel, body, received_at, status, facts, evidence, flags, risk, "
        "options, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (subject, sender, channel, body, received_at, "processing", "{}", "{}", "[]", "{}", "[]",
         user["id"] if user else None, now, now))
    db.log(request_id, _name(user) if user else sender, "user", "created", {"channel": channel})
    _background(process, request_id)
    return request_id


def import_email(inbox_id: int, user: dict) -> int:
    mail = db.one("SELECT * FROM inbox WHERE id=?", (inbox_id,))
    if not mail:
        raise LookupError("Email not found")
    if mail["request_id"]:
        raise ValueError("Email already imported")
    request_id = create_request(mail["subject"], mail["body"], mail["sender"], mail["channel"], mail["received_at"], user)
    db.execute("UPDATE inbox SET request_id=? WHERE id=?", (request_id, inbox_id))
    return request_id


def process(request_id: int) -> None:
    req = get(request_id)
    hits = rules.detect_injection(req["body"])
    try:
        out = agents.extract(req)
    except AgentError as e:
        flags = [{"code": "llm_unavailable", "message": f"Could not read the request automatically: {e}. Retry, or fill the fields by hand."}]
        if hits:
            flags.append(_injection_flag())
        _save(request_id, flags=flags, status="needs_info", confidence=0)
        db.log(request_id, "system", "system", "processing_failed", {"error": str(e)})
        return
    evidence = out.get("evidence") or {}
    facts = {f: _clean(f, out.get(f)) for f in FACT_FIELDS}
    facts.update(summary=out["summary"], intents=out["intents"], dependencies=out.get("dependencies") or [],
                 notes=out["notes"], injection_suspected=bool(hits or out["injection_suspected"]))
    dropped = rules.ungrounded_fields(facts, evidence, req["body"])
    flags = []
    if dropped:
        shown = ", ".join(f"{f}={facts[f]!r}" for f in dropped)
        flags.append({"code": "ungrounded", "fields": dropped,
                      "message": f"Values not supported by the email text were removed: {shown}"})
        for f in dropped:
            facts[f] = None
    _save(request_id, facts=facts, evidence=evidence, flags=flags, summary=facts["summary"])
    db.log(request_id, "intake", "agent", "extracted", {"fields": [f for f in FACT_FIELDS if facts[f] is not None]})
    assess(request_id)


def _clean(field: str, value):
    if value == "":
        return None
    if field in INT_FIELDS:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return value


def _injection_flag() -> dict:
    return {"code": "injection",
            "message": "Request contains instructions aimed at the system; they were ignored. Human review required."}


def _store_for(facts: dict) -> dict | None:
    for store in db.query("SELECT * FROM stores"):
        for value in (facts.get("store"), facts.get("location")):
            if rules.same_place(value, store["city"]) or rules.same_place(value, store["name"]):
                return store
    return None


def assess(request_id: int, keep_risk: bool = False) -> None:
    req = get(request_id)
    facts, now = req["facts"], clock.now()
    missing = rules.missing_fields(facts)
    inventory = rules.match_inventory(facts, db.query("SELECT * FROM inventory"))
    clashes = rules.conflicts(facts, inventory)
    earlier = [dict(r, facts=db.loads(r["facts"], {})) for r in
               db.query("SELECT id, status, body, facts FROM requests WHERE id<?", (request_id,))]
    duplicate_of = rules.find_duplicate(facts, req["body"], earlier)
    kept = [f for f in req["flags"] if f["code"] in KEEP_FLAGS]
    ungrounded = next((f["fields"] for f in kept if f["code"] == "ungrounded"), [])

    flags = list(kept)
    if missing:
        flags.append({"code": "missing_info", "message": "Missing: " + ", ".join(missing)})
    for text in clashes:
        flags.append({"code": "conflict", "message": text})
    if duplicate_of:
        flags.append({"code": "duplicate", "message": f"Looks like a duplicate of request #{duplicate_of}"})
    injection = bool(facts.get("injection_suspected"))
    if injection:
        flags.append(_injection_flag())
    if any(o.get("rejected") for o in req["options"]):
        flags.append({"code": "approval_rejected", "message": "A proposed option was rejected by an approver"})

    store = _store_for(facts)
    verification = {"missing": missing, "conflicts": clashes, "inventory": inventory}
    rule_level = rules.rule_risk(facts, store, now)
    risk, options, insufficient = req["risk"] if keep_risk else {}, req["options"], False
    try:
        if not risk:
            agent_risk = agents.assess_risk(req, facts, verification, {"store": store, "now": clock.iso(now)})
            disagree = rules.agents_disagree(agent_risk["risk_level"], rule_level)
            risk = {**agent_risk, "rule_level": rule_level, "disagree": disagree,
                    "final_level": rules.higher_level(agent_risk["risk_level"], rule_level)}
        if risk.get("disagree"):
            flags.append({"code": "agents_disagree", "message":
                          f"Risk agent says {risk['risk_level']}, rules say {risk['rule_level']}; the higher is used"})
        rejected = [o for o in req["options"] if o.get("rejected")]
        item_words = rules.words(facts.get("item"))
        context = {"inventory": [r for r in db.query("SELECT * FROM inventory") if item_words & rules.words(r["item"])],
                   "suppliers": db.query("SELECT * FROM suppliers"), "store": store, "now": clock.iso(now)}
        plan = agents.recommend(req, facts, risk, verification, context, [o["title"] for o in rejected])
        for i, option in enumerate(plan["options"]):
            option["recommended"] = i == plan["recommended"]
        options = rejected + plan["options"]
        insufficient = plan["insufficient_evidence"]
        if insufficient:
            flags.append({"code": "insufficient_evidence", "message":
                          "Not enough evidence to recommend safely. Missing: " + ", ".join(map(str, plan["missing_for_decision"]))})
    except AgentError as e:
        if not risk:
            risk = {"rule_level": rule_level, "final_level": rule_level, "reasons": ["Rule-based level only"]}
        flags.append({"code": "llm_unavailable", "message": f"AI step failed: {e}. Retry later."})
        options = [o for o in req["options"] if o.get("rejected")]

    score = rules.confidence(missing, ungrounded, clashes, bool(risk.get("disagree")), injection, insufficient)
    if score < rules.LOW_CONFIDENCE:
        flags.append({"code": "low_confidence", "message": f"Confidence {score} is below {rules.LOW_CONFIDENCE}"})
    status = "needs_info" if missing else "review"
    _save(request_id, flags=flags, risk=risk, options=options, confidence=score, duplicate_of=duplicate_of, status=status)
    if missing:
        _draft_clarification(req, missing)
    db.log(request_id, "system", "system", "assessed",
           {"confidence": score, "flags": [f["code"] for f in flags], "status": status})


def _draft_clarification(req: dict, missing: list[str]) -> None:
    subject = f"More information needed: {req['subject']}"
    if db.one("SELECT 1 AS x FROM messages WHERE request_id=? AND subject=? AND status='draft'", (req["id"], subject)):
        return
    body = (f"Hello,\n\nWe received your request \"{req['subject']}\" but could not act on it yet. "
            f"Please reply with: {', '.join(m.replace('_', ' ') for m in missing)}.\n\nOperations team")
    _add_message(req["id"], req["sender"], subject, body)


def _add_message(request_id: int, recipient: str, subject: str, body: str) -> int:
    return db.execute(
        "INSERT INTO messages(request_id, recipient, subject, body, status, created_at) VALUES (?,?,?,?,?,?)",
        (request_id, recipient, subject, body, "draft", clock.iso(clock.now())))


def correct_field(request_id: int, user: dict, field: str, value, reason: str) -> None:
    req = get(request_id)
    if field not in FACT_FIELDS:
        raise InputError("Unknown field")
    if req["status"] not in ("needs_info", "review"):
        raise ValueError("Fields can only be corrected before a decision is chosen")
    check_rate(user)
    if field in INT_FIELDS and value not in (None, ""):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise InputError(f"{field} must be a whole number")
    value = None if value == "" else value
    facts, evidence = req["facts"], req["evidence"]
    before = facts.get(field)
    facts[field] = value
    evidence[field] = {"by": _name(user)}
    flags = []
    for flag in req["flags"]:
        if flag["code"] == "ungrounded" and field in flag["fields"]:
            flag["fields"].remove(field)
            if not flag["fields"]:
                continue
        flags.append(flag)
    _save(request_id, facts=facts, evidence=evidence, flags=flags, status="processing")
    db.log(request_id, _name(user), "user", "field_corrected", {"field": field, "before": before, "after": value, "reason": reason})
    _background(assess, request_id)


def choose(request_id: int, user: dict, option_index: int | None = None, custom: dict | None = None, reason: str = "") -> None:
    req = get(request_id)
    if req["status"] != "review":
        raise ValueError("A decision can only be chosen while the request is in review")
    if custom:
        title, cost = str(custom.get("title", "")).strip(), custom.get("est_cost_inr", 0)
        if not title or not isinstance(cost, int) or cost < 0:
            raise InputError("Custom action needs a title and a cost of 0 or more")
        option = {"title": title, "actions": [], "est_cost_inr": cost, "rationale": "Entered by the user", "custom": True}
    else:
        if option_index is None or not 0 <= option_index < len(req["options"]):
            raise InputError("Choose one of the listed options")
        option = req["options"][option_index]
        if option.get("rejected"):
            raise ValueError("That option was already rejected")
    low_confidence = any(f["code"] == "low_confidence" for f in req["flags"])
    override = bool(custom) or not option.get("recommended") or low_confidence
    if override and not reason.strip():
        raise InputError("A reason is required to override or to act on a low-confidence recommendation")
    db.log(request_id, _name(user), "user", "recommendation_overridden" if override else "recommendation_accepted",
           {"option": option["title"], "est_cost_inr": option["est_cost_inr"], "reason": reason})
    now, cost = clock.now(), option["est_cost_inr"]
    why = rules.approval_reasons(cost, (req["risk"] or {}).get("final_level"), low_confidence)
    if not why:
        _save(request_id, chosen=option, status="in_progress")
        return
    role = rules.approver_for(cost)
    deadline = req["facts"].get("deadline")
    due = clock.parse(deadline) if deadline and clock.parse(deadline) > now else now + timedelta(hours=2)
    db.execute("INSERT INTO approvals(request_id, role, amount, due_at, status, created_at) VALUES (?,?,?,?,?,?)",
               (request_id, role, cost, clock.iso(due), "pending", clock.iso(now)))
    _add_message(request_id, role, f"Approval needed: {req['subject']}",
                 f"Request #{request_id} needs your approval.\nAction: {option['title']}\n"
                 f"Extra cost: {inr(cost)}\nWhy approval is needed: {'; '.join(why)}\nDecision needed by: {due.strftime('%d %b %Y, %I:%M %p')} IST.")
    _save(request_id, chosen=option, status="pending_approval")
    db.log(request_id, "system", "system", "approval_requested", {"role": role, "amount": cost, "due_at": clock.iso(due), "why": why})


def decide(approval_id: int, user: dict, approve: bool, reason: str = "") -> None:
    approval = db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not approval:
        raise LookupError("Approval not found")
    request_id = approval["request_id"]
    if approval["status"] != "pending":
        raise ValueError(f"This approval is already {approval['status']}")
    chooser = db.one("SELECT actor FROM audit WHERE request_id=? AND action IN "
                     "('recommendation_accepted', 'recommendation_overridden') ORDER BY id DESC LIMIT 1", (request_id,))
    own = bool(chooser) and chooser["actor"] == _name(user)
    if own or not rules.can_approve(user["role"], approval["amount"]):
        db.log(request_id, _name(user), "user", "unauthorized_attempt",
               {"approval_id": approval_id, "amount": approval["amount"], "role": user["role"], "own_choice": own})
        raise PermissionError("You cannot approve an action you chose yourself" if own
                              else "You are not allowed to decide this approval")
    if not approve and not reason.strip():
        raise InputError("A reason is required to reject")
    now = clock.iso(clock.now())
    db.execute("UPDATE approvals SET status=?, decided_by=?, reason=?, decided_at=? WHERE id=?",
               ("approved" if approve else "rejected", user["id"], reason, now, approval_id))
    db.log(request_id, _name(user), "user", "approval_approved" if approve else "approval_rejected",
           {"approval_id": approval_id, "amount": approval["amount"], "reason": reason})
    req = get(request_id)
    if approve:
        _save(request_id, status="in_progress")
        return
    options, chosen = req["options"], req["chosen"]
    matching = [o for o in options if chosen and o["title"] == chosen["title"]]
    for option in matching:
        option["rejected"] = True
    if chosen and not matching:
        options.append({**chosen, "rejected": True})
    _save(request_id, options=options, chosen=None, status="processing")
    _background(assess, request_id, True)


def escalate_overdue() -> int:
    now = clock.now()
    count = 0
    for approval in db.query("SELECT * FROM approvals WHERE status='pending'"):
        if clock.parse(approval["due_at"]) >= now:
            continue
        count += 1
        db.execute("UPDATE approvals SET status='escalated', decided_at=? WHERE id=?", (clock.iso(now), approval["id"]))
        request_id, role = approval["request_id"], approval["role"]
        higher = rules.next_role(role)
        db.log(request_id, "system", "system", "approval_escalated", {"approval_id": approval["id"], "from": role, "to": higher})
        if higher:
            due = now + timedelta(hours=2)
            db.execute("INSERT INTO approvals(request_id, role, amount, due_at, status, created_at) VALUES (?,?,?,?,?,?)",
                       (request_id, higher, approval["amount"], clock.iso(due), "pending", clock.iso(now)))
            req = get(request_id)
            _add_message(request_id, higher, f"Escalated approval: {req['subject']}",
                         f"The {role} did not respond in time. Request #{request_id} needs your approval for "
                         f"{inr(approval['amount'])}.")
        else:
            req = get(request_id)
            flags = req["flags"] + [{"code": "approval_overdue", "message": "No approver left to escalate to; approval is overdue"}]
            _save(request_id, flags=flags)
    return count


def supplier_update(request_id: int, user: dict, new_delay_days: int, note: str = "") -> None:
    req = get(request_id)
    if req["status"] not in ("needs_info", "review", "pending_approval", "in_progress"):
        raise ValueError("This request can no longer be changed")
    if not isinstance(new_delay_days, int) or new_delay_days < 0:
        raise InputError("Delay must be a whole number of days")
    check_rate(user)
    facts, evidence = req["facts"], req["evidence"]
    before = facts.get("delay_days")
    facts["delay_days"] = new_delay_days
    evidence["delay_days"] = {"by": _name(user)}
    db.log(request_id, _name(user), "user", "circumstances_changed", {"delay_days_before": before, "delay_days_after": new_delay_days, "note": note})
    _cancel_pending(request_id, user, "Supplier revised the delay")
    _save(request_id, facts=facts, evidence=evidence, chosen=None, status="processing")
    _background(assess, request_id)


def _cancel_pending(request_id: int, user: dict, why: str) -> list[dict]:
    pending = db.query("SELECT * FROM approvals WHERE request_id=? AND status='pending'", (request_id,))
    for approval in pending:
        db.execute("UPDATE approvals SET status='cancelled', decided_at=? WHERE id=?", (clock.iso(clock.now()), approval["id"]))
        db.log(request_id, _name(user), "user", "approval_cancelled", {"approval_id": approval["id"], "why": why})
    return pending


TRANSITIONS = {"resolve": ("in_progress", "resolved"), "close": ("resolved", "closed")}


def transition(request_id: int, user: dict, action: str, note: str = "") -> None:
    req = get(request_id)
    before = req["status"]
    if action == "cancel":
        if before not in OPEN:
            raise ValueError("Request is already finished")
        for approval in _cancel_pending(request_id, user, note or "Request cancelled"):
            _add_message(request_id, approval["role"], f"Cancelled: {req['subject']}",
                         f"Request #{request_id} was cancelled. No decision is needed.")
        after = "cancelled"
    elif action in TRANSITIONS:
        needed, after = TRANSITIONS[action]
        if before != needed:
            raise ValueError(f"Cannot {action} a request that is {before}")
    else:
        raise InputError("Unknown action")
    _save(request_id, status=after)
    db.log(request_id, _name(user), "user", "status_changed", {"from": before, "to": after, "note": note})


def retry(request_id: int, user: dict) -> None:
    req = get(request_id)
    if req["status"] not in ("needs_info", "review"):
        raise ValueError("Only requests waiting for information or review can be retried")
    check_rate(user)
    db.log(request_id, _name(user), "user", "retry", {})
    _save(request_id, status="processing")
    _background(process if not req["facts"] else assess, request_id)


def _case(req: dict) -> dict:
    approvals = db.query("SELECT role, amount, status, due_at FROM approvals WHERE request_id=?", (req["id"],))
    return {"subject": req["subject"], "facts": req["facts"], "status": req["status"], "chosen": req["chosen"],
            "risk": (req["risk"] or {}).get("final_level"), "approvals": approvals}


def draft(request_id: int, user: dict, audience: str) -> int:
    if audience not in AUDIENCES:
        raise InputError("Audience must be one of: " + ", ".join(AUDIENCES))
    check_rate(user)
    req = get(request_id)
    out = agents.draft_message(req, audience, _case(req))
    message_id = _add_message(request_id, out["recipient"], out["subject"], out["body"])
    db.log(request_id, _name(user), "user", "message_drafted", {"message_id": message_id, "audience": audience})
    return message_id


def send(message_id: int, user: dict) -> str:
    message = db.one("SELECT * FROM messages WHERE id=?", (message_id,))
    if not message:
        raise LookupError("Message not found")
    if message["status"] not in ("draft", "failed"):
        raise ValueError("Message was already sent")
    detail = {"message_id": message_id, "recipient": message["recipient"]}
    if db.get_setting("outbox_down") == "1":
        db.execute("UPDATE messages SET status='failed' WHERE id=?", (message_id,))
        db.log(message["request_id"], "outbox", "system", "message_failed", {**detail, "error": "Mail server unavailable"})
        return "failed"
    db.execute("UPDATE messages SET status='sent', sent_at=? WHERE id=?", (clock.iso(clock.now()), message_id))
    db.log(message["request_id"], _name(user), "user", "message_sent", detail)
    return "sent"


def summarize(request_id: int, user: dict) -> str:
    check_rate(user)
    req = get(request_id)
    out = agents.report(_case(req), request_id)
    text = f"{out['headline']}\n\n{out['body']}"
    _save(request_id, summary=text)
    db.log(request_id, _name(user), "user", "summary_generated", {})
    return text


def portfolio_report(user: dict) -> dict:
    check_rate(user)
    db.log(None, _name(user), "user", "report_generated", {})
    cases = []
    for row in db.query("SELECT id FROM requests WHERE status != 'cancelled' ORDER BY id"):
        req = get(row["id"])
        cases.append({"id": req["id"], **_case(req)})
    for case in cases:
        case.pop("facts")
        case["chosen"] = (case["chosen"] or {}).get("title")
    return agents.report(cases)
