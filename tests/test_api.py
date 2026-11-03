import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import user


def client_for(username: str | None = None) -> TestClient:
    client = TestClient(app)
    if username:
        assert client.post("/api/login", json={"username": username, "password": "demo1234"}).status_code == 200
    return client


def request_awaiting(cost: int) -> int:
    analyst = client_for("analyst")
    request_id = analyst.post("/api/inbox/1/import").json()["id"]
    body = {"custom": {"title": "Buy locally", "est_cost_inr": cost}, "reason": "Fastest option"}
    assert analyst.post(f"/api/requests/{request_id}/choose", json=body).status_code == 200
    return request_id


def approval_id(request_id: int) -> int:
    return db.one("SELECT id FROM approvals WHERE request_id=?", (request_id,))["id"]


def test_login_with_wrong_password_fails():
    response = client_for().post("/api/login", json={"username": "analyst", "password": "nope"})
    assert response.status_code == 401


def test_login_sets_session_and_returns_role():
    client = client_for("manager")
    assert client.get("/api/me").json()["role"] == "manager"


def test_requests_need_a_session():
    assert client_for().get("/api/requests").status_code == 401


def test_logout_ends_the_session():
    client = client_for("analyst")
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401


def test_analyst_cannot_approve_and_attempt_is_audited(fake_llm):
    request_id = request_awaiting(40000)
    response = client_for("analyst").post(f"/api/approvals/{approval_id(request_id)}/decide", json={"approve": True})
    assert response.status_code == 403
    assert db.one("SELECT 1 AS x FROM audit WHERE request_id=? AND action='unauthorized_attempt'", (request_id,))


def test_manager_cannot_approve_above_limit(fake_llm):
    request_id = request_awaiting(60000)
    response = client_for("manager").post(f"/api/approvals/{approval_id(request_id)}/decide", json={"approve": True})
    assert response.status_code == 403


def test_regional_head_can_approve_above_manager_limit(fake_llm):
    request_id = request_awaiting(60000)
    response = client_for("head").post(f"/api/approvals/{approval_id(request_id)}/decide", json={"approve": True})
    assert response.status_code == 200


def test_pending_approvals_list_hides_amounts_above_authority(fake_llm):
    request_awaiting(60000)
    assert client_for("manager").get("/api/approvals?mine=1").json() == []
    assert len(client_for("head").get("/api/approvals?mine=1").json()) == 1


@pytest.mark.parametrize("path", ["/api/admin/state"])
def test_admin_state_is_forbidden_for_other_roles(path):
    assert client_for("manager").get(path).status_code == 403


def test_admin_clock_change_is_forbidden_for_analyst():
    assert client_for("analyst").post("/api/admin/clock", json={"hours": 4}).status_code == 403


def test_admin_can_switch_llm_off_and_requests_wait_for_info():
    admin = client_for("admin")
    admin.post("/api/admin/llm", json={"down": True})
    assert admin.get("/api/health").json()["llm_down"] is True
    request_id = admin.post("/api/inbox/1/import").json()["id"]
    assert admin.get(f"/api/requests/{request_id}").json()["status"] == "needs_info"


def test_override_without_reason_returns_400(fake_llm):
    analyst = client_for("analyst")
    request_id = analyst.post("/api/inbox/1/import").json()["id"]
    response = analyst.post(f"/api/requests/{request_id}/choose", json={"option_index": 1})
    assert response.status_code == 400


def test_unknown_request_returns_404():
    assert client_for("analyst").get("/api/requests/999").status_code == 404


def test_importing_an_email_twice_returns_409(fake_llm):
    analyst = client_for("analyst")
    analyst.post("/api/inbox/1/import")
    assert analyst.post("/api/inbox/1/import").status_code == 409


def test_oversized_body_is_rejected():
    response = client_for("analyst").post("/api/requests", json={"subject": "x", "sender": "y", "body": "a" * 20_001})
    assert response.status_code == 422


def test_detail_includes_approvals_and_audit(fake_llm):
    request_id = request_awaiting(40000)
    detail = client_for("analyst").get(f"/api/requests/{request_id}").json()
    assert detail["approvals"][0]["role"] == "manager" and detail["audit"]


def test_logout_keeps_other_sessions_of_the_same_user():
    first, second = client_for("analyst"), client_for("analyst")
    first.post("/api/logout")
    assert first.get("/api/me").status_code == 401
    assert second.get("/api/me").status_code == 200


def test_only_admin_can_reset_demo():
    assert client_for("manager").post("/api/admin/reset").status_code == 403
    assert client_for("admin").post("/api/admin/reset").status_code == 200
