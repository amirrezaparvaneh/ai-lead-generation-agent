import os
import time
import httpx
from typing import Dict, Any, Tuple

# این همان کلاسی است که جا افتاده بود و باعث خطای ۵۰۲ شد
class LLMError(Exception):
    pass

# کلید API خودت را اینجا وارد کن
API_KEY = os.getenv("AVALAI_API_KEY", "کلید_ای_پی_آی_شما")  
BASE_URL = "https://api.avalai.ir/v1/chat/completions"

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
            if e.response.status_code == 429:
                if attempt == max_retries - 1:
                    raise LLMError(f"Rate limit exceeded after {max_retries} attempts")
                
                retry_after = e.response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_time = int(retry_after)
                else:
                    wait_time = base_wait * (2 ** attempt)
                
                print(f"Rate limit hit! Waiting {wait_time} seconds before retry (Attempt {attempt+1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                raise LLMError(f"API Error: {e.response.status_code}")
        except Exception as e:
             raise LLMError(f"Request failed: {str(e)}")

def chat_json(model: str, price: float, system: str, user: str) -> Tuple[Dict[str, Any], float]:
    data = _call(model, system, user, max_tokens=1500, temperature=0.2)
    
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    
    cost = ((prompt_tokens + completion_tokens) / 1000) * price
    
    import json
    content = data["choices"][0]["message"]["content"]
    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError:
        parsed_content = {"error": "Invalid JSON response", "raw": content}
        
    return parsed_content, cost