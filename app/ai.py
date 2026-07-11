# app/ai.py  (Task 4 replaces this file entirely)
from .config import settings


def available() -> bool:
    return bool(settings.anthropic_api_key)
