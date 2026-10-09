import os
from dotenv import load_dotenv

load_dotenv()


def _price(name, default):
    a, b = os.getenv(name, default).split(",")
    return float(a), float(b)


LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
TRIAGE_MODEL = os.getenv("TRIAGE_MODEL", "gpt-4o-mini")
DEEP_MODEL = os.getenv("DEEP_MODEL", "gpt-4o")
TRIAGE_PRICE = _price("TRIAGE_PRICE", "0.15,0.6")
DEEP_PRICE = _price("DEEP_PRICE", "2.5,10")
FIT_THRESHOLD = int(os.getenv("FIT_THRESHOLD", "60"))
USD_TO_RIAL_FALLBACK = float(os.getenv("USD_TO_RIAL_FALLBACK", "600000"))
EXCHANGE_RATE_REFRESH_SECONDS = int(os.getenv("EXCHANGE_RATE_REFRESH_SECONDS", "21600"))
