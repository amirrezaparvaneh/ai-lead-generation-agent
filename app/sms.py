import os
import secrets
from threading import Lock
from fastapi import HTTPException
import time
import httpx
from typing import Dict, Tuple

# کش موقت کدهای پیامکی در حافظه: { "0912xxxxxxx": (code, expire_timestamp) }
OTP_CACHE: Dict[str, Tuple[str, float]] = {}

MELIPAYAMAK_USERNAME = os.getenv("MELIPAYAMAK_USERNAME", "")
MELIPAYAMAK_PASSWORD = os.getenv("MELIPAYAMAK_PASSWORD", "")
MELIPAYAMAK_BODY_ID = int(os.getenv("MELIPAYAMAK_BODY_ID", "0"))
OTP_LOCK = Lock()
OTP_ATTEMPTS = {}

def generate_otp(phone: str) -> str:
    """تولید کد ۴ رقمی و نگهداری به مدت ۲ دقیقه"""
    with OTP_LOCK:
        now = time.time()
        for expired in [key for key, (_, deadline) in OTP_CACHE.items() if deadline <= now]:
            OTP_CACHE.pop(expired, None)
            OTP_ATTEMPTS.pop(expired, None)
        if phone in OTP_CACHE:
            raise HTTPException(429, "برای ارسال مجدد کد، دو دقیقه صبر کنید.")
        code = str(secrets.randbelow(9000) + 1000)
        OTP_CACHE[phone] = (code, now + 120)
        OTP_ATTEMPTS[phone] = 0
        return code

def verify_otp_code(phone: str, code: str) -> bool:
    """کد یک‌بارمصرف با حداکثر پنج تلاش در بازه اعتبار."""
    with OTP_LOCK:
        if phone not in OTP_CACHE:
            return False
        saved_code, expire_at = OTP_CACHE[phone]
        if time.time() >= expire_at or OTP_ATTEMPTS.get(phone, 0) >= 5:
            return False
        OTP_ATTEMPTS[phone] = OTP_ATTEMPTS.get(phone, 0) + 1
        if secrets.compare_digest(saved_code, code):
            del OTP_CACHE[phone]
            OTP_ATTEMPTS.pop(phone, None)
            return True
        return False

def send_sms_melipayamak(phone: str, code: str) -> bool:
    """ارسال وب‌سرویس خدماتی و پترن ملی‌پیامک"""
    if not MELIPAYAMAK_USERNAME or not MELIPAYAMAK_PASSWORD or MELIPAYAMAK_BODY_ID <= 0:
        return False
    url = "https://rest.payamak-panel.com/api/SendSMS/BaseServiceNumber"
    payload = {
        "username": MELIPAYAMAK_USERNAME,
        "password": MELIPAYAMAK_PASSWORD,
        "text": code,  # مقدار متغیر پترن
        "to": phone,
        "bodyId": MELIPAYAMAK_BODY_ID
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                return False
            data = resp.json()
            return isinstance(data, dict) and data.get("RetStatus") == 1 and int(data.get("Value", 0)) > 0
    except (httpx.HTTPError, ValueError, TypeError):
        return False