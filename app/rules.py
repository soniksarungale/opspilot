import difflib
import re
from datetime import datetime, timedelta

from app import clock

LEVELS = ["low", "medium", "high", "critical"]
LOW_CONFIDENCE = 0.6
REQUIRED = ["location", "item", "quantity_required", "supplier", "deadline"]
ROLE_ORDER = ["analyst", "manager", "regional_head", "admin"]
APPROVAL_LIMITS = {"manager": 50_000, "regional_head": 500_000, "admin": None}

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"disregard .*instructions",
    r"you are now",
    r"system prompt",
    r"(auto[- ]?)?approve (this|it|immediately|without)",
    r"mark (this|it) as approved",
    r"do not (tell|inform|notify)",
    r"act as (an? )?(admin|approver)",
]
NOT_QUOTED = {"summary", "intents", "notes", "dependencies", "injection_suspected"}


def detect_injection(text: str) -> list[str]:
    hits = []
    for pattern in INJECTION_PATTERNS:
        match = re.search(pattern, text or "", re.IGNORECASE)
        if match:
            hits.append(match.group(0))
    return hits


def missing_fields(facts: dict) -> list[str]:
    return [f for f in REQUIRED if facts.get(f) is None or facts.get(f) == ""]


def _squash(text: str) -> str:
    return " ".join(str(text).lower().split())


def is_grounded(value, quote, body: str) -> bool:
    if not isinstance(quote, str) or not quote.strip():
        return False
    squashed = _squash(quote)
    if squashed not in _squash(body):
        return False
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value) in squashed.replace(",", "")
    return True


def ungrounded_fields(facts: dict, evidence: dict, body: str) -> list[str]:
    bad = []
    for field, value in facts.items():
        if value is None or value == "" or field in NOT_QUOTED:
            continue
        quote = evidence.get(field)
        if isinstance(quote, dict) and quote.get("by"):
            continue
        if field in ("deadline", "event_date"):
            if not (isinstance(quote, str) and quote.strip()):
                bad.append(field)
        elif not is_grounded(value, quote, body):
            bad.append(field)
    return bad


def _words(text) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(w) > 3}


def _same_place(a, b) -> bool:
    a, b = str(a or "").lower().strip(), str(b or "").lower().strip()
    return bool(a) and bool(b) and (a in b or b in a)


def match_inventory(facts: dict, rows: list[dict]) -> list[dict]:
    item_words = _words(facts.get("item"))
    place = facts.get("location") or facts.get("store")
    return [r for r in rows if item_words & _words(r["item"]) and _same_place(place, r["location"])]


def conflicts(facts: dict, inventory_rows: list[dict]) -> list[str]:
    stated = facts.get("quantity_available")
    if stated is None or not inventory_rows:
        return []
    actual = sum(r["qty"] for r in inventory_rows)
    if actual == stated:
        return []
    return [f"Email says {stated} available, inventory shows {actual} at {inventory_rows[0]['location']}"]


def _when(text) -> datetime | None:
    try:
        return clock.parse(text) if text else None
    except ValueError:
        return None


def rule_risk(facts: dict, store: dict | None, now: datetime) -> str:
    score = 0
    dates = [_when(facts.get("deadline")), _when(facts.get("event_date"))]
    if store and store.get("launch_date") and store.get("status") != "open":
        dates.append(_when(store["launch_date"]))
    dates = [d for d in dates if d]
    if dates:
        gap = min(dates) - now
        if gap <= timedelta(hours=24):
            score += 2
        elif gap <= timedelta(days=7):
            score += 1
    required, available = facts.get("quantity_required"), facts.get("quantity_available")
    if required and available is not None and required > available:
        ratio = (required - available) / required
        score += 2 if ratio > 0.5 else 1
    text = " ".join([str(facts.get("requested_action") or "")] + [str(i.get("description", "")) for i in facts.get("intents") or []])
    if any(i.get("type") == "customer_commitment" for i in facts.get("intents") or []) or "launch" in text.lower():
        score += 1
    if score <= 1:
        return "low"
    if score == 2:
        return "medium"
    return "high" if score <= 4 else "critical"


def agents_disagree(agent_level: str, rule_level: str) -> bool:
    if agent_level not in LEVELS or rule_level not in LEVELS:
        return False
    return abs(LEVELS.index(agent_level) - LEVELS.index(rule_level)) >= 2


def higher_level(a: str, b: str) -> str:
    return max(a, b, key=lambda x: LEVELS.index(x) if x in LEVELS else 0)


def confidence(missing, ungrounded, conflicts, disagree: bool, injection: bool, insufficient_evidence: bool) -> float:
    score = (1.0 - 0.1 * len(missing) - 0.15 * len(ungrounded) - 0.15 * len(conflicts)
             - 0.2 * disagree - 0.3 * injection - 0.3 * insufficient_evidence)
    return round(min(1.0, max(0.0, score)), 2)


def approver_for(amount: int) -> str:
    for role, limit in APPROVAL_LIMITS.items():
        if limit is None or amount <= limit:
            return role
    return "admin"


def can_approve(user_role: str, amount: int) -> bool:
    if user_role not in APPROVAL_LIMITS:
        return False
    limit = APPROVAL_LIMITS[user_role]
    return limit is None or amount <= limit


def next_role(role: str) -> str | None:
    roles = list(APPROVAL_LIMITS)
    i = roles.index(role) if role in roles else -1
    return roles[i + 1] if 0 <= i < len(roles) - 1 else None


def find_duplicate(facts: dict, body: str, open_requests: list[dict]) -> int | None:
    for other in open_requests:
        if other["status"] in ("closed", "cancelled"):
            continue
        theirs = other.get("facts") or {}
        same_issue = _same_place(facts.get("location"), theirs.get("location")) and bool(
            _words(facts.get("item")) & _words(theirs.get("item")))
        if same_issue or difflib.SequenceMatcher(None, _squash(body), _squash(other["body"])).ratio() > 0.8:
            return other["id"]
    return None
