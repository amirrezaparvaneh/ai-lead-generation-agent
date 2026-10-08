import os
import time
import httpx
from typing import Dict, Any, Tuple

# خواندن کلید API از متغیرهای محیطی یا فایل تنظیمات
API_KEY = os.getenv("AVALAI_API_KEY", "کلید_ای_پی_آی_شما")  # اگر کلید هاردکد شده بود، اینجا جایگزین کن
BASE_URL = "https://api.avalai.ir/v1/chat/completions"

# تنظیم یک کلاینت مشترک برای افزایش سرعت اتصال (Connection Pooling)
client = httpx.Client(timeout=60.0)

def _call(model: str, system: str, user: str, max_tokens: int, temperature: float) -> Dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"}
    }
    
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    max_retries = 5
    base_wait = 2

    for attempt in range(max_retries):
        try:
            resp = client.post(BASE_URL, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
            
        except httpx.HTTPStatusError as e:
            # اگر خطای ۴۲۹ (محدودیت درخواست) گرفتیم
            if e.response.status_code == 429:
                if attempt == max_retries - 1:
                    raise  # اگر تلاش‌ها تمام شد، ارور بده
                
                # خواندن زمان انتظار از هدر AvalAI یا استفاده از تاخیر نمایی
                retry_after = e.response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_time = int(retry_after)
                else:
                    wait_time = base_wait * (2 ** attempt)
                
                print(f"Rate limit hit! Waiting {wait_time} seconds before retry (Attempt {attempt+1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                # اگر خطای دیگری مثل ۵۰۰ یا ۴۰۱ بود، مستقیماً ارور بده
                raise

def chat_json(model: str, price: float, system: str, user: str) -> Tuple[Dict[str, Any], float]:
    # استفاده از _call جدید که خودش مدیریت خطا دارد
    data = _call(model, system, user, max_tokens=1500, temperature=0.2)
    
    # محاسبه هزینه بر اساس توکن‌های مصرفی
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    
    # فرض می‌کنیم قیمت ورودی و خروجی یکسان است (بر اساس هزار توکن)
    cost = ((prompt_tokens + completion_tokens) / 1000) * price
    
    import json
    content = data["choices"][0]["message"]["content"]
    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError:
        # در صورتی که خروجی دقیقا JSON معتبر نبود
        parsed_content = {"error": "Invalid JSON response", "raw": content}
        
    return parsed_content, cost