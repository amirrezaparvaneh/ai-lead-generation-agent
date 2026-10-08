import json
import pathlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import List

from fastapi import Depends, FastAPI, HTTPException, Response, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ابزارهای Rate Limiting (استاندارد)
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from . import auth, db
from .agent import evaluate

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTEXT_WINDOW = 3
MAX_MESSAGES = 50
MAX_QUOTA = 100  # سقف مصرف کلی برای کاربر (Quota)

app = FastAPI(title="Lead Finder")
db.init()

# تنظیمات Rate Limiter (شناسایی بر اساس IP کاربر)
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=6)

class ProductIn(BaseModel):
    text: str = Field(min_length=10, max_length=2000)

class AnalyzeIn(BaseModel):
    messages: List[str]
    source: str = "نامشخص" 

class FeedbackIn(BaseModel):
    good: bool

def _login(response: Response, user_id: int):
    token = auth.new_session(user_id)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=7 * 86400)

@app.post("/api/register")
@limiter.limit("5/minute")  # جلوگیری از ساخت اکانت اسپم
def register(request: Request, body: Credentials, response: Response):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "invalid email")
    try:
        user_id = db.execute(
            "INSERT INTO users(email, pw) VALUES(?, ?)",
            (email, auth.hash_password(body.password)),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "email already registered")
    _login(response, user_id)
    return {"email": email}

@app.post("/api/login")
@limiter.limit("10/minute")
def login(request: Request, body: Credentials, response: Response):
    rows = db.query("SELECT id, pw FROM users WHERE email=?", (body.email.strip().lower(),))
    if not rows or not auth.check_password(body.password, rows[0]["pw"]):
        raise HTTPException(401, "wrong email or password")
    _login(response, rows[0]["id"])
    return {"email": body.email.strip().lower()}

@app.post("/api/logout")
def logout(response: Response, user=Depends(auth.current_user)):
    db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    response.delete_cookie("session")
    return {"ok": True}

@app.get("/api/me")
def me(user=Depends(auth.current_user)):
    product = db.query("SELECT text FROM products WHERE user_id=?", (user["id"],))
    return {"email": user["email"], "product": product[0]["text"] if product else None}

@app.put("/api/product")
def set_product(body: ProductIn, user=Depends(auth.current_user)):
    db.execute(
        "INSERT INTO products(user_id, text) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET text=excluded.text",
        (user["id"], body.text.strip()),
    )
    return {"ok": True}

@app.get("/api/sample")
def sample():
    path = ROOT / "data" / "sample_messages.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"product": data["product"], "messages": [m["text"] for m in data["messages"]]}

def _store(user_id, text, state):
    fit, triage = state.get("fit"), state.get("triage")
    reason = (fit or triage or {}).get("reason") or state.get("error")
    rid = db.execute(
        "INSERT INTO results(user_id, text, stage, is_lead, score, reason, analysis, "
        "draft, tokens, cost) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, text, state["stage"], int(state["stage"] == "draft"),
            fit["score"] if fit else None, reason,
            json.dumps(state.get("analysis"), ensure_ascii=False),
            (state.get("draft") or {}).get("reply"),
            state["tokens"], state["cost"],
        ),
    )
    return rid

@app.post("/api/analyze")
@limiter.limit("3/minute")  # محافظت در برابر اسپم (نهایتا ۳ درخواست در دقیقه)
def analyze(request: Request, body: AnalyzeIn, user=Depends(auth.current_user)):
    product = db.query("SELECT text FROM products WHERE user_id=?", (user["id"],))
    if not product:
        raise HTTPException(400, "set your product first")
    
    # --- بررسی سقف مصرف (Quota) ---
    usage_res = db.query("SELECT COUNT(id) as c FROM results WHERE user_id=?", (user["id"],))
    current_usage = usage_res[0]["c"] if usage_res else 0
    
    if current_usage >= MAX_QUOTA:
        raise HTTPException(402, f"سقف مجاز شما ({MAX_QUOTA} پیام) به پایان رسیده است.")

    messages = [m.strip()[:1000] for m in body.messages if m.strip()]
    allowed_capacity = MAX_QUOTA - current_usage
    limit = min(MAX_MESSAGES, allowed_capacity)
    messages = messages[:limit]

    if not messages:
        raise HTTPException(400, "no messages")

    def work(i):
        context = messages[max(0, i - CONTEXT_WINDOW):i]
        return evaluate(product[0]["text"], messages[i], context, body.source)

    with ThreadPoolExecutor(max_workers=4) as pool:
        states = list(pool.map(work, range(len(messages))))
    ids = [_store(user["id"], t, s) for t, s in zip(messages, states)]
    
    return {"ids": ids, "usage": {"current": current_usage + len(messages), "max": MAX_QUOTA}}

@app.get("/api/results")
def results(only_leads: bool = False, user=Depends(auth.current_user)):
    sql = "SELECT * FROM results WHERE user_id=?"
    if only_leads:
        sql += " AND is_lead=1"
    return db.query(sql + " ORDER BY id DESC", (user["id"],))

@app.post("/api/results/{result_id}/feedback")
def feedback(result_id: int, body: FeedbackIn, user=Depends(auth.current_user)):
    db.execute(
        "UPDATE results SET feedback=? WHERE id=? AND user_id=?",
        (int(body.good), result_id, user["id"]),
    )
    return {"ok": True}

@app.get("/api/stats")
def stats(user=Depends(auth.current_user)):
    return db.stats(user["id"])

STATIC = ROOT / "static"
if STATIC.exists():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")