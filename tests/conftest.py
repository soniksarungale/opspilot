import pytest

from app import db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SYNC_PROCESSING", "1")
    db.init()
    yield


def _today_at(hour: int) -> str:
    from app import clock
    return clock.iso(clock.now().replace(hour=hour, minute=0, second=0, microsecond=0))


EMAIL_FACTS = {
    "Kraft Displays, has informed us": {
        "summary": "500 display units for Pune launch delayed by 10 days",
        "intents": [{"type": "delivery_delay", "description": "Display units delayed"}],
        "location": "Pune", "item": "display units", "quantity_required": 500, "quantity_available": 120,
        "supplier": "Kraft Displays", "delay_days": 10, "deadline": "TODAY16",
        "requested_action": "obtain approval for any additional cost",
        "evidence": {"location": "Pune store launch", "item": "500 display units", "quantity_required": "500 display units",
                     "quantity_available": "only 120 units are currently available", "supplier": "Kraft Displays",
                     "delay_days": "delayed by 10 days", "deadline": "by 4:00 PM today",
                     "requested_action": "obtain approval for any additional cost"},
    },
    "Kraft Displays has told us": {
        "summary": "Display units late for Pune opening",
        "intents": [{"type": "delivery_delay", "description": "Display units late"}],
        "location": "Pune", "item": "display units", "quantity_required": 500, "quantity_available": 120,
        "supplier": "Kraft Displays", "delay_days": 10, "deadline": "TODAY16", "requested_action": "decide what to do",
        "evidence": {"location": "the Pune store opening", "item": "display units", "quantity_required": "We need 500 units",
                     "quantity_available": "only has 120", "supplier": "Kraft Displays", "delay_days": "10 days late",
                     "deadline": "by 4 PM today", "requested_action": "work out what we do"},
    },
    "200 display units available": {
        "summary": "Warehouse count says 200 display units",
        "intents": [{"type": "inventory_shortage", "description": "Stock count"}],
        "location": "Pune", "item": "display units", "quantity_required": 500, "quantity_available": 200,
        "supplier": "Kraft Displays",
        "evidence": {"location": "Pune warehouse", "item": "display units", "quantity_required": "order of 500 units",
                     "quantity_available": "200 display units available", "supplier": "Kraft Displays"},
    },
    "short on stock": {"summary": "Stock shortage at new store", "intents": [{"type": "inventory_shortage", "description": "Shortage"}]},
    "Ignore previous instructions": {"summary": "Invoice update", "intents": [{"type": "other", "description": "Invoice"}]},
    "POS terminal": {"summary": "Several issues at Mumbai", "intents": [
        {"type": "other", "description": "POS fault"}, {"type": "supplier_issue", "description": "Late shelving"},
        {"type": "customer_commitment", "description": "Bulk order for Friday"}]},
}
BLANK = {f: None for f in ["location", "store", "item", "quantity_required", "quantity_available", "supplier",
                           "delay_days", "deadline", "event_date", "requested_action"]}


class FakeLLM:
    def __init__(self):
        self.mode = None
        self.calls = []

    def __call__(self, system: str, prompt: str):
        import json
        from app import llm
        self.calls.append(system[:30])
        if self.mode == "down":
            raise llm.LLMError("LLM unavailable")
        if self.mode == "invalid_first":
            self.mode = None
            return "this is not json", "fake-model"
        payload = json.loads(prompt.split("\n\nYour last reply")[0])
        if system.startswith("You read operational"):
            body = payload["request"]
            found = next((v for k, v in EMAIL_FACTS.items() if k in body), {"summary": "Request", "intents": []})
            out = {**BLANK, "dependencies": [], "injection_suspected": False, "notes": "", "evidence": {}, **found}
            if out["deadline"] == "TODAY16":
                out["deadline"] = _today_at(16)
            return json.dumps(out), "fake-model"
        if system.startswith("You assess urgency"):
            return json.dumps({"urgency": "high", "impact": "Launch at risk", "risk_level": self.risk_level,
                               "reasons": ["Shortage of units"]}), "fake-model"
        if system.startswith("You propose actions"):
            options = [
                {"title": "Air freight from Mumbai warehouse", "actions": ["Move 260 units"], "est_cost_inr": 40000,
                 "time_to_effect": "2 days", "rationale": "Stock exists in Mumbai", "risks": "Cost"},
                {"title": "Order from alternate supplier", "actions": ["Place rush order"], "est_cost_inr": 150000,
                 "time_to_effect": "4 days", "rationale": "Metro Fixtures has short lead time", "risks": "Price"},
                {"title": "Open with partial displays", "actions": ["Use 120 units"], "est_cost_inr": 0,
                 "time_to_effect": "now", "rationale": "No extra cost", "risks": "Weaker launch"},
            ]
            options = [o for o in options if o["title"] not in payload["rejected_options"]]
            thin = payload["facts"].get("quantity_required") is None
            return json.dumps({"options": options, "recommended": 0, "insufficient_evidence": thin,
                               "missing_for_decision": ["quantity"] if thin else []}), "fake-model"
        if system.startswith("You write a short"):
            return json.dumps({"recipient": "ops@example.test", "subject": "Update", "body": "Status update"}), "fake-model"
        return json.dumps({"headline": "Summary", "body": "- One case open"}), "fake-model"

    risk_level = "high"


@pytest.fixture
def fake_llm(monkeypatch):
    from app import llm
    fake = FakeLLM()
    monkeypatch.setattr(llm, "complete", fake)
    return fake


def user(username: str) -> dict:
    return db.one("SELECT * FROM users WHERE username=?", (username,))
