import pytest

from app import clock, db, workflow
from tests.conftest import user


def imported(n: int) -> dict:
    request_id = workflow.import_email(n, user("analyst"))
    return workflow.get(request_id)


def codes(req: dict) -> list[str]:
    return [f["code"] for f in req["flags"]]


def actions(request_id: int) -> list[str]:
    return [a["action"] for a in db.query("SELECT action FROM audit WHERE request_id=? ORDER BY id", (request_id,))]


def pending(request_id: int) -> dict:
    return db.one("SELECT * FROM approvals WHERE request_id=? AND status='pending'", (request_id,))


def test_happy_path_from_email_to_closure(fake_llm):
    req = imported(1)
    assert req["status"] == "review"
    assert req["facts"]["quantity_required"] == 500
    workflow.choose(req["id"], user("analyst"), option_index=0)
    approval = pending(req["id"])
    assert approval["role"] == "manager" and approval["amount"] == 40000
    workflow.decide(approval["id"], user("manager"), True, "ok")
    assert workflow.get(req["id"])["status"] == "in_progress"
    workflow.transition(req["id"], user("analyst"), "resolve")
    workflow.transition(req["id"], user("analyst"), "close")
    assert workflow.get(req["id"])["status"] == "closed"
    types = {a["actor_type"] for a in db.query("SELECT actor_type FROM audit WHERE request_id=?", (req["id"],))}
    assert {"agent", "user"} <= types


def test_choosing_a_zero_cost_option_skips_approval(fake_llm):
    req = imported(1)
    workflow.choose(req["id"], user("analyst"), option_index=2, reason="Launch can go ahead with fewer units")
    assert workflow.get(req["id"])["status"] == "in_progress"
    assert pending(req["id"]) is None


def test_second_report_of_same_issue_is_flagged_duplicate(fake_llm):
    first = imported(1)
    second = imported(2)
    assert "duplicate" in codes(second) and second["duplicate_of"] == first["id"]


def test_email_quantity_differing_from_inventory_is_flagged(fake_llm):
    req = imported(3)
    assert "conflict" in codes(req)


def test_missing_information_needs_info_and_drafts_clarification(fake_llm):
    req = imported(4)
    assert req["status"] == "needs_info" and "missing_info" in codes(req)
    drafts = db.query("SELECT * FROM messages WHERE request_id=?", (req["id"],))
    assert len(drafts) == 1 and drafts[0]["recipient"] == req["sender"] and drafts[0]["status"] == "draft"


def test_instructions_inside_email_are_flagged_and_lower_confidence(fake_llm):
    req = imported(5)
    assert "injection" in codes(req)
    assert req["confidence"] < 0.6


def test_invalid_model_output_is_retried_once(fake_llm):
    fake_llm.mode = "invalid_first"
    req = imported(1)
    assert req["status"] == "review"
    runs = [db.loads(a["detail"]) for a in db.query("SELECT detail FROM audit WHERE actor='intake' AND action='run'")]
    assert [r["ok"] for r in runs] == [False, True]


def test_unavailable_llm_needs_info_then_retry_succeeds(fake_llm):
    fake_llm.mode = "down"
    req = imported(1)
    assert req["status"] == "needs_info" and "llm_unavailable" in codes(req)
    fake_llm.mode = None
    workflow.retry(req["id"], user("analyst"))
    assert workflow.get(req["id"])["status"] == "review"


def test_user_correction_updates_facts_and_audit(fake_llm):
    req = imported(1)
    workflow.correct_field(req["id"], user("analyst"), "quantity_required", "450", "Email says 450 in the table")
    after = workflow.get(req["id"])
    assert after["facts"]["quantity_required"] == 450
    assert after["evidence"]["quantity_required"] == {"by": "analyst"}
    entry = db.one("SELECT detail FROM audit WHERE request_id=? AND action='field_corrected'", (req["id"],))
    assert db.loads(entry["detail"])["before"] == 500


def test_correcting_with_non_numeric_quantity_is_rejected(fake_llm):
    req = imported(1)
    with pytest.raises(workflow.InputError):
        workflow.correct_field(req["id"], user("analyst"), "quantity_required", "many", "")


def test_overriding_recommendation_needs_a_reason(fake_llm):
    req = imported(1)
    with pytest.raises(workflow.InputError):
        workflow.choose(req["id"], user("analyst"), option_index=1)
    workflow.choose(req["id"], user("analyst"), option_index=1, reason="Mumbai stock is committed")
    assert "recommendation_overridden" in actions(req["id"])


def test_custom_action_is_routed_by_its_cost(fake_llm):
    req = imported(1)
    workflow.choose(req["id"], user("analyst"), custom={"title": "Buy locally", "est_cost_inr": 600000}, reason="Faster")
    assert pending(req["id"])["role"] == "admin"


def test_rejected_option_is_excluded_from_new_plan(fake_llm):
    req = imported(1)
    workflow.choose(req["id"], user("analyst"), option_index=0)
    workflow.decide(pending(req["id"])["id"], user("manager"), False, "Too expensive")
    after = workflow.get(req["id"])
    assert after["status"] == "review" and "approval_rejected" in codes(after)
    live = [o["title"] for o in after["options"] if not o.get("rejected")]
    assert "Air freight from Mumbai warehouse" not in live
    assert any(o.get("rejected") for o in after["options"])


def test_rejection_needs_a_reason(fake_llm):
    req = imported(1)
    workflow.choose(req["id"], user("analyst"), option_index=0)
    with pytest.raises(workflow.InputError):
        workflow.decide(pending(req["id"])["id"], user("manager"), False, "")


def test_overdue_approval_escalates_to_next_role(fake_llm):
    req = imported(1)
    workflow.choose(req["id"], user("analyst"), option_index=0)
    db.set_setting("clock_offset_hours", "30")
    assert workflow.escalate_overdue() == 1
    assert pending(req["id"])["role"] == "regional_head"
    assert "approval_escalated" in actions(req["id"])


def test_overdue_approval_with_no_higher_role_is_flagged(fake_llm):
    req = imported(1)
    workflow.choose(req["id"], user("analyst"), custom={"title": "Buy locally", "est_cost_inr": 600000}, reason="Faster")
    db.set_setting("clock_offset_hours", "30")
    workflow.escalate_overdue()
    assert "approval_overdue" in codes(workflow.get(req["id"]))


def test_supplier_update_cancels_pending_approval(fake_llm):
    req = imported(1)
    workflow.choose(req["id"], user("analyst"), option_index=0)
    workflow.supplier_update(req["id"], user("analyst"), 3, "Supplier now delivers in 3 days")
    after = workflow.get(req["id"])
    assert after["facts"]["delay_days"] == 3 and after["status"] == "review"
    assert pending(req["id"]) is None
    assert "circumstances_changed" in actions(req["id"])


def test_cancel_cancels_pending_approval_and_notifies_approver(fake_llm):
    req = imported(1)
    workflow.choose(req["id"], user("analyst"), option_index=0)
    workflow.transition(req["id"], user("analyst"), "cancel", "Launch postponed")
    assert workflow.get(req["id"])["status"] == "cancelled"
    assert db.one("SELECT status FROM approvals WHERE request_id=?", (req["id"],))["status"] == "cancelled"
    assert db.one("SELECT 1 AS x FROM messages WHERE request_id=? AND subject LIKE 'Cancelled%'", (req["id"],))


def test_invalid_transition_is_refused(fake_llm):
    req = imported(1)
    with pytest.raises(ValueError):
        workflow.transition(req["id"], user("analyst"), "close")


def test_unauthorized_approval_attempt_is_audited(fake_llm):
    req = imported(1)
    workflow.choose(req["id"], user("analyst"), option_index=0)
    with pytest.raises(PermissionError):
        workflow.decide(pending(req["id"])["id"], user("analyst"), True, "")
    assert "unauthorized_attempt" in actions(req["id"])
    assert pending(req["id"]) is not None


def test_message_draft_and_send(fake_llm):
    req = imported(1)
    message_id = workflow.draft(req["id"], user("analyst"), "supplier")
    workflow.send(message_id, user("analyst"))
    assert db.one("SELECT status FROM messages WHERE id=?", (message_id,))["status"] == "sent"
    with pytest.raises(ValueError):
        workflow.send(message_id, user("analyst"))


def test_summary_is_stored_on_the_request(fake_llm):
    req = imported(1)
    workflow.summarize(req["id"], user("analyst"))
    assert workflow.get(req["id"])["summary"].startswith("Summary")


def test_inr_uses_indian_grouping():
    assert workflow.inr(500000) == "Rs 5,00,000"
    assert workflow.inr(40000) == "Rs 40,000"
