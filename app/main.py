import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import auth, clock, db, rules, workflow
from app.agents import AgentError
from app.auth import current_user, require

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def _escalation_loop() -> None:
    while True:
        time.sleep(60)
        try:
            workflow.escalate_overdue()
        except Exception:
            log.exception("escalation check failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    threading.Thread(target=_escalation_loop, daemon=True).start()
    yield


app = FastAPI(title="OpsPilot", lifespan=lifespan)


def _error(status: int):
    async def handler(request, exc):
        return JSONResponse({"detail": str(exc.args[0]) if exc.args else str(exc)}, status_code=status)
    return handler


for exc_type, code in [(PermissionError, 403), (workflow.InputError, 400), (ValueError, 409),
                       (LookupError, 404), (AgentError, 503)]:
    app.add_exception_handler(exc_type, _error(code))


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class NewRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    sender: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20_000)


class FieldEdit(BaseModel):
    field: str = Field(max_length=50)
    value: str | int | None = None
    reason: str = Field(default="", max_length=1000)


class Custom(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    est_cost_inr: int = Field(ge=0, le=1_000_000_000)


class Choice(BaseModel):
    option_index: int | None = None
    custom: Custom | None = None
    reason: str = Field(default="", max_length=1000)


class SupplierUpdate(BaseModel):
    delay_days: int = Field(ge=0, le=3650)
    note: str = Field(default="", max_length=1000)


class Transition(BaseModel):
    action: str = Field(max_length=20)
    note: str = Field(default="", max_length=1000)


class Audience(BaseModel):
    audience: str = Field(max_length=30)


class Decision(BaseModel):
    approve: bool
    reason: str = Field(default="", max_length=1000)


class ClockChange(BaseModel):
    hours: float | None = Field(default=None, ge=0, le=720)


class LLMSwitch(BaseModel):
    down: bool


def public(user: dict) -> dict:
    return {"username": user["username"], "name": user["name"], "role": user["role"]}


@app.post("/api/login")
def login(body: Login, response: Response):
    token = auth.login(body.username, body.password)
    if not token:
        raise HTTPException(401, "Wrong username or password")
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400)
    return public(db.one("SELECT * FROM users WHERE username=?", (body.username,)))


@app.post("/api/logout")
def logout(response: Response, user: dict = Depends(current_user)):
    db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return public(user)


@app.get("/api/inbox")
def inbox(user: dict = Depends(current_user)):
    return db.query("SELECT * FROM inbox ORDER BY received_at")


@app.post("/api/inbox/{inbox_id}/import")
def import_email(inbox_id: int, user: dict = Depends(current_user)):
    return {"id": workflow.import_email(inbox_id, user)}


@app.post("/api/requests")
def create_request(body: NewRequest, user: dict = Depends(current_user)):
    request_id = workflow.create_request(body.subject, body.body, body.sender, "manual", clock.iso(clock.now()), user)
    return {"id": request_id}


@app.get("/api/requests")
def list_requests(user: dict = Depends(current_user)):
    rows = db.query("SELECT id FROM requests ORDER BY id DESC")
    out = []
    for row in rows:
        req = workflow.get(row["id"])
        out.append({"id": req["id"], "subject": req["subject"], "status": req["status"],
                    "risk": (req["risk"] or {}).get("final_level"), "confidence": req["confidence"],
                    "deadline": req["facts"].get("deadline"), "flags": [f["code"] for f in req["flags"]]})
    return out


@app.get("/api/requests/{request_id}")
def request_detail(request_id: int, user: dict = Depends(current_user)):
    req = workflow.get(request_id)
    req["approvals"] = db.query("SELECT * FROM approvals WHERE request_id=? ORDER BY id", (request_id,))
    req["messages"] = db.query("SELECT * FROM messages WHERE request_id=? ORDER BY id", (request_id,))
    req["audit"] = [{**a, "detail": db.loads(a["detail"], {})}
                    for a in db.query("SELECT * FROM audit WHERE request_id=? ORDER BY id", (request_id,))]
    return req


@app.post("/api/requests/{request_id}/fields")
def edit_field(request_id: int, body: FieldEdit, user: dict = Depends(current_user)):
    workflow.correct_field(request_id, user, body.field, body.value, body.reason)
    return {"ok": True}


@app.post("/api/requests/{request_id}/choose")
def choose(request_id: int, body: Choice, user: dict = Depends(current_user)):
    custom = body.custom.model_dump() if body.custom else None
    workflow.choose(request_id, user, body.option_index, custom, body.reason)
    return {"ok": True}


@app.post("/api/requests/{request_id}/supplier-update")
def supplier_update(request_id: int, body: SupplierUpdate, user: dict = Depends(current_user)):
    workflow.supplier_update(request_id, user, body.delay_days, body.note)
    return {"ok": True}


@app.post("/api/requests/{request_id}/transition")
def transition(request_id: int, body: Transition, user: dict = Depends(current_user)):
    workflow.transition(request_id, user, body.action, body.note)
    return {"ok": True}


@app.post("/api/requests/{request_id}/retry")
def retry(request_id: int, user: dict = Depends(current_user)):
    workflow.retry(request_id, user)
    return {"ok": True}


@app.post("/api/requests/{request_id}/messages")
def draft_message(request_id: int, body: Audience, user: dict = Depends(current_user)):
    return {"id": workflow.draft(request_id, user, body.audience)}


@app.post("/api/messages/{message_id}/send")
def send_message(message_id: int, user: dict = Depends(current_user)):
    workflow.send(message_id, user)
    return {"ok": True}


@app.post("/api/requests/{request_id}/summary")
def summary(request_id: int, user: dict = Depends(current_user)):
    return {"summary": workflow.summarize(request_id, user)}


@app.post("/api/report")
def report(user: dict = Depends(current_user)):
    return workflow.portfolio_report()


@app.get("/api/approvals")
def approvals(mine: int = Query(0), user: dict = Depends(current_user)):
    rows = db.query("SELECT a.*, r.subject FROM approvals a JOIN requests r ON r.id = a.request_id "
                    "WHERE a.status='pending' ORDER BY a.due_at")
    if mine:
        rows = [a for a in rows if rules.can_approve(user["role"], a["amount"])]
    return rows


@app.post("/api/approvals/{approval_id}/decide")
def decide(approval_id: int, body: Decision, user: dict = Depends(current_user)):
    workflow.decide(approval_id, user, body.approve, body.reason)
    return {"ok": True}


@app.get("/api/admin/state")
def admin_state(user: dict = Depends(require("admin"))):
    return {"now": clock.iso(clock.now()), "offset_hours": clock.offset_hours(),
            "llm_down": db.get_setting("llm_down") == "1"}


@app.post("/api/admin/clock")
def admin_clock(body: ClockChange, user: dict = Depends(require("admin"))):
    offset = 0.0 if body.hours is None else clock.offset_hours() + body.hours
    db.set_setting("clock_offset_hours", str(offset))
    db.log(None, user["username"], "user", "clock_changed", {"offset_hours": offset})
    return {"offset_hours": offset, "escalated": workflow.escalate_overdue()}


@app.post("/api/admin/llm")
def admin_llm(body: LLMSwitch, user: dict = Depends(require("admin"))):
    db.set_setting("llm_down", "1" if body.down else "0")
    db.log(None, user["username"], "user", "llm_switch", {"down": body.down})
    return {"llm_down": body.down}


@app.get("/api/health")
def health():
    return {"ok": True, "db": db.one("SELECT 1 AS ok") is not None, "llm_down": db.get_setting("llm_down") == "1"}


if STATIC.exists():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
