from typing import Optional, TypedDict
from pydantic import BaseModel
from langgraph.graph import StateGraph, END

from . import config
from .llm import LLMError, chat_json


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


class State(TypedDict):
    product: str
    message: str
    context: list
    triage: Optional[dict]
    analysis: Optional[dict]
    fit: Optional[dict]
    draft: Optional[dict]
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


def triage(state: State):
    system = (
        "تو یک غربال‌گر هستی. فقط با JSON پاسخ بده: "
        '{"relevant": true/false, "reason": "..."}. '
        "پیام وقتی relevant است که نویسنده خودش نیازی دارد که این محصول می‌تواند برآورده کند "
        "(مثلاً دنبال یادگیری، دوره یا راهنمایی برای شروع است). "
        "پرسش تخصصی کسی که به موضوع مسلط است، یا پیامی که ربطی به محصول ندارد، relevant نیست. "
        "اگر مطمئن نیستی، relevant را true بگذار."
    )
    user = f"محصول:\n{state['product']}\n\nپیام:\n{state['message']}"
    res, acc = _run(state, config.TRIAGE_MODEL, config.TRIAGE_PRICE, Triage, system, user)
    return {"triage": res, "stage": "triage", **acc}


def analyze(state: State):
    system = (
        "زمینه‌ی گفتگو را بخوان و فقط با JSON پاسخ بده: "
        '{"level": "سطح فرد", "need": "نیاز واقعی او", "intent": "قصد او (یادگیری/شغل/کنجکاوی...)"}.'
    )
    user = f"زمینه:\n{_ctx(state)}\n\nپیام:\n{state['message']}"
    res, acc = _run(state, config.DEEP_MODEL, config.DEEP_PRICE, Analysis, system, user)
    return {"analysis": res, "stage": "analysis", **acc}


def fit(state: State):
    system = (
        "تناسب این فرد با محصول را بسنج. فقط با JSON پاسخ بده: "
        '{"score": عدد ۰ تا ۱۰۰, "reason": "دلیل تناسب یا عدم تناسب"}. '
        "اگر سطح یا قصد فرد با محصول نمی‌خواند، امتیاز را پایین بده."
    )
    user = f"محصول:\n{state['product']}\n\nتحلیل فرد:\n{state['analysis']}"
    res, acc = _run(state, config.DEEP_MODEL, config.DEEP_PRICE, Fit, system, user)
    return {"fit": res, "stage": "fit", **acc}


def draft(state: State):
    system = (
        "یک پاسخ کوتاه، دوستانه و غیرتبلیغاتی به فارسی بنویس که اول به سؤال فرد کمک کند "
        'و بعد به‌طور طبیعی محصول را معرفی کند. فقط JSON: {"reply": "..."}.'
    )
    user = f"محصول:\n{state['product']}\n\nپیام:\n{state['message']}\n\nتحلیل:\n{state['analysis']}"
    res, acc = _run(state, config.DEEP_MODEL, config.DEEP_PRICE, Draft, system, user)
    return {"draft": res, "stage": "draft", **acc}


def _after_triage(state):
    return "analyze" if state["triage"]["relevant"] else END


def _after_fit(state):
    return "draft" if state["fit"]["score"] >= config.FIT_THRESHOLD else END


def build():
    g = StateGraph(State)
    for name, fn in [("triage", triage), ("analyze", analyze), ("fit", fit), ("draft", draft)]:
        g.add_node(name, fn)
    g.set_entry_point("triage")
    g.add_conditional_edges("triage", _after_triage)
    g.add_edge("analyze", "fit")
    g.add_conditional_edges("fit", _after_fit)
    g.add_edge("draft", END)
    return g.compile()


graph = build()


def evaluate(product, message, context=None):
    init = {
        "product": product, "message": message, "context": context or [],
        "triage": None, "analysis": None, "fit": None, "draft": None,
        "stage": "", "tokens": 0, "cost": 0.0,
    }
    try:
        return graph.invoke(init)
    except (LLMError, ValueError) as e:
        return {**init, "stage": "error", "error": str(e)}
