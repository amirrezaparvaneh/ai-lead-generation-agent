from typing import Optional, TypedDict
from pydantic import BaseModel
from langgraph.graph import StateGraph, END

from . import config
from .llm import LLMError, chat_json

# --- Pydantic Models ---
class Triage(BaseModel):
    relevant: bool
    reason: str

class Analysis(BaseModel):
    level: str
    need: str
    intent: str

class Fit(BaseModel):
    score: int
    reason: str

class Draft(BaseModel):
    reply: str

class Review(BaseModel):
    approved: bool
    feedback: str

# --- State Definition ---
class State(TypedDict):
    product: str
    message: str
    source: str          # مبدأ پیام: تلگرام، اینستاگرام، لینکدین و...
    context: list
    triage: Optional[dict]
    analysis: Optional[dict]
    fit: Optional[dict]
    draft: Optional[dict]
    review: Optional[dict] # نتیجه بررسی منتقد
    revision_count: int  # تعداد دفعات بازنویسی
    feedback_examples: list  # بازخورد تیم فروش روی موارد قبلی
    stage: str
    tokens: int
    cost: float

def _run(state, model, price, schema, system, user):
    out, usage = chat_json(model, price, system, user)
    return schema(**out).model_dump(), {
        "tokens": state["tokens"] + usage["tokens"],
        "cost": state["cost"] + usage["cost"],
    }

def _ctx(state):
    return "\n".join(state["context"]) or "(بدون زمینه)"

# --- Agents ---
def triage(state: State):
    system = (
        "تو یک تحلیل‌گر ارتباطات تجاری هستی. فقط با JSON پاسخ بده: "
        '{"relevant": true/false, "reason": "..."}. '
        "با توجه به ماهیت محصولی که به تو داده می‌شود، بررسی کن آیا پیام کاربر ربطی به این کسب‌وکار دارد یا یک پیام عمومی/اسپم است. "
        "اگر کاربر نیاز یا کنجکاوی مرتبط با محصول دارد، relevant را true بگذار."
    )
    user = f"محصول/خدمت:\n{state['product']}\n\nپیام:\n{state['message']}"
    res, acc = _run(state, config.TRIAGE_MODEL, config.TRIAGE_PRICE, Triage, system, user)
    return {"triage": res, "stage": "triage", **acc}

def analyze(state: State):
    system = (
        "زمینه‌ی گفتگو را بخوان و پروفایل کاربر را بساز. فقط با JSON پاسخ بده: "
        '{"level": "سطح تخصص کاربر", "need": "نیاز واقعی و پنهان او", "intent": "قصد اصلی (خرید/کنجکاوی/پشتیبانی)"}.'
    )
    user = f"زمینه:\n{_ctx(state)}\n\nپیام:\n{state['message']}"
    res, acc = _run(state, config.DEEP_MODEL, config.DEEP_PRICE, Analysis, system, user)
    return {"analysis": res, "stage": "analysis", **acc}

def fit(state: State):
    fb = state.get("feedback_examples") or []
    fb_lines = []
    for ex in fb[:5]:
        verdict = "درست بود" if ex["feedback"] else "اشتباه بود (نباید نشانه شمرده می‌شد یا برعکس)"
        fb_lines.append(f"- پیام قبلی: «{ex['text']}» | امتیاز دادی: {ex['score']} | نظر تیم فروش: این تشخیص {verdict}")
    fb_block = (
        "\n\nبازخورد تیم فروش روی تشخیص‌های قبلی خودت (برای کالیبره کردن قضاوت این بار استفاده کن، کورکورانه کپی نکن):\n"
        + "\n".join(fb_lines)
    ) if fb_lines else ""

    system = (
        "احتمال تبدیل شدن این فرد به مشتری (Lead Score) را بسنج. فقط با JSON پاسخ بده: "
        '{"score": عدد ۰ تا ۱۰۰, "reason": "دلیل این امتیاز"}. '
        "به قصد کاربر و همخوانی نیازش با محصول دقت کن."
        f"{fb_block}"
    )
    user = f"محصول:\n{state['product']}\n\nتحلیل فرد:\n{state['analysis']}"
    res, acc = _run(state, config.DEEP_MODEL, config.DEEP_PRICE, Fit, system, user)
    return {"fit": res, "stage": "fit", **acc}

def draft(state: State):
    # تنظیم لحن بر اساس پلتفرم
    tone_guide = "لحن صمیمی و محاوره‌ای داشته باش."
    source = state.get("source", "").lower()
    if "linkedin" in source or "لینکدین" in source:
        tone_guide = "لحن حرفه‌ای، رسمی و B2B داشته باش."
    
    # بررسی بازخورد منتقد (اگر از مرحله نقد برگشته باشد)
    feedback_text = ""
    if state.get("review") and not state["review"]["approved"]:
        feedback_text = f"\nبازخورد مدیر فروش برای اصلاح متن قبلی: {state['review']['feedback']}\nمتن را بر اساس این بازخورد بازنویسی کن."

    system = (
        "یک پاسخ کوتاه، جذاب و انسانی به فارسی بنویس که اول ارزش خلق کند و بعد محصول را ظریف معرفی کند. "
        f"{tone_guide} هرگز شبیه ربات یا متن کپی‌شده تبلیغاتی نباش. "
        'فقط با JSON پاسخ بده: {"reply": "..."}.'
        f"{feedback_text}"
    )
    user = f"محصول:\n{state['product']}\n\nپیام:\n{state['message']}\n\nتحلیل:\n{state['analysis']}"
    res, acc = _run(state, config.DEEP_MODEL, config.DEEP_PRICE, Draft, system, user)
    
    # افزایش کانتر بازنویسی
    new_revision_count = state.get("revision_count", 0) + 1 if state.get("review") else 0
    return {"draft": res, "stage": "draft", "revision_count": new_revision_count, **acc}

def review(state: State):
    system = (
        "تو مدیر فروش سخت‌گیری هستی. پاسخ پیشنهادی ایجنت را بخوان. "
        "اگر پاسخ خیلی طولانی، شبیه ربات، یا به شکل گل‌درشت تبلیغاتی است، آن را رد کن. "
        'فقط با JSON پاسخ بده: {"approved": true/false, "feedback": "اگر رد شد، دلیل و راهنمایی برای اصلاح"}.'
    )
    user = f"پیام مشتری:\n{state['message']}\n\nپاسخ پیشنهادی:\n{state['draft']['reply']}"
    res, acc = _run(state, config.DEEP_MODEL, config.DEEP_PRICE, Review, system, user)
    return {"review": res, "stage": "review", **acc}

# --- Graph Logic ---
def _after_triage(state):
    return "analyze" if state["triage"]["relevant"] else END

def _after_fit(state):
    return "draft" if state["fit"]["score"] >= config.FIT_THRESHOLD else END

def _after_review(state):
    # جلوگیری از حلقه بی‌نهایت: حداکثر ۲ بار بازنویسی
    if state["review"]["approved"] or state.get("revision_count", 0) >= 2:
        return END
    return "draft"

def build():
    g = StateGraph(State)
    for name, fn in [("triage", triage), ("analyze", analyze), ("fit", fit), ("draft", draft), ("review", review)]:
        g.add_node(name, fn)
    
    g.set_entry_point("triage")
    g.add_conditional_edges("triage", _after_triage)
    g.add_edge("analyze", "fit")
    g.add_conditional_edges("fit", _after_fit)
    g.add_edge("draft", "review")
    g.add_conditional_edges("review", _after_review)
    return g.compile()

graph = build()

def evaluate(product, message, context=None, source="نامشخص", feedback_examples=None):
    init = {
        "product": product, "message": message, "source": source, "context": context or [],
        "triage": None, "analysis": None, "fit": None, "draft": None, "review": None,
        "revision_count": 0, "feedback_examples": feedback_examples or [], "stage": "", "tokens": 0, "cost": 0.0,
    }
    try:
        return graph.invoke(init)
    except (LLMError, ValueError) as e:
        return {**init, "stage": "error", "error": str(e)}