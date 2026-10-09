import json
import os
import pathlib
import re
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Union

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import threading
from . import auth, db
from .agent import evaluate

from .sms import generate_otp, verify_otp_code, send_sms_melipayamak

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTEXT_WINDOW = 3
MAX_MESSAGES = 50
MAX_QUOTA = 1000  # سقف مصرف کلی برای کاربر

app = FastAPI(title="Lead Finder")
db.init()

# پیشرفت لحظه‌ای تحلیل، به ازای هر کاربر (فقط برای یک پردازه؛ با --workers=1 اجرا شود)
PROGRESS = {}
PROGRESS_LOCK = threading.Lock()

# تنظیمات Rate Limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=6)


class RegistrationIn(Credentials):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)

class ProductIn(BaseModel):
    text: str = Field(min_length=10, max_length=2000)
class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    sender_name: Optional[str] = Field(default=None, max_length=100)
    sender_username: Optional[str] = Field(default=None, max_length=100)
    sender_id: Optional[str] = Field(default=None, max_length=50)
    message_link: Optional[str] = Field(default=None, max_length=255)
    telegram_update_id: Optional[str] = Field(default=None, max_length=100)


class AnalyzeIn(BaseModel):
    messages: List[Union[str, MessageIn]]
    source: str = "نامشخص"  # اتصال پلتفرم به بک‌اند


class FeedbackIn(BaseModel):
    good: bool


class ReportIn(BaseModel):
    kind: str
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=10, max_length=4000)


class ReviewReportIn(BaseModel):
    action: str
    message_credit: int = Field(default=0, ge=0, le=10000)
    rial_credit: int = Field(default=0, ge=0, le=100_000_000)
    note: str = Field(default="", max_length=1000)


class AdminLoginIn(BaseModel):
    username: str
    password: str


class CreditUserIn(BaseModel):
    message_credit: int = Field(default=0, ge=0, le=10000)
    rial_credit: int = Field(default=0, ge=0, le=100_000_000)
    note: str = Field(default="", max_length=1000)


def require_admin(admin_session: Optional[str] = Cookie(default=None)):
    if not admin_session or not db.admin_session(admin_session):
        raise HTTPException(401, "admin login required")

def _login(response: Response, user_id: int):
    token = auth.new_session(user_id)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=7 * 86400)

@app.post("/api/register")
@limiter.limit("5/minute")
def register(request: Request, body: RegistrationIn, response: Response):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "invalid email")
    try:
        user_id = db.execute(
            "INSERT INTO users(email, pw, first_name, last_name) VALUES(?, ?, ?, ?)",
            (email, auth.hash_password(body.password), body.first_name.strip(), body.last_name.strip()),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "email already registered")
    _login(response, user_id)
    return {"email": email, "first_name": body.first_name.strip(), "last_name": body.last_name.strip()}

@app.post("/api/login")
@limiter.limit("10/minute")
def login(request: Request, body: Credentials, response: Response):
    rows = db.query("SELECT id, pw FROM users WHERE email=?", (body.email.strip().lower(),))
    if not rows or not auth.check_password(body.password, rows[0]["pw"]):
        raise HTTPException(401, "wrong email or password")
    _login(response, rows[0]["id"])
    user = db.query("SELECT first_name, last_name FROM users WHERE id=?", (rows[0]["id"],))[0]
    return {"email": body.email.strip().lower(), "first_name": user["first_name"], "last_name": user["last_name"]}

@app.post("/api/logout")
def logout(response: Response, user=Depends(auth.current_user)):
    db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    response.delete_cookie("session")
    return {"ok": True}

@app.get("/api/me")
def me(user=Depends(auth.current_user)):
    product = db.query("SELECT text FROM products WHERE user_id=?", (user["id"],))
    return {"email": user["email"], "phone": user["phone"], "first_name": user["first_name"], "last_name": user["last_name"], "product": product[0]["text"] if product else None}

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

def _store(user_id, message, state):
    """Store the analyzed message with sender identity and Telegram permalink."""
    fit, triage = state.get("fit"), state.get("triage")
    reason = (fit or triage or {}).get("reason") or state.get("error")
    return db.execute(
        "INSERT INTO results(user_id, text, sender_name, sender_username, sender_id, message_link, telegram_update_id, "
        "stage, is_lead, score, reason, analysis, draft, tokens, cost) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, message["text"], message.get("sender_name"), message.get("sender_username"),
            message.get("sender_id"), message.get("message_link"), message.get("telegram_update_id"),
            state["stage"], int(state["stage"] == "draft"), fit["score"] if fit else None, reason,
            json.dumps(state.get("analysis"), ensure_ascii=False), (state.get("draft") or {}).get("reply"),
            state["tokens"], state["cost"],
        ),
    )

def telegram_webhook_key(x_telegram_webhook_secret: Optional[str] = Header(default=None)):
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected or x_telegram_webhook_secret != expected:
        raise HTTPException(401, "invalid telegram webhook secret")


def _telegram_message_link(chat, message_id):
    if not message_id:
        return None
    username = chat.get("username")
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id = str(chat.get("id", ""))
    clean_id = chat_id[4:] if chat_id.startswith("-100") else chat_id.lstrip("-")
    return f"https://t.me/c/{clean_id}/{message_id}" if clean_id else None


@app.post("/api/leads/telegram-webhook")
@limiter.limit("120/minute")
async def telegram_webhook(request: Request, _: None = Depends(telegram_webhook_key)):
    data = await request.json()
    message = data.get("message") or {}
    raw_text = (message.get("text") or "").strip()
    if not raw_text:
        return {"status": "ignored_no_text"}

    from_user = message.get("from") or {}
    chat = message.get("chat") or {}
    sender_name = f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip() or "کاربر تلگرام"
    sender_username = from_user.get("username")
    sender_id = str(from_user["id"]) if from_user.get("id") is not None else None
    message_id = message.get("message_id")
    update_id = str(data["update_id"]) if data.get("update_id") is not None else None
    message_link = _telegram_message_link(chat, message_id)

    # این روت باید به یک کاربر/محصول متصل باشد؛ تنظیمات اختصاصی تلگرام در مرحله اتصال افزوده می‌شود.
    users = db.query("SELECT id FROM users ORDER BY id LIMIT 1")
    if not users:
        raise HTTPException(503, "no user is configured for telegram webhook")
    if update_id and db.query("SELECT id FROM results WHERE telegram_update_id=?", (update_id,)):
        return {"status": "duplicate", "update_id": update_id}

    product = db.query("SELECT text FROM products WHERE user_id=?", (users[0]["id"],))
    if not product:
        raise HTTPException(409, "set a product before receiving telegram messages")
    examples = db.feedback_examples(users[0]["id"])
    state = evaluate(product[0]["text"], raw_text, [], "telegram", examples)
    result_id = _store(users[0]["id"], {
        "text": raw_text, "sender_name": sender_name, "sender_username": sender_username,
        "sender_id": sender_id, "message_link": message_link, "telegram_update_id": update_id,
    }, state)
    return {"status": "success", "lead_id": result_id, "is_lead": state.get("stage") == "draft"}
@app.post("/api/process")
@limiter.limit("30/minute")  # سقف رو برای زمان تست بالا بردیم تا خطای اسپم ندهد
def process(request: Request, body: AnalyzeIn, user=Depends(auth.current_user)):
    product = db.query("SELECT text FROM products WHERE user_id=?", (user["id"],))
    if not product:
        raise HTTPException(400, "set your product first")
    
    usage_res = db.query("SELECT COUNT(id) as c FROM results WHERE user_id=?", (user["id"],))
    current_usage = usage_res[0]["c"] if usage_res else 0
    
    wallet = db.wallet(user["id"])
    free_capacity = max(0, MAX_QUOTA - current_usage)
    allowed_capacity = free_capacity + wallet["message_credit"]
    if not allowed_capacity:
        raise HTTPException(402, "سقف مجاز شما به پایان رسیده است. با گزارش باگ یا پیشنهاد مؤثر، اعتبار پیام دریافت کنید.")

    messages = []
    for item in body.messages:
        message = item if isinstance(item, MessageIn) else MessageIn(text=item)
        messages.append(message.model_dump())
    messages = [{**m, "text": m["text"].strip()[:1000]} for m in messages if m["text"].strip()]
    limit = min(MAX_MESSAGES, allowed_capacity)
    messages = messages[:limit]

    if not messages:
        raise HTTPException(400, "no messages")

    examples = db.feedback_examples(user["id"])
    total = len(messages)
    with PROGRESS_LOCK:
        PROGRESS[user["id"]] = {"done": 0, "total": total}

    def work(i):
        context = [item["text"] for item in messages[max(0, i - CONTEXT_WINDOW):i]]
        return evaluate(product[0]["text"], messages[i]["text"], context, body.source, examples)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            states = list(pool.map(work, range(len(messages))))
        ids = [_store(user["id"], message, state) for message, state in zip(messages, states)]
    finally:
        with PROGRESS_LOCK:
            PROGRESS.pop(user["id"], None)

    credited_messages_used = db.consume_message_credit(user["id"], max(0, len(messages) - free_capacity))
    return {"ids": ids, "usage": {"current": current_usage + len(messages), "max": MAX_QUOTA}, "message_credit_used": credited_messages_used}


@app.get("/api/progress")
def progress(user=Depends(auth.current_user)):
    with PROGRESS_LOCK:
        p = PROGRESS.get(user["id"])
    return p or {"done": 0, "total": 0}

@app.get("/api/results")
def results(only_leads: bool = False, user=Depends(auth.current_user)):
    sql = "SELECT * FROM results WHERE user_id=?"
    if only_leads:
        sql += " AND is_lead=1"
    return db.query(sql + " ORDER BY id DESC", (user["id"],))

@app.delete("/api/results")
def clear_results(user=Depends(auth.current_user)):
    with db.conn() as c:
        deleted = c.execute("DELETE FROM results WHERE user_id=?", (user["id"],)).rowcount
    return {"ok": True, "deleted": deleted}

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


@app.get("/api/wallet")
def get_wallet(user=Depends(auth.current_user)):
    return {"balance": db.wallet(user["id"]), "transactions": db.query("SELECT message_credit, rial_credit, description, created_at FROM wallet_transactions WHERE user_id=? ORDER BY id DESC LIMIT 20", (user["id"],))}


@app.get("/api/reports")
def get_reports(user=Depends(auth.current_user)):
    return db.query("SELECT id, kind, title, description, status, admin_note, created_at, reviewed_at FROM reports WHERE user_id=? ORDER BY id DESC", (user["id"],))


@app.post("/api/reports")
@limiter.limit("10/hour")
def create_report(request: Request, body: ReportIn, user=Depends(auth.current_user)):
    if body.kind not in {"bug", "suggestion"}:
        raise HTTPException(400, "invalid report type")
    report_id = db.execute("INSERT INTO reports(user_id, kind, title, description) VALUES(?,?,?,?)", (user["id"], body.kind, body.title.strip(), body.description.strip()))
    return {"id": report_id, "status": "pending"}


@app.post("/api/admin/login")
@limiter.limit("5/minute")
def admin_login(request: Request, body: AdminLoginIn, response: Response):
    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "12345")
    if not secrets.compare_digest(body.username, username) or not secrets.compare_digest(body.password, password):
        raise HTTPException(401, "نام کاربری یا گذرواژه نادرست است.")
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO admin_sessions(token) VALUES(?)", (token,))
    response.set_cookie("admin_session", token, httponly=True, samesite="strict", max_age=8 * 3600)
    return {"ok": True}


@app.post("/api/admin/logout")
def admin_logout(response: Response, admin=Depends(require_admin), admin_session: Optional[str] = Cookie(default=None)):
    db.execute("DELETE FROM admin_sessions WHERE token=?", (admin_session,))
    response.delete_cookie("admin_session")
    return {"ok": True}


@app.get("/api/admin/overview")
def admin_overview(admin=Depends(require_admin)):
    return {
        "users": db.query("SELECT COUNT(*) AS count FROM users")[0]["count"],
        "reports_pending": db.query("SELECT COUNT(*) AS count FROM reports WHERE status='pending'")[0]["count"],
        "reports_approved": db.query("SELECT COUNT(*) AS count FROM reports WHERE status='approved'")[0]["count"],
        "reports_rejected": db.query("SELECT COUNT(*) AS count FROM reports WHERE status='rejected'")[0]["count"],
        "results": db.query("SELECT COUNT(*) AS count FROM results")[0]["count"],
    }


@app.get("/api/admin/users")
def admin_users(admin=Depends(require_admin)):
    return db.query(
        "SELECT u.id, u.email, u.phone, COUNT(r.id) AS messages, COALESCE(SUM(r.cost), 0) AS usage_cost, "
        "COALESCE(w.message_credit, 0) AS message_credit, COALESCE(w.rial_credit, 0) AS rial_credit, "
        "COALESCE((SELECT SUM(t.rial_credit) FROM wallet_transactions t WHERE t.user_id=u.id AND t.rial_credit > 0), 0) AS payments "
        "FROM users u LEFT JOIN results r ON r.user_id=u.id LEFT JOIN wallets w ON w.user_id=u.id "
        "GROUP BY u.id ORDER BY u.id DESC"
    )


@app.post("/api/admin/users/{user_id}/credit")
def admin_credit_user(user_id: int, body: CreditUserIn, admin=Depends(require_admin)):
    if not body.message_credit and not body.rial_credit:
        raise HTTPException(400, "حداقل یک اعتبار وارد کنید.")
    try:
        db.credit_user(user_id, body.message_credit, body.rial_credit, body.note.strip())
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


@app.get("/api/admin/reports")
def admin_reports(status: str = "pending", user=Depends(require_admin)):
    if status not in {"pending", "approved", "rejected", "all"}:
        raise HTTPException(400, "invalid status")
    sql = "SELECT r.*, u.email, u.phone FROM reports r JOIN users u ON u.id=r.user_id"
    args = ()
    if status != "all":
        sql += " WHERE r.status=?"
        args = (status,)
    return db.query(sql + " ORDER BY r.id DESC", args)


@app.post("/api/admin/reports/{report_id}/review")
def review_report(report_id: int, body: ReviewReportIn, user=Depends(require_admin)):
    try:
        if body.action == "approve":
            db.credit_report(report_id, body.message_credit, body.rial_credit, body.note.strip())
        elif body.action == "reject":
            db.reject_report(report_id, body.note.strip())
        else:
            raise HTTPException(400, "invalid review action")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}


class SendOTPRequest(BaseModel):
    phone: str

class VerifyOTPRequest(BaseModel):
    phone: str
    code: str

@app.post("/api/auth/send-otp")
@limiter.limit("3/minute")
def send_otp_endpoint(request: Request, req: SendOTPRequest):
    phone = req.phone.strip()
    if not re.fullmatch(r"09\d{9}", phone):
        raise HTTPException(status_code=400, detail="شماره موبایل باید ۱۱ رقمی و با ۰۹ شروع شود.")

    code = generate_otp(phone)
    if not send_sms_melipayamak(phone, code):
        from .sms import OTP_CACHE, OTP_ATTEMPTS, OTP_LOCK
        with OTP_LOCK:
            OTP_CACHE.pop(phone, None)
            OTP_ATTEMPTS.pop(phone, None)
        raise HTTPException(status_code=502, detail="ارسال پیامک ناموفق بود؛ تنظیمات پنل و الگوی پیامک را بررسی کنید.")

    return {"status": "success", "message": "کد تایید ارسال گردید."}

@app.post("/api/auth/verify-otp")
@limiter.limit("5/minute")
def verify_otp_endpoint(request: Request, req: VerifyOTPRequest, response: Response):
    phone = req.phone.strip()
    code = req.code.strip()
    if not re.fullmatch(r"09\d{9}", phone) or not re.fullmatch(r"\d{4}", code):
        raise HTTPException(status_code=400, detail="شماره موبایل یا کد تایید معتبر نیست.")
    if not verify_otp_code(phone, code):
        raise HTTPException(status_code=400, detail="کد وارد شده اشتباه است یا منقضی شده.")

    with db.conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if row:
            user_id = row["id"]
        else:
            # حساب OTP ایمیل/رمز عبور ندارد؛ از جعل یا تداخل ایمیل جلوگیری می‌کند.
            email = f"{secrets.token_hex(24)}@otp.invalid"
            user_id = conn.execute(
                "INSERT INTO users(email, pw, phone) VALUES(?, ?, ?)",
                (email, auth.hash_password(secrets.token_urlsafe(32)), phone),
            ).lastrowid

    _login(response, user_id)
    return {"status": "success", "message": "ورود با موفقیت انجام شد.", "phone": phone}

STATIC = ROOT / "static"
if STATIC.exists():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")