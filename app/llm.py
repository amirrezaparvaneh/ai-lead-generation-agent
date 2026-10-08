import json
import httpx
from . import config


class LLMError(Exception):
    pass


def _call(model, system, user, max_tokens, temperature):
    resp = httpx.post(
        f"{config.LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
        json={
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def chat_json(model, price, system, user, max_tokens=500):
    """Call an OpenAI-compatible endpoint and return (parsed_json, usage).

    The reply is capped by max_tokens. If it comes back cut off or is not valid
    JSON, the call is retried once; the cost of both attempts is counted.
    """
    usage_total = {"tokens": 0, "cost": 0.0}
    last_error = None

    for attempt in range(2):
        data = _call(model, system, user, max_tokens, 0.2 if attempt == 0 else 0)
        usage = data["usage"]
        usage_total["tokens"] += usage["total_tokens"]
        usage_total["cost"] += (
            usage["prompt_tokens"] * price[0] + usage["completion_tokens"] * price[1]
        ) / 1e6

        choice = data["choices"][0]
        try:
            if choice.get("finish_reason") == "length":
                raise ValueError("reply was cut off at max_tokens")
            return json.loads(choice["message"]["content"]), usage_total
        except ValueError as e:
            last_error = e

    raise LLMError(f"invalid JSON from model: {last_error}")
